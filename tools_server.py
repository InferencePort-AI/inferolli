"""Tool loop — the bot can LOOK THINGS UP before answering.

Direct mentions run through a multi-turn loop: the model answers with either
a tool call ({tool, args}) or the final {reply, actions}. The harness
executes tools and feeds results back (max hops, then it must answer).

The tool surface itself lives in tool_registry.py: every tool below is a
decorated handler with a schema, tier, timeout and audit flag, and the prompt
the model sees is GENERATED from that registry — what's advertised and what's
installed are the same set by construction. This module owns the loop and the
handler bodies; run_tool is a thin delegate.
"""

import asyncio
import html
import json
import logging
import os
import re
import shlex
import shutil

import discord
import httpx

import ai
from ai import AIError
import memory
import settings
import tool_registry
from tool_registry import tool

log = logging.getLogger("tools")

TINYFISH_KEY = os.environ.get("TINYFISH_API_KEY", "").strip()
MAX_HOPS = 3

EVIDENCE_RULE = (
    "\n\nEVIDENCE RULE — this one is absolute:\n"
    "- Never claim you checked, looked up, searched, read, or verified something unless a "
    "tool result in THIS conversation actually showed it to you.\n"
    "- \"I think\", \"last I knew\", \"from memory\" are honest and fine. \"I checked\" is not, "
    "if you didn't.\n"
    "- If a lookup failed, say the lookup failed and name what blocked it. Don't paper over "
    "a failed tool call with a confident answer.\n"
    "- Absence of evidence is not evidence: not finding something doesn't prove it's untrue, "
    "outdated, or broken.\n"
    "- Before answering a question about current state (versions, prices, who said what, "
    "what's deployed), prefer one real lookup over a remembered guess."
)

# phrases that imply verification — used only to log when the model asserts
# something it didn't check (no behaviour change, just observability)
_CLAIM_RX = re.compile(
    r"\b(i (?:just |already )?(?:checked|looked|searched|read|verified|confirmed|pulled up)"
    r"|i(?:'ve| have) (?:checked|looked|searched|verified|confirmed)"
    r"|according to (?:the|my) (?:search|lookup|source)"
    r"|i found (?:that |it |the ))\b", re.I)


def claimed_without_evidence(reply: str, tool_calls: int) -> bool:
    """True when a reply asserts verification but no tool ran this turn."""
    return bool(reply) and tool_calls == 0 and bool(_CLAIM_RX.search(reply))


# The TOOLS section of the system prompt is GENERATED from the registry —
# only what exists can be advertised, and a test keeps the two from drifting.
# The registry is filled by the @tool decorators below, so this can't be a
# plain global computed at import time (it would snapshot an empty registry):
# module __getattr__ (PEP 562) makes tools_server.TOOLS_PROMPT generate on
# first access, after the whole module has run. Member view; run_loop asks
# the registry for the caller's view (admin adds the admin-tier tools).
def __getattr__(name):
    if name == "TOOLS_PROMPT":
        return tool_registry.tools_prompt(admin=False)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------- web search: TinyFish if keyed, DuckDuckGo otherwise ----------

@tool(name="web_search", description="search the web",
      args={"query": "str:300"}, timeout=25)
async def web_search(query: str) -> str:
    if TINYFISH_KEY:
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get("https://api.tinyfish.ai/v1/search",
                                params={"query": query},
                                headers={"Authorization": f"Bearer {TINYFISH_KEY}"})
                r.raise_for_status()
                data = r.json()
            results = data.get("results", []) or []
            if results:
                return "\n".join(f"- {r_.get('title', '')}: {r_.get('snippet', '')} ({r_.get('url', '')})"
                                 for r_ in results[:6])
            return "(no results)"
        except Exception as e:
            log.warning("tinyfish search failed (%s) — falling back to ddg", e)

    # keyless fallback: DuckDuckGo html endpoint
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; Inferolli/1.0)"}) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": query})
            r.raise_for_status()
        body = r.text
        items = re.findall(
            r'class="result__a"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
            body, re.S)
        out = []
        for title, snippet in items[:6]:
            clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            out.append(f"- {clean(title)}: {clean(snippet)[:160]}")
        return "\n".join(out) if out else "(no results)"
    except Exception as e:
        return f"(web search unavailable: {str(e)[:80]})"


