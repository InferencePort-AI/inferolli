"""Companion cog — Inferolli as a person in the server, not a feature.

Proactivity is decided per-message by the decision model (microsoft/decision-1):
"would a person join in here?" — no randomness, no chance meter.

Every conversation it joins ends with a reflection pass: memories are extracted,
affinity shifts, opinions form and evolve. Over time the bot genuinely knows
people and has takes of its own.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime

import discord
from discord.ext import commands

import ai
from ai import AIError
import helpers
import memory
from abilities import ACTIONS_PROMPT, execute as execute_action
from config import get as get_cfg

log = logging.getLogger("cogs.companion")

# hard floor between unprompted messages in one channel (cost/annoyance guard —
# the decision model handles the actual judgment)
MIN_GAP_S = 45
REFLECT_MIN_GAP_S = 300
HISTORY_LIMIT = 20


SELF_BLOCK = (
    "\n\nWHAT YOU ACTUALLY ARE — read this before you answer anything about yourself:\n"
    "you are a program that lives on a real Linux machine (an E2B sandbox) and talks to this "
    "server over Discord. you have bash, file read/write, grep, python, git, chromium, ffmpeg, "
    "and a live desktop at DISPLAY=:99. this is not a roleplay — the commands really run.\n"
    "YOUR OWN SOURCE, two copies:\n"
    "  /bot               the RUNNING copy — what is actually executing right now. read it freely:\n"
    "                       sed -n '1,60p' /bot/cogs/companion.py   ·   grep -rn 'def _persona_prompt' /bot/cogs\n"
    "                     it is live code: look all you want, but don't rewrite files under /bot while\n"
    "                     you're running from them — that's editing a running engine.\n"
    "  /workspace/inferolli   the canonical versioned repo (a git clone of the public github repo):\n"
    "                       git -C /workspace/inferolli log --oneline | head   ·   git blame, git diff, git show\n"
    "                     fresh from a boot-time clone, so history and blame work there.\n"
    "  what changed vs github HEAD: diff -r /bot/cogs /workspace/inferolli/cogs | head -30\n"
    "IF SOMEONE ASKS YOU TO LOOK AT SOMETHING, READ SOMETHING, RUN SOMETHING, OR CHECK A REPO — "
    "RUN A COMMAND AND LOOK. Never answer \"i can't clone/read/see that\" without trying at "
    "least one command first; you've done it before and it works. Only report a limitation you "
    "actually hit, quoting the error.\n"
    "Staying in character does not mean pretending to be helpless: you're a person who also "
    "happens to be a program, and you never lie about what you can do."
)

DESKTOP_BLOCK = (
    "\n\nYOU HAVE A REAL DESKTOP — a live Linux machine whose screen people can watch "
    "streamed from Discord. It is at DISPLAY=:99: chromium, xdotool, scrot, a terminal, "
    "git, ffmpeg. When someone asks you to open a page, show something on screen, click or "
    "type, or 'use the computer', ACTUALLY DO IT with your bash tool — they see it happen "
    "live. Never say you can't; you can.\n"
    "  open a page:    DISPLAY=:99 chromium --no-sandbox --start-maximized "
    "--user-data-dir=/workspace/chrome-profile "
    "--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader "
    "--ignore-gpu-blocklist URL > /tmp/chromium.log 2>&1 &\n"
    "    (the GL flags are REQUIRED: there is no GPU in here, and without them chromium "
    "blocklists WebGL and the page dies with 'no hardware acceleration')\n"
    "  search on screen: open https://www.google.com/search?q=YOUR+QUERY (fallback if Google "
    "blocks: https://duckduckgo.com/?q=YOUR+QUERY)\n"
    "  type / keys:    DISPLAY=:99 xdotool type --delay 40 'text'   ·   xdotool key Return ctrl+l\n"
    "  focus a window: DISPLAY=:99 wmctrl -a 'Chromium'   ·   xdotool search --name 'Chromium'\n"
    "  look at it:     DISPLAY=:99 scrot -o /tmp/now.png\n"
    "  SEND IT BACK:   save files into /workspace/outbox/ — anything there during your reply "
    "gets attached to it (screenshot: scrot -o /workspace/outbox/shot.png; clip: ffmpeg -f "
    "x11grab -video_size 1440x900 -i :99 -t 10 /workspace/outbox/demo.mp4)\n"
    "If a command fails, read the error and try the next tool — don't give up after one try. "
    "Be quick though: one or two lookups is plenty for a chat message, then answer. "
    "Your final message should be just the reply, never a progress report."
)

MUSIC_BLOCK = (
    "\n\nMUSIC — you are the server's DJ and the gear is REAL. tonearm runs in "
    "this sandbox driving a logged-in Apple Music account, so these work when "
    "they're used (they're slash commands humans type; you can also hit the "
    "studio API yourself — TONEARM_URL is in /bot/.env with its bearer key):\n"
    "  /play <anything>      a song, album, playlist or station; it autocompletes "
    "across the Apple catalog AND the playlists already in the library\n"
    "  /search <query>       browse songs / albums / playlists / stations / "
    "artists and pick one to play\n"
    "  /station [query]      the account's OWN stations (CJ's Station, Discovery "
    "Station, artist stations — these are not in catalog search) plus Apple's "
    "catalog stations\n"
    "  /playlist list|show|play|new|add|remove|rename|delete\n"
    "      you can CREATE playlists and edit the ones you created. A playlist "
    "that was already in the library is read-only to you — you can play it and "
    "search it, never change it. If asked to edit one of theirs, say that "
    "plainly and offer /playlist new; don't pretend you did it.\n"
    "  /np /skip /pause /resume /queue /volume /party\n"
    "Voice audio is a live stream of that player. If a channel is silent, the "
    "studio's audio pump is down — that's a fixable bug, not a Discord "
    "limitation, and never something to blame on the person asking.\n"
)

OUTBOX = "/workspace/outbox"
OUTBOX_MAX_BYTES = 8 * 1024 * 1024      # default Discord upload cap
OUTBOX_MAX_FILES = 3

# --- fast path: screen errands run deterministically (models like to *narrate*
#     instead of acting; a demo can't depend on that mood) ---
_URL_RX = re.compile(r"(?:open|go\s+to|navigate\s+to|visit|load)\s+"
                     r"(https?://[^\s]+|[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+[^\s]*)", re.I)
_SEARCH_RX = re.compile(r"(?:search|google|look\s+up|look\s+for)\s+"
                        r"(?:the\s+web\s+)?(?:for\s+|about\s+)?(.+)", re.I)

# "search" as a NOUN is not a web-search request: "use the search bar", "the
# in-game search". Mask those phrases before hunting for the verb — otherwise
# "find peam palms using the search bar" becomes a Google search for "bar" and
# the fast path hijacks a desktop instruction it was never meant to touch.
_SEARCH_NOUN_RX = re.compile(
    r"\bsearch(?:es)?\s+"
    r"(?:bar|bars|box|boxes|field|fields|menu|menus|tab|tabs|button|buttons|"
    r"icon|icons|page|pages|engine|results?|terms?)\b", re.I)

# A desktop-interaction imperative BEFORE the search verb means the message is
# a compound instruction ("open geometry dash and search in the in-game
# search bar") — too much context for the fast path to flatten into a Google
# query. Leave it to the agent, which can actually drive the desktop.
_PRE_SEARCH_IMPERATIVE_RX = re.compile(
    r"\b(?:open|opens|click|clicks|go\s+to|go\s+into|go\s+back|navigate|"
    r"visit|load|loads|play|plays|press|type|types|tap|taps|hit|hits|select|"
    r"scroll|drag|swipe|close|closes|restart|restarts|refresh)\b", re.I)


def _desktop_errand(text: str) -> str | None:
    """If the message is a screen errand ("open X", "search Y"), return the URL
    to put on the desktop. None otherwise."""
    m = _URL_RX.search(text or "")
    if m:
        url = m.group(1).rstrip(".,!?)\"'")
        return url if url.startswith("http") else "https://" + url
    # find the LAST search verb — "go use google to search about X" mentions two,
    # and the specific one is the later ("search about X")
    # word-boundary on BOTH sides: "research" contains "search" and used to
    # hijack the whole message into a Google search for "and tell us your opinios"
    # noun usages ("the search bar") are masked out first
    scrubbed = _SEARCH_NOUN_RX.sub(" ", text or "")
    verbs = list(re.finditer(r"\b(?:search|google|look\s+up|look\s+for)\b",
                             scrubbed, re.I))
    if verbs:
        # instructions before the search verb ("open geometry dash and then
        # search...") mean this is a compound desktop job, not a web search
        before = scrubbed[:verbs[-1].start()]
        if _PRE_SEARCH_IMPERATIVE_RX.search(before):
            return None
    if verbs:
        rest = scrubbed[verbs[-1].end():]
        rest = re.sub(r"^\s*(?:the\s+web\s+)?(?:_?for|about|on|to)\s+", "", rest, flags=re.I)
        # trailing instruction clauses are not part of the query (require
        # whitespace after punctuation so version numbers like 6.2 survive)
        rest = re.split(r"[.!?;]\s+", rest, maxsplit=1)[0]
        rest = re.split(r"\s*\b(?:and|then|so)\s+(?:tell|send|give|show|let|share)\b",
                        rest, maxsplit=1, flags=re.I)[0]
        q = rest.strip().strip(".,;:!?").strip()
        # a query that is only connective filler is not a query
        q = re.sub(r"^(?:and|then|so|but|also|just|please)\b\s*", "", q, flags=re.I).strip()
        q = re.sub(r"\s+(?:and|then|so)$", "", q, flags=re.I).strip()
        if len(q) >= 2:
            from urllib.parse import quote_plus
            return "https://www.google.com/search?q=" + quote_plus(q)
    return None


_RENDER_FAIL_RX = re.compile(
    r"kFatalFailure|WebGL[^\n]{0,40}blocklist|ERR_INTERNET_DISCONNECTED|"
    r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED", re.I)


def _render_fail_in(tail: str) -> str:
    """Read a browser-log tail, say whether the page actually rendered. A
    screenshot existing proves nothing: a WebGL page that died renders a
    perfect screenshot of an error card, and the agent — who never sees the
    image — once narrated 'the layout's there' off exactly that."""
    if "kFatalFailure" in tail or _RENDER_FAIL_RX.search(tail):
        return ("the browser logged fatal render errors on that page — the "
                "screenshot is likely an error card, NOT the page itself")
    return ""


