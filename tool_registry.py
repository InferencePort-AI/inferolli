"""tool_registry — one declarative entry per capability.

The old tool surface was seven `if name ==` branches in tools_server.run_tool
plus a hand-maintained TOOLS_PROMPT string. Two places that must agree never
do for long, and the failure mode is the one thing this bot may never do:
the persona advertising a capability that doesn't exist.

Here, the registry is the single source of truth. The prompt shown to the model
is *generated* from the registered set, the dispatcher routes through it, and
a test asserts the two can never drift. Adding a tool is one decorator:

    @tool(name="weather", description="conditions for a place",
          args={"place": "str:80", "days": "?int:1..7"},
          tier="member", cost="free", timeout=10)
    async def weather(bot, place, days=None): ...

Schema strings:
    "str:80"        required string, clamped to 80 chars ("str" = 500)
    "int:1..7"      required int, clamped/bounded      ("int" = 0..1_000_000)
    "choice:a|b"    required, must be one of the options
    "?<spec>"       optional — absent means the handler default applies

Handlers are coroutines. They receive `bot` if (and only if) they declare a
`bot` parameter, and each schema arg by name — so existing handlers with their
own signatures migrate unchanged.

Tiers use permissions.py, resolved per guild through config.json
(`permissions.tool_tiers` overrides a spec's default tier). `audit=True` tools
write a mod-log record per call, like abilities does for server changes.
"""

import asyncio
import inspect
import logging
from dataclasses import dataclass, field

import discord

import permissions

log = logging.getLogger("tools")

TIERS = permissions.TIERS

_MAX_STR = 500
_MAX_INT = 1_000_000


class RegistryError(Exception):
    """Raised at import time for a malformed registration."""


@dataclass
class ToolSpec:
    name: str
    description: str
    args: dict
    handler: object = None
    tier: str = "member"
    audit: bool = False
    timeout: float = 20.0
    cost: str = "free"
    line: str = ""          # prompt line override; generated when empty


_REGISTRY: dict[str, ToolSpec] = {}
_ORDER: list[str] = []


def tool(*, name: str, description: str, args: dict, tier: str = "member",
         audit: bool = False, timeout: float = 20.0, cost: str = "free",
         line: str = ""):
    """Decorator: register a handler. Import of the module is the registration."""
    def deco(fn):
        if not inspect.iscoroutinefunction(fn):
            raise RegistryError(f"tool {name!r}: handler must be a coroutine function")
        if name in _REGISTRY:
            raise RegistryError(f"tool {name!r}: already registered")
        if tier not in TIERS:
            raise RegistryError(f"tool {name!r}: unknown tier {tier!r}")
        spec = ToolSpec(name=name, description=description, args=dict(args or {}),
                        handler=fn, tier=tier, audit=audit, timeout=timeout,
                        cost=cost, line=line)
        for arg, rule in spec.args.items():
            _parse_rule(spec.name, arg, rule)      # fail at import, not at call
        _REGISTRY[name] = spec
        _ORDER.append(name)
        return fn
    return deco


# ---------- schema ----------

def _parse_rule(tool_name, arg, rule):
    """Validate a schema rule at registration time and return its pieces."""
    rule = str(rule)
    optional = rule.startswith("?")
    rule = rule.lstrip("?").strip()
    if rule.startswith("str"):
        spec = rule.split(":", 1)
        limit = int(spec[1]) if len(spec) > 1 and spec[1].isdigit() else _MAX_STR
        return ("str", limit, optional)
    if rule.startswith("int"):
        bounds = (0, _MAX_INT)
        if ":" in rule:
            try:
                lo, hi = rule.split(":", 1)[1].split("..")
                bounds = (int(lo), int(hi))
            except ValueError:
                raise RegistryError(f"tool {tool_name!r}: bad int rule {rule!r}")
        return ("int", bounds, optional)
    if rule.startswith("choice:"):
        options = tuple(o.strip() for o in rule[len("choice:"):].split("|") if o.strip())
        if not options:
            raise RegistryError(f"tool {tool_name!r}: choice with no options")
        return ("choice", options, optional)
    if rule.startswith("bool"):
        return ("bool", None, optional)
    raise RegistryError(f"tool {tool_name!r}: unknown arg rule {rule!r} (arg {arg!r})")


def coerce(spec: ToolSpec, args: dict) -> tuple[dict, str | None]:
    """Validate + clamp raw args against the spec. (kwargs, error)."""
    args = args if isinstance(args, dict) else {}
    out = {}
    for arg, rule in spec.args.items():
        kind, lim, optional = _parse_rule(spec.name, arg, rule)
        raw = args.get(arg)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if optional:
                continue
            return {}, f"missing required arg '{arg}'"
        if kind == "str":
            out[arg] = str(raw)[:lim]
        elif kind == "int":
            try:
                out[arg] = max(lim[0], min(lim[1], int(float(raw))))
            except (TypeError, ValueError):
                return {}, f"arg '{arg}' must be a whole number"
        elif kind == "choice":
            val = str(raw).strip()
            if val not in lim:
                return {}, f"arg '{arg}' must be one of: {'/'.join(lim)}"
            out[arg] = val
        elif kind == "bool":
            out[arg] = str(raw).strip().lower() in ("1", "true", "yes", "on")
    return out, None


# ---------- registry views ----------

def names() -> list[str]:
    return list(_ORDER)


def specs() -> list[ToolSpec]:
    return [_REGISTRY[n] for n in _ORDER]


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get((name or "").strip())


# ---------- dispatch ----------