@tool(name="fetch_page", description="read a web page",
      args={"url": "str:500"}, timeout=30)
async def fetch_page(url: str) -> str:
    if TINYFISH_KEY:
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                r = await c.get("https://api.tinyfish.ai/v1/fetch",
                                params={"url": url},
                                headers={"Authorization": f"Bearer {TINYFISH_KEY}"})
                r.raise_for_status()
                text = r.json().get("content") or r.text
                return text[:1500]
        except Exception as e:
            log.warning("tinyfish fetch failed (%s) — direct fetch", e)
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; Inferolli/1.0)"}) as c:
            r = await c.get(url)
            r.raise_for_status()
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:1500] or "(empty page)"
    except Exception as e:
        return f"(couldn't fetch: {str(e)[:80]})"


# ---------- server-side tools ----------

@tool(name="channel_history",
      description="recent messages from any channel",
      args={"channel": "str:80", "n": "?int:2..25"})
async def channel_history(bot, channel: str, n: int = 10) -> str:
    channel_name = channel
    for guild in bot.guilds:
        ch = discord_find_channel(guild, channel_name)
        if ch is None:
            return f"(no channel named {channel_name})"
        lines = []
        async for m in ch.history(limit=max(2, min(n, 25))):
            if not m.author.bot and m.content:
                lines.append(f"{m.author.display_name}: {m.content[:130]}")
        return f"#{ch.name} (recent):\n" + "\n".join(reversed(lines)) if lines else "(quiet in there)"
    return "(no channel found)"


def discord_find_channel(guild, name: str):
    name = name.strip().lstrip("#").lstrip("<").rstrip(">")
    name = name.split("│")[-1].split("|")[-1]
    ch = discord.utils.get(guild.text_channels, name=name)
    if ch is None:
        for c in guild.text_channels:
            if name in c.name:
                return c
    return ch


@tool(name="discord_search",
      description='search the server\'s ENTIRE history (reaches back further than '
                  'your own memory; use it for "what did we say about X a while ago")',
      args={"query": "str:300", "channel": "?str:80", "author": "?str:60",
            "n": "?int:1..25"})
async def discord_search(bot, query: str, channel: str | None = None,
                         author: str | None = None, n: int = 10) -> str:
    """Search the guild's FULL message history via Discord's own search index.

    This is the long-reach equivalent of channel_history: Discord indexes every
    message the bot can read, so this reaches back years without us storing
    anything. Bounded on purpose — a handful of results, visible-channel only —
    because unbounded historical recall is exactly how a reply ends up citing a
    conversation from another room nobody asked about.
    """
    import httpx
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        return "(no Discord token available for search)"

    guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return "(no guild)"

    # only channels the bot can actually read — never widen visibility
    me = guild.me
    visible = {c.id: c.name for c in guild.text_channels
               if c.permissions_for(me).read_message_history}
    if not visible:
        return "(no readable channels)"

    params = {"content": (query or "").strip()[:300], "limit": max(1, min(int(n), 25)),
              "include_nsfw": "false", "sort_by": "timestamp", "sort_order": "desc"}
    if channel:
        ch = discord_find_channel(guild, channel)
        if ch is not None and ch.id in visible:
            params["channel_id"] = str(ch.id)
        elif ch is not None:
            return f"(can't read #{ch.name})"
    if author:
        member = discord.utils.find(
            lambda m: m.display_name.lower() == author.lower().lstrip("@")
            or m.name.lower() == author.lower().lstrip("@"), guild.members)
        if member is not None:
            params["author_id"] = str(member.id)

    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"https://discord.com/api/v10/guilds/{guild.id}/messages/search",
                            params=params,
                            headers={"Authorization": f"Bot {token}"})
    except Exception as e:
        return f"(search failed: {str(e)[:80]})"

    if r.status_code == 403:
        return "(Discord refused the search — the bot needs access to the channels)"
    if r.status_code != 200:
        return f"(search failed: HTTP {r.status_code})"

    data = r.json()
    groups = data.get("messages") or []
    hits = []
    for group in groups:
        for m in group if isinstance(group, list) else [group]:
            cid = m.get("channel_id")
            if isinstance(cid, str) and cid.isdigit():
                cid = int(cid)
            if cid not in visible:          # belt and braces: never leak a channel
                continue
            content = (m.get("content") or "").strip()
            if not content:
                continue
            hits.append((m.get("timestamp", "")[:16], visible.get(cid, "?"),
                         (m.get("author") or {}).get("username", "?"), content[:200]))
            if len(hits) >= params["limit"]:
                break
        if len(hits) >= params["limit"]:
            break

    if not hits:
        return "(nothing found for that search)"
    lines = [f"- {ts} #{chan} {who}: {text}" for ts, chan, who, text in hits]
    total = data.get("total_results")
    head = f"discord search for {query!r}"
    if total:
        head += f" ({total} matches on record, showing {len(hits)})"
    return head + ":\n" + "\n".join(lines)