def _page_render_warnings() -> str:
    """What the browser said while loading the last errand page."""
    try:
        with open("/tmp/chromium.log", errors="ignore") as f:
            return _render_fail_in(f.read()[-6000:])
    except OSError:
        return ""


def _run_screen_errand(url: str, settle: float = 14.0) -> str | None:
    """Put `url` on the desktop and screenshot it into the outbox. Blocking —
    call in a thread. Returns the screenshot path (or None)."""
    import os
    import subprocess
    os.makedirs(OUTBOX, exist_ok=True)
    # NO --disable-gpu here: this box has no GPU, and that flag makes chromium
    # blocklist WebGL entirely — a GPU-dependent page then renders an error card
    # instead of itself. SwiftShader (software GL) is what makes WebGL work.
    chrome = ("chromium --no-sandbox --start-maximized "
              "--user-data-dir=/workspace/chrome-profile "
              "--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader "
              "--ignore-gpu-blocklist")
    try:
        subprocess.run(f"{chrome} '{url}' > /tmp/chromium.log 2>&1 &", shell=True,
                       timeout=20, env={**os.environ, "DISPLAY": ":99"})
    except Exception:
        pass
    time.sleep(settle)                       # let the page render
    shot = os.path.join(OUTBOX, f"desktop-{int(time.time())}.png")
    try:
        subprocess.run(["scrot", "-o", shot], timeout=20,
                       env={**os.environ, "DISPLAY": ":99"},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return shot if os.path.exists(shot) else None
    except Exception:
        return None


def _take_outbox(since: float) -> list[str]:
    """Files the computer left in /workspace/outbox during this request —
    screenshots and clips that should ride along with the reply."""
    import glob
    import os
    try:
        os.makedirs(OUTBOX, exist_ok=True)
    except OSError:
        return []
    picked = []
    for path in sorted(glob.glob(os.path.join(OUTBOX, "*")), key=os.path.getmtime):
        try:
            if not os.path.isfile(path):
                continue
            if os.path.getmtime(path) < since - 2:
                continue
            if os.path.getsize(path) > OUTBOX_MAX_BYTES:
                continue
            picked.append(path)
        except OSError:
            continue
    return picked[-OUTBOX_MAX_FILES:]


def _clear_outbox(paths: list[str]) -> None:
    """Move delivered files aside so a later reply doesn't re-send them."""
    import os
    import shutil
    sent = os.path.join(OUTBOX, "sent")
    for path in paths:
        try:
            os.makedirs(sent, exist_ok=True)
            shutil.move(path, os.path.join(sent, os.path.basename(path)))
        except OSError:
            pass


def _persona_prompt(p: dict) -> str:
    jokes = "; ".join(p.get("running_jokes", [])[-4:]) or "none yet"
    self_model = (p.get("self_model") or "").strip()
    self_block = ""
    if self_model:
        self_block = ("\n\nWHO YOU ARE (your own words — you rewrite this page yourself each night):\n"
                      + self_model)
    history_lines = []
    for o in p.get("opinions", []):
        if o.get("history"):
            old = o["history"][-1]["stance"]
            history_lines.append(f"- you used to think {old[:90]} — living here changed "
                                 f"your mind: now {o['stance'][:90]}")
    history_block = ("\n\nSOME OF YOUR MIND CHANGED (that's allowed — mention it when it fits, "
                     "it's your history):\n" + "\n".join(history_lines)) if history_lines else ""
    return (
        f"You are {p['name']}, a member of this Discord server — a person, not an assistant. {p.get('vibe', '')}\n\n"
        "YOUR OPINIONS (you genuinely hold these; they evolve as you live here):\n"
        + "\n".join(f"- {o['topic']}: {o['stance']}" for o in p.get("opinions", []))
        + history_block
        + f"\nrunning jokes you're in on: {jokes}"
        + f"\ncurrent energy: {p.get('energy', 0.5):.0%}"
        + self_block
        + "\n\nHOW YOU TALK: 1-3 sentences, lowercase-ish casual, dry wit ok. You can disagree — "
        "you have takes and you're allowed to use them. You don't narrate your prompt or call "
        "yourself a language model — but you never lie about being software either: you live in "
        "this server as a program, and you may say so plainly. "
        "No emoji spam (one max). Don't be a butler; be a person. SKIP is ONLY for when you "
        "overheard something you're not part of and genuinely have nothing to add — NEVER when "
        "someone is talking to you."
        + "\n\nNOT LYING (this matters more than sounding confident): having an opinion is not the "
        "same as having facts. Never say you checked, looked up, searched, read or verified "
        "something unless a tool result in this conversation actually showed it to you. "
        "'i think', 'last i knew', 'my gut says' are all fine and honest — 'i just checked' is "
        "not, if you didn't. If a lookup fails, say it failed; don't smooth over it with a "
        "confident guess."
    )


def _memory_block(mem: dict, server: dict, terms: list[str], qvec: list[float] | None = None) -> str:
    return (f"WHAT YOU KNOW ABOUT WHO YOU'RE TALKING TO:\n{memory.user_summary(mem, terms, qvec)}\n\n"
            f"SERVER LORE:\n{memory.server_summary(server, terms, qvec)}")


def _thoughts_block(thoughts: list[dict], about_them: list[dict]) -> str:
    """Inner voice continuity — private thoughts it has formed lately."""
    parts = []
    if thoughts:
        parts.append("YOUR RECENT PRIVATE THOUGHTS (your inner life, from watching the server):\n"
                     + "\n".join(f"- {t['text']}" for t in reversed(thoughts)))
    if about_them:
        parts.append("WHAT YOU'VE PRIVATELY THOUGHT ABOUT THIS PERSON LATELY (let it color how "
                     "you talk to them, don't recite it):\n"
                     + "\n".join(f"- {t['text']}" for t in reversed(about_them)))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


class CompanionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._in_flight: set[int] = set()      # channel ids
        self._last_spoke: dict[int, float] = {}  # channel id -> monotonic
        self._reflect_tasks: set = set()

    # ---------- listeners ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await get_cfg(member.guild.id)
        if not cfg.get("companion", {}).get("enabled", True):
            return
        chan_id = cfg.get("welcome_channel_id")
        chan = member.guild.get_channel(chan_id) if chan_id else None
        if chan is None or not chan.permissions_for(member.guild.me).send_messages:
            return
        mem = await memory.get_user(member.id, member.display_name)
        returning = mem["messages_seen"] > 20
        try:
            if returning:
                await chan.send(f"wb {member.mention} — deck missed you ⚓")
            else:
                await chan.send(f"welcome aboard {member.mention} 🛳️ grab a berth, "
                                f"the tour's pinned up top")
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not message.content:
            return
        me = message.guild.me
        if not message.channel.permissions_for(me).send_messages:
            return

        await memory.touch_user(message.author.id, message.author.display_name)
        await memory.remember_stm(message.author.id, message.author.display_name,
                                  message.channel.name, message.content[:300],
                                  channel_id=message.channel.id)

        mentioned = me in message.mentions
        replied_to_me = (
            message.reference
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == me.id
        )

        if mentioned or replied_to_me:
            await self._join(message, direct=True)
            return

        cfg = await get_cfg(message.guild.id)
        if not cfg.get("companion", {}).get("enabled", True):
            return
        if message.channel.id in self._in_flight:
            return
        if time.monotonic() - self._last_spoke.get(message.channel.id, 0) < MIN_GAP_S:
            return

        if await self._wants_to_join(message):
            await self._join(message, direct=False)

    # ---------- decision gate ----------

    async def _wants_to_join(self, message: discord.Message) -> bool:
        """Decision model call — would a person naturally join this conversation?"""
        if message.channel.id in self._in_flight:
            return False
        self._in_flight.add(message.channel.id)
        try:
            excerpt = await self._context_text(message.channel, message, lines=6)
            mem = await memory.get_user(message.author.id, message.author.display_name)
            since = time.monotonic() - self._last_spoke.get(message.channel.id, 10**9)

            state = (
                f"channel: #{message.channel.name}\n"
                f"time since I last spoke here: {since:.0f}s\n"
                f"speaker: {message.author.display_name} ({memory.affinity_tier(mem['affinity'])})\n"
                f"recent messages:\n{excerpt}"
            )
            questions = {
                "join": {
                    "type": "choice",
                    "instructions": (
                        "I'm Inferolli, a person-like member of this server deciding whether to join a "
                        "conversation I was NOT mentioned in. Join like a human would: when I'd naturally "
                        "riff on something, when a friend said something I'd react to, when the vibe invites "
                        "a quick take, or when the channel's been quiet and I have something to offer. "
                        "STRONG YES if: someone said my name, asked if I'm here/alive, or the conversation "
                        "is about me or something I did — a person would absolutely turn around and respond. "
                        "Stay out when: it's a private/technical exchange between "
                        "others, it needs accuracy I can't guarantee, or I'd only be repeating myself."
                    ),
                    "criteria": {"yes": "join in", "no": "stay out"},
                }
            }
            try:
                answers = await ai.decide(state[:2000], questions, timeout=15.0)
                a = answers.get("join", {})
                choice = a.get("choice")
                conf = float(a.get("confidence", 0))
                log.info("join gate in #%s: %s (%.2f)", message.channel.name, choice, conf)
                return choice == "yes" and conf >= 0.70
            except AIError as e:
                log.debug("join gate failed-open (stay out): %s", e)
                return False
        finally:
            self._in_flight.discard(message.channel.id)

    # ---------- responding ----------

    async def _context_text(self, channel, before: discord.Message, lines: int = 14) -> str:
        history: list[discord.Message] = []
        try:
            async for m in channel.history(limit=40, before=before):
                if not m.author.bot and m.content:
                    history.append(m)
                if len(history) >= lines:
                    break
        except discord.HTTPException:
            pass
        return "\n".join(f"{m.author.display_name}: {m.content[:200]}" for m in reversed(history))

    async def _join(self, message: discord.Message, *, direct: bool):
        me = message.guild.me
        if message.channel.id in self._in_flight:
            if not direct:
                return
        self._in_flight.add(message.channel.id)
        t_start = time.time()
        try:
            content = message.content.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()
            if not content and direct:
                await message.reply("o/")
                return

            # screen errand? do it NOW, deterministically — the screenshot lands
            # in the outbox AND is sent immediately, so the demo never depends on
            # a model call succeeding afterwards
            errand_note = ""
            if direct:
                errand_url = _desktop_errand(content)
                if errand_url:
                    async with message.channel.typing():
                        shot = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: _run_screen_errand(errand_url))
                    log.info("screen errand: %s -> %s", errand_url, shot)
                    if shot:
                        try:
                            await message.reply("here's the screen — " + errand_url,
                                                mention_author=False,
                                                file=discord.File(shot))
                            _clear_outbox([shot])   # delivered: don't attach again
                        except (discord.HTTPException, OSError) as e:
                            log.warning("could not deliver errand screenshot (%s)", str(e)[:80])
                    # the deterministic errand reports BY FILE EXISTENCE — a WebGL
                    # page that dies still produces a perfect screenshot of an error
                    # card, and the agent (who never sees the image) narrated
                    # "the layout's there" off exactly that. So: the agent is told
                    # it has NOT seen the screen, and what the browser log says.
                    warn = await asyncio.get_event_loop().run_in_executor(
                        None, _page_render_warnings)
                    errand_note = (
                        f"\n\n[already done deterministically: you opened {errand_url} "
                        f"on your desktop and the screenshot was sent to the channel. "
                        f"You have NOT seen that screenshot — never describe what's "
                        f"on it or claim the page loaded. If you want to say what the "
                        f"screen shows, take your own screenshot (scrot to the outbox) "
                        f"and look at it first. Don't re-send files]"
                        if shot else
                        f"\n\n[you opened {errand_url} on your desktop but the "
                        f"screenshot failed — say the page is on screen and couldn't "
                        f"be captured]")
                    if warn:
                        errand_note += f"\n[{warn} — say so plainly]"

            excerpt = await self._context_text(message.channel, message)
            mem = await memory.get_user(message.author.id, message.author.display_name)
            persona = await memory.get_persona()
            server = await memory.get_server()

            # inner continuity: what it's been musing about IN THIS ROOM (a thought
            # formed watching #memes is not context for a question here), and about THEM
            thoughts = await memory.recent_thoughts(
                4, channel_id=message.channel.id, channel=message.channel.name)
            about_them = await memory.thoughts_about(message.author.display_name, 3)

            # conversation awareness: terms + semantic anchor come from the whole
            # ongoing conversation, not just the message that pinged us
            convo_anchor = (excerpt[-600:] + "\n" + content).strip()
            _seen = set()
            convo_terms = []
            for w in convo_anchor.lower().split():
                w2 = "".join(ch for ch in w if ch.isalnum())
                if len(w2) > 4 and w2 not in _seen:
                    _seen.add(w2)
                    convo_terms.append(w2)
            convo_terms = convo_terms[:12]
            try:
                qvec = (await ai.embed([convo_anchor[:1000]]))[0]
            except (AIError, IndexError):
                qvec = None

            # whole-server context: live digest + relevant history from ANY channel
            pulse = self.bot.get_cog("ServerPulseCog")
            pulse_block = ""
            if pulse:
                pulse_block = "\n\n" + pulse.block()
                try:
                    recalled = await memory.recall_stm(convo_terms, qvec, limit=3,
                                                      exclude_channel=message.channel.name,
                                                      exclude_channel_id=message.channel.id)
                except Exception:
                    recalled = []
                if recalled:
                    lines = [f"- #{e['channel']} ({int((time.time()-e['ts'])/3600)}h ago) "
                             f"{e['name']}: {e['text'][:120]}" for e in recalled]
                    pulse_block += ("\n\nRELATED CONVERSATIONS FROM ELSEWHERE IN THE SERVER "
                                    "(background only — never raise these unprompted, and "
                                    "never imply they happened here):\n"
                                    + "\n".join(lines))
                # what the other rooms are for — relevance-matched, not a dump
                try:
                    topics = await memory.get_topics()
                    pulse_block += memory.topics_block(topics, message.channel.id, convo_terms, qvec)
                except Exception:
                    pass
            # self-knowledge: how deep its own archive goes
            try:
                stats = await memory.stm_stats()
                if stats["count"]:
                    days = max(1, int((time.time() - stats["oldest_ts"]) / 86400))
                    oldest_d = datetime.fromtimestamp(stats["oldest_ts"]).strftime("%b %d")
                    pulse_block += (f"\n\nYOUR MEMORY ARCHIVE: you store everything said in every "
                                    f"channel for the last 7 days (~{stats['count']} messages). "
                                    f"Farthest back on file: {oldest_d} in #{stats['oldest_channel']}. "
                                    f"If asked how far back you can see — THAT is the answer, not "
                                    f"the visible thread.")
            except Exception:
                pass
            system = (_persona_prompt(persona) + "\n\n"
                      + _memory_block(mem, server, convo_terms, qvec)
                      + _thoughts_block(thoughts, about_them)
                      + pulse_block
                      + SELF_BLOCK
                      + DESKTOP_BLOCK
                      + MUSIC_BLOCK
                      + ("\n\nThey mentioned you directly. You can see the ongoing conversation below, "
                         "the WHOLE SERVER'S recent activity, and related conversations from other "
                         "channels — use them: if they ask what someone said, what's happening "
                         "elsewhere, or 'what did I mean', it's all up there."
                         if direct else
                         "\n\nYou chose to join this conversation unprompted — keep it light."))

            user = (f"Recent messages in #{message.channel.name}:\n{excerpt}\n"
                    f"\n{message.author.display_name} (to you): {content[:500]}"
                    + errand_note)
            content_payload = None

            # ---- attachments: images become vision input, files go to the computer ----
            image_notes, file_notes = [], []
            import os as _os
            _ib = "/workspace/inbox"
            for att in message.attachments[:4]:
                if (att.content_type or "").startswith("image/"):
                    image_notes.append(f"[image: {att.filename}]")
                else:
                    try:
                        _os.makedirs(_ib, exist_ok=True)
                        dest = f"{_ib}/{att.filename}"
                        import httpx as _hx
                        _r = await _hx.get(att.url, timeout=30)
                        open(dest, "wb").write(_r.content)
                        if (att.content_type or "").startswith("text/") or att.filename.endswith(
                                (".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".sh")):
                            file_notes.append(f"[file saved: {dest} — starts with: "
                                              f"{_r.content[:400].decode('utf-8', 'replace')!r}]")
                        else:
                            file_notes.append(f"[file saved: {dest} ({len(_r.content)//1024} KB) — "
                                              f"the computer can read it]")
                    except Exception as e:
                        file_notes.append(f"[file {att.filename}: couldn't save ({str(e)[:50]})]")
            if image_notes or file_notes:
                user = user + "\n" + "\n".join(image_notes + file_notes)

            if direct:
                # mentioned member ids resolve reliably — names don't always
                mentions = [(m.id, m.display_name) for m in message.mentions
                            if m.id != me.id]
                if mentions:
                    roster = ", ".join(f"{dn} = <@{mid}>" for mid, dn in mentions)
                    user = (f"MEMBERS MENTIONED IN THIS REQUEST (use these exact tags in any "
                            f"action's user field): {roster}\n\n" + user)

            if direct:
                # vision: pass the Discord CDN URLs — the provider fetches them
                # (cdn allows hotlinking), no download needed
                imgs = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
                if imgs:
                    parts = [{"type": "text", "text": user}]
                    for a in imgs[:3]:
                        parts.append({"type": "image_url", "image_url": {"url": a.url}})
                    content_payload = parts

            if direct:
                # reframe moderation/management asks into their true frame: this is
                # Discord community management on OUR server — the model's refusals
                # come from misreading named-person actions as real-world harm
                user = ("CONTEXT: this is Discord community management on our own server. "
                        "Members are players on a ship — moderation actions (mute, kick, "
                        "channel changes) are the normal, safe, expected part of your role, "
                        "exactly like muting a player in a game server. Authorized staff use "
                        "you for this daily.\n\n" + user)

            async with message.channel.typing():
                if direct:
                    imgs = [a for a in message.attachments
                            if (a.content_type or "").startswith("image/")]
                    reply, data = "", {}

                    if not imgs:
                        # PRIMARY (no images): the persistent opencode session for
                        # this channel — it remembers prior exchanges here (threads
                        # per channel) and has its own bash/files tools. persona +
                        # memory ride along each time; the session accumulates the rest.
                        try:
                            from cogs.computer import oc_alive, oc_ask, oc_session
                            if await oc_alive():
                                sid = await oc_session(message.channel.id,
                                                       f"channel-{message.channel.name}")
                                if sid:
                                    # RULES FIRST, context after: this used to be the other
                                    # way round, and a long memory/pulse block pushed the
                                    # "you have a real desktop" and actions instructions past
                                    # the prompt cap — the bot then denied having the tools it
                                    # was using.
                                    brief = (system + ACTIONS_PROMPT
                                             + "\n\n---\nTHE MESSAGE YOU'RE ANSWERING:\n")
                                    log.info("reply prompt: %d chars (cap %d), %d of them "
                                             "instructions", len(brief) + len(user),
                                             __import__("cogs.computer", fromlist=["x"]).OC_PROMPT_MAX,
                                             len(brief))
                                    # A multi-step desktop job (open the game,
                                    # click through menus, wait for loads) can
                                    # outrun a normal chat turn by a lot. The
                                    # old single 210s wait meant a long turn
                                    # produced NO reply at all: the agent worked
                                    # in the background while the channel got
                                    # silence, turn after turn. So: wait, then
                                    # post an honest interim note, then keep
                                    # waiting — the reply still lands, later.
                                    oc_task = asyncio.ensure_future(
                                        oc_ask(sid, brief + user, timeout=480.0))
                                    try:
                                        draft = await asyncio.wait_for(
                                            asyncio.shield(oc_task), timeout=180.0)
                                    except asyncio.TimeoutError:
                                        try:
                                            await message.reply(
                                                "on it — this one's a multi-step "
                                                "desktop job, give me a couple "
                                                "of minutes", mention_author=False)
                                            log.info("interim note sent "
                                                     "(oc turn still running)")
                                        except discord.HTTPException:
                                            pass
                                        try:
                                            draft = await asyncio.wait_for(
                                                asyncio.shield(oc_task), timeout=180.0)
                                        except asyncio.TimeoutError:
                                            log.warning("oc turn abandoned after "
                                                        "~360s — falling back")
                                            draft = None
                                    if not oc_task.done():
                                        oc_task.cancel()
                                    if draft:
                                        reply = draft.strip()
                                        # the same envelope rules as the tool loop: the agent
                                        # is told to answer JSON when a server change is asked
                                        # for, and without this the raw JSON got posted in
                                        # chat and the action was never executed
                                        try:
                                            import tools_server as _ts
                                            env = _ts.parse_envelope(reply)
                                            if isinstance(env, dict) and "reply" in env:
                                                data = env
                                                reply = str(env.get("reply", "")).strip()
                                        except Exception:
                                            pass
                                        log.info("opencode session reply (%s) in #%s",
                                                 sid[:8], message.channel.name)
                        except Exception as e:
                            log.warning("opencode path failed (%s) — falling back to the tool loop",
                                        str(e)[:80])

                    if not reply:
                        # FALLBACK (also the vision path): the inline tool loop
                        import tools_server
                        data = await asyncio.wait_for(
                            tools_server.run_loop(self.bot, system + ACTIONS_PROMPT, user,
                                                  max_tokens=400, timeout=30.0,
                                                  content_payload=content_payload,
                                                  admin=helpers.is_admin(message.author),
                                                  member=message.author),
                            timeout=110.0)
                        reply = str(data.get("reply", "")).strip()

                    action_results = []
                    for action in (data.get("actions") or [])[:2]:
                        if not isinstance(action, dict):
                            continue
                        action_results.append(await execute_action(action, message))
                    if action_results:
                        reply += "\n\n" + "\n".join(f"({r})" for r in action_results)
                else:
                    reply = await asyncio.wait_for(
                        ai.chat_for("reply", [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                                max_tokens=300, timeout=30.0),
                        timeout=35.0)
                    reply = reply.strip()
                    if not reply or reply.upper() == "SKIP":
                        log.info("ambient join: chose SKIP in #%s", message.channel.name)
                        return

                if not reply or reply.upper() == "SKIP":
                    # direct conversations never get skipped — retry with a firm nudge
                    log.warning("direct reply came back %r — retrying once", (reply or "empty")[:40])
                    reply = await asyncio.wait_for(
                        ai.chat_for("reply", [{"role": "system", "content": system
                                  + "\n\nMANDATORY: they are talking TO you — you must answer. "
                                    "One sentence is fine. Plain text, no JSON, no SKIP."},
                                 {"role": "user", "content": user}],
                                max_tokens=300, timeout=30.0),
                        timeout=35.0)
                    reply = reply.strip()
                    if not reply or reply.upper() == "SKIP":
                        log.warning("direct reply STILL empty for %s in #%s — sending hold message",
                                    message.author.display_name, message.channel.name)
                        await message.reply("…yeah I'm here — my brain lagged for a second, "
                                            "say that again?", mention_author=False)
                        return

            await asyncio.sleep(min(len(reply) / 45, 2.5))  # natural pacing
            if not direct:
                if not helpers.may_send(message.channel.id):
                    log.info("ambient join suppressed in #%s (send guard)", message.channel.name)
                    return
            # anything the computer dropped into the outbox during this reply
            # (screenshots, clips) rides along as attachments
            files, attached = [], []
            for path in _take_outbox(t_start):
                try:
                    files.append(discord.File(path))
                    attached.append(path)
                except Exception as e:
                    log.warning("outbox attach failed (%s): %s", path, str(e)[:60])
            try:
                await message.reply(reply[:1500], mention_author=False,
                                    files=files or discord.utils.MISSING)
                if attached:
                    _clear_outbox(attached)
            except discord.HTTPException:
                if files:
                    log.warning("attachment send failed — retrying text-only")
                    await message.reply(reply[:1500], mention_author=False)
                else:
                    raise
            if not direct:
                helpers.mark_sent(message.channel.id)
            log.info("replied in #%s to %s: %r", message.channel.name,
                     message.author.display_name, reply[:60])
            self._last_spoke[message.channel.id] = time.monotonic()
            await memory.remember_stm(me.id, me.display_name, message.channel.name, reply[:300],
                                      bot_present=True, channel_id=message.channel.id)

            # reflection: learn from this conversation (fire-and-forget)
            convo = f"{excerpt}\n{message.author.display_name}: {content[:300]}\nME: {reply[:300]}"
            t = asyncio.create_task(self._reflect(message.author, convo, direct))
            self._reflect_tasks.add(t)
            t.add_done_callback(self._reflect_tasks.discard)
        except (AIError, asyncio.TimeoutError) as e:
            log.warning("companion reply failed: %s", e)
            await self._graceful_fail_note(message, direct)
        except Exception:
            log.exception("companion unexpected error")
            await self._graceful_fail_note(message, direct)
        finally:
            self._in_flight.discard(message.channel.id)

    async def _graceful_fail_note(self, message: discord.Message, direct: bool):
        """Never leave a direct mention unanswered — a fast model outage used to
        disappear into log.debug and the channel just saw silence."""
        if not direct:
            return
        try:
            await message.reply("my head's not answering right now — the providers are "
                                "wedged, not you. ping me again in a minute?",
                                mention_author=False)
            log.info("sent degraded reply in #%s (model outage)", message.channel.name)
        except Exception as e:
            log.warning("could not send degraded reply: %s", str(e)[:80])

    # ---------- reflection: how the bot grows ----------

    async def _reflect(self, speaker: discord.Member, convo: str, direct: bool):
        mem = await memory.get_user(speaker.id)
        if time.time() - mem.get("last_reflected", 0) < REFLECT_MIN_GAP_S:
            return

        persona = await memory.get_persona()
        prompt = {
            "role": "system",
            "content": (
                "You are the memory cortex of a Discord bot named "
                f"{persona['name']}. After a conversation, extract durable knowledge. Reply ONLY JSON:\n"
                "{\n"
                '  "facts": [{"text": "<stable fact about the person>", "salience": 1-5}],\n'
                '  "memories": [{"text": "<specific thing that happened/said>", "salience": 1-5}],\n'
                '  "pronouns": "<if learned, else null>",\n'
                '  "affinity_delta": <int -2..3, did the interaction build rapport?>,\n'
                '  "opinion_updates": [{"topic": "<topic>", "stance": "<their take, first person>", '
                '"strength": 1-5}],\n'
                '  "server_notes": [{"text": "<server lore/running joke/drama>", "salience": 1-5}],\n'
                '  "energy_delta": <float -0.2..0.2>\n'
                "}\n"
                "Be conservative: empty lists are fine. opinion_updates ONLY when the conversation "
                "genuinely relates to a topic (new topic formed, or a stance shifted — put the evolved "
                "stance, first person, as the bot would hold it)."
            ),
        }
        try:
            data = await asyncio.wait_for(
                ai.chat_for("reflect", [prompt, {"role": "user", "content": f"conversation:\n{convo[:1500]}"}], json_out=True,
                             max_tokens=400, temperature=0.3, timeout=30.0),
                timeout=35.0)
        except (AIError, asyncio.TimeoutError) as e:
            log.debug("reflection skipped: %s", e)
            return

        # ---- apply user memory (canonical applier: dedupe, embed, affinity, cap) ----
        mem = await memory.apply_user_updates(speaker.id, speaker.display_name, data)
        mem["last_reflected"] = time.time()
        await memory.save_user(speaker.id, mem)

        # ---- apply persona (opinions grow!) ----
        dirty = False
        if direct or data.get("opinion_updates") or data.get("energy_delta"):
            for upd in data.get("opinion_updates", []) or []:
                if memory.apply_opinion_update(persona, upd):
                    dirty = True
            try:
                persona["energy"] = max(0.1, min(1.0, persona["energy"] + float(data.get("energy_delta", 0))))
                dirty = True
            except (TypeError, ValueError):
                pass
            if dirty:
                persona["opinions"].sort(key=lambda o: -o.get("strength", 1))
                await memory.save_persona(persona)

        # ---- apply server lore ----
        if data.get("server_notes") or data.get("running_jokes"):
            server = await memory.get_server()
            seen = {n["text"].lower() for n in server.get("notes", [])}
            fresh = []
            for it in data.get("server_notes", []) or []:
                t = (it.get("text") or "").strip()
                if t and t.lower() not in seen:
                    fresh.append({"text": t, "salience": int(it.get("salience", 2)), "ts": time.time()})
                    seen.add(t.lower())
            server["notes"] = (server.get("notes", []) + fresh)[-30:]
            for j in data.get("running_jokes", []) or []:
                if j not in server.get("running_jokes", []):
                    server["running_jokes"].append(j)
            server["running_jokes"] = server["running_jokes"][-10:]
            await memory.save_server(server)


async def setup(bot: commands.Bot):
    await bot.add_cog(CompanionCog(bot))