def required_tier(spec: ToolSpec) -> str:
    """config.json `permissions.tool_tiers` overrides the spec default."""
    return permissions.tool_tier(spec.name, spec.tier)


def allows(spec: ToolSpec, member=None, admin: bool = False) -> bool:
    """Gate one spec against a caller. A real Member wins; the tool loop's
    admin flag (the caller's tier was already checked there) is the fallback
    when no Member object rode along with the request."""
    if member is not None:
        held = permissions.tier_of(member)
    else:
        held = "admin" if admin else "member"
    return permissions.rank(held) >= permissions.rank(required_tier(spec))


async def run(bot, name: str, args: dict, member=None, admin: bool = False) -> str:
    """Route one tool call. Always returns text for the model, never raises.

    Failures are honest sentences — the model must relay them, not paper over
    them, and nothing here gives it a chance to invent a result.
    """
    spec = get(name)
    if spec is None:
        return f"(unknown tool: {name})"
    if not allows(spec, member=member, admin=admin):
        need = required_tier(spec)
        held = permissions.tier_of(member) if member is not None else ("admin" if admin else "member")
        return (f"(tool {spec.name}: that one needs the {need} tier — you're {held}, "
                "so i can't run it for you)")
    kwargs, err = coerce(spec, args or {})
    if err:
        return f"(tool {spec.name} needs different args: {err})"
    fn = spec.handler
    wants_bot = "bot" in inspect.signature(fn).parameters
    call = fn(bot, **kwargs) if wants_bot else fn(**kwargs)
    try:
        result = await asyncio.wait_for(asyncio.ensure_future(call), timeout=spec.timeout)
    except asyncio.TimeoutError:
        return f"(tool {spec.name} timed out after {spec.timeout:.0f}s)"
    except Exception as e:
        log.warning("tool %s failed: %s", spec.name, str(e)[:120])
        return f"(tool {spec.name} failed: {str(e)[:80]})"
    if spec.audit:
        asyncio.ensure_future(_audit(member, spec, kwargs, ok=True, result=result))
    text = str(result or "").strip()
    return text or f"(tool {spec.name} returned nothing)"


async def _audit(member, spec: ToolSpec, kwargs: dict, *, ok: bool, result=None):
    """Mod-log one audited tool call, fire-and-forget. Never raises."""
    try:
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        from config import get as get_cfg
        cfg = await get_cfg(guild.id)
        chan_id = cfg.get("modlog_channel_id")
        chan = guild.get_channel(chan_id) if chan_id else None
        if not chan or not getattr(chan, "permissions_for", None):
            return
        if not chan.permissions_for(guild.me).send_messages:
            return
        who = getattr(member, "display_name", "?")
        shown = ", ".join(f"{k}={str(v)[:80]}" for k, v in (kwargs or {}).items()) or "(no args)"
        outcome = "ok" if ok else "failed"
        await chan.send(embed=discord.Embed(
            title=f"🧰 tool {spec.name} ({outcome})",
            description=f"by {who}: {shown}"[:2000],
            colour=0x586289))
    except Exception:
        log.exception("tool audit write failed")


# ---------- the generated prompt (advertised == installed, by construction) ----------

_HEADER = """
TOOLS (you can look things up before answering). Reply ONLY JSON, either:
  {"tool": "<name>", "args": {...}}          ← to look something up first
  {"reply": "<your answer>", "actions": []}  ← your final answer (+optional server actions)

Available tools:
"""

_FOOTER = """
Use them when the answer isn't already in what you know: current events, prices,
anything after your training, or specifics from other channels. Then answer in
your normal voice — cite what you found naturally, don't announce tool use.

FORMAT IS STRICT: your entire reply must be ONE JSON object — nothing before it,
nothing after it, no XML, no <tool calls> tags, no prose around it. If you don't
need a tool, just answer: {"reply": "..."}. Most messages need no tool at all —
small talk, opinions, and anything you already know should go straight to
{"reply": "..."}.
"""


def _arg_shape(spec: ToolSpec) -> str:
    """`{"query": "...", "n": 10}` — the model sees real arg names/defaults."""
    pieces = []
    for arg, rule in spec.args.items():
        kind, lim, optional = _parse_rule(spec.name, arg, rule)
        if kind == "str":
            pieces.append(f'"{arg}": "..."')
        elif kind == "int":
            pieces.append(f'"{arg}": {lim[1] // 10 or 1}')
        elif kind == "choice":
            pieces.append(f'"{arg}": "{lim[0]}"')
        else:
            pieces.append(f'"{arg}": true')
    return "{" + ", ".join(pieces) + "}"


def tools_prompt(admin: bool = False) -> str:
    """The TOOLS section, generated. Only what exists can be advertised."""
    rank = permissions.rank("admin" if admin else "member")
    lines = []
    for spec in specs():
        if permissions.rank(required_tier(spec)) > rank:
            continue
        lines.append(spec.line or
                     f'  {{"tool": "{spec.name}", "args": {_arg_shape(spec)}}}'
                     f" — {spec.description}")
    return _HEADER + "\n".join(lines) + "\n" + _FOOTER


def listing(tier: str = "member") -> str:
    """Human-facing /tools listing: what's installed, who can use it."""
    rank = permissions.rank(tier if tier in TIERS else "member")
    rows = []
    for spec in specs():
        need = required_tier(spec)
        mark = "✅" if permissions.rank(need) <= rank else "🔒"
        cost = "" if spec.cost == "free" else f" · {spec.cost}"
        rows.append(f"{mark} **{spec.name}** ({need}{cost}) — {spec.description}")
    return "\n".join(rows) or "(no tools installed)"