@tool(name="member_profile",
      description="everything you know about a member",
      args={"name": "str:60"})
async def member_profile(bot, name: str) -> str:
    entries = await memory.stm_window(hours=24 * 7)
    theirs = [e for e in entries if name.lower() in e["name"].lower()]
    if not theirs:
        return f"(nothing in the archive from anyone called {name})"
    uid = theirs[-1]["user_id"]
    mem = await memory.get_user(uid, theirs[-1]["name"])
    server = await memory.get_server()
    from cogs.companion import _memory_block
    recent = "\n".join(f"- #{e['channel']}: {e['text'][:110]}" for e in theirs[-6:])
    return (f"what you know about {theirs[-1]['name']}:\n"
            f"{_memory_block(mem, server, [])}\n\ntheir recent messages:\n{recent}")


@tool(name="docs_search",
      description="search the bot's own docs (setup, voice troubleshooting)",
      args={"query": "str:300"})
async def docs_search(query: str) -> str:
    try:
        import rag
        results = rag.search(query)
        if not results:
            return "(no doc matches — if it's not in the docs, say so plainly)"
        return "\n".join(f"- {r.get('title', '?')}: {r.get('text', '')[:150]}" for r in results[:4])
    except Exception as e:
        return f"(docs unavailable: {str(e)[:80]})"


# ---------- the loop ----------

def _unwrap_tool_tags(text: str) -> dict | None:
    """Recover a tool call from XML-ish envelopes the model sometimes emits.

    Observed in production: instead of the JSON envelope it was asked for, the
    model replied with
        <tool calls>
          {"tool": "channel_history", "args": {...}}
        </tool calls>
    which we treated as a provider outage and answered with a degraded line.
    The intent is unambiguous, so honour it.
    """
    if "<" not in text:
        return None
    for obj in _json_objects(text):
        if isinstance(obj, dict) and ("tool" in obj or "reply" in obj):
            return obj
    return None


def _json_objects(text: str):
    r"""Yield every top-level JSON object embedded in arbitrary text.

    Needed because the obvious regex (``\{.*?\}``) stops at the first closing
    brace it sees — which, for a nested envelope like
    ``{"tool": "x", "args": {"n": 5}}``, is the *inner* one. Scanning with a
    depth counter is the only way to get the whole object back.
    """
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        yield json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        pass
                    start = None


def parse_envelope(text: str) -> dict | None:
    """Turn a model reply into a tool-loop envelope, tolerating fences and tags."""
    raw = (text or "").strip()
    if not raw:
        return None
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    for candidate in (cleaned, raw):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        # only an envelope counts — a plain JSON blob with no tool/reply key is
        # not something to act on, and returning it would silently blank a reply
        if isinstance(obj, dict) and ("tool" in obj or "reply" in obj):
            return obj
    return _unwrap_tool_tags(cleaned) or _unwrap_tool_tags(raw)


@tool(name="computer",
      description="delegate a task to your computer: bash, files, AND its live desktop "
                  "(open a web page in chromium, click/type with xdotool, screenshot) — "
                  "ADMIN requests only, quick tasks (90s cap)",
      args={"prompt": "str:500"}, tier="admin", timeout=120)
async def computer(prompt: str) -> str:
    if not settings.get_bool("computer.tool_enabled", True,
                            env="COMPUTER_TOOL_ENABLED"):
        return "(computer access disabled)"
    # /workspace only exists inside the sandbox. Running the command
    # anyway made sh answer "can't cd to /workspace", which the model
    # then narrated as a mysterious failure ("my computer tool keeps
    # dying on a cd /workspace") instead of the plain fact: there is
    # no sandbox. Say that, and execute nothing.
    workspace = os.environ.get("COMPUTER_WORKSPACE", "/workspace")
    if not os.path.isdir(workspace):
        return (f"(no sandbox: nothing at {workspace}, so i didn't run "
                "anything. i can only use my computer while my sandbox is up)")
    if not shutil.which("opencode"):
        return ("(no sandbox: `opencode` isn't on PATH here, so i didn't "
                "run anything. i can only use my computer inside my sandbox)")
    import subprocess
    cmd = (f"cd {shlex.quote(workspace)} && timeout 90 opencode run "
           f"{shlex.quote(prompt)}")
    r = await asyncio.get_event_loop().run_in_executor(
        None, lambda: subprocess.run(
            ["bash", "-lc", cmd], capture_output=True, text=True,
            timeout=110))
    out = (r.stdout or "").strip()
    if not out:
        why = (r.stderr or "").strip()[:200] or f"exit {r.returncode}"
        return f"(the sandbox ran nothing for me: {why})"
    return out[:1200]


async def run_tool(bot, name: str, args: dict, member=None,
                   admin: bool = False) -> str:
    """Dispatch one tool call through the registry (schema, tier, timeout,
    audit all live there). Kept as this module's front door because callers
    and tests know it; it is now a delegate, not the source of truth."""
    return await tool_registry.run(bot, name, args, member=member, admin=admin)


async def run_loop(bot, base_system: str, user_prompt, *,
                   max_tokens: int = 400, timeout: float = 30.0,
                   allow_actions: bool = True, content_payload=None,
                   admin: bool = False, member=None) -> dict:
    """The multi-turn loop. Returns the final {"reply": ..., "actions": [...]} (or {}).

    user_prompt: a string, or (for vision) an OpenAI content-part list —
    the images ride on the first user message.
    member: the discord.Member who triggered this, when known — tools gate
        on their tier. admin: shorthand for the caller's tier already being
        admin (used when no Member object rode along).
    admin: enables the admin-tier tools (computer, opencode delegation) for
        this loop — their prompt lines only appear, and their calls only
        run, when the caller is allowed.
    """
    sys_prompt = base_system + tool_registry.tools_prompt(admin=admin) + EVIDENCE_RULE
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt if content_payload is None
                 else content_payload}]
    tool_calls = 0
    for hop in range(MAX_HOPS):
        try:
            raw = await ai.chat_for("reply", messages, json_out=False,
                                    max_tokens=max_tokens, timeout=timeout)
        except AIError:
            raise
        # parse here rather than in ai.chat_for: models occasionally wrap the
        # envelope in tags or prose, and the intent is still obvious
        data = parse_envelope(raw)
        if data is None:
            # not a tool call and not an envelope — treat it as the answer
            text = (raw or "").strip()
            log.info("tool loop got plain text, using it as the reply")
            return {"reply": text, "actions": []}
        if "tool" in data and hop < MAX_HOPS - 1:
            tool_calls += 1
            result = await run_tool(bot, data.get("tool"), data.get("args") or {},
                                    member=member, admin=admin)
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append({"role": "user", "content": f"tool result:\n{result[:1200]}\n\n"
                             "(continue — reply or call another tool)"})
            continue
        # final answer
        reply = str(data.get("reply", "")).strip()
        actions = data.get("actions") or [] if allow_actions else []
        if claimed_without_evidence(reply, tool_calls):
            log.warning("reply claims verification without a tool call: %r", reply[:90])
        return {"reply": reply, "actions": actions}
    return {"reply": "", "actions": []}


# Registration happens on import — and this import sits at the BOTTOM of the
# module on purpose: the prompt lists tools in registration order, so this
# reads "the server tools first, then the free pack". Importing at the top
# would put weather ahead of web_search in every prompt the model sees.
import tools_free  # noqa: E402,F401  (registers weather/time/wiki/news/fx/define)
