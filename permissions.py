"""permissions.py — per-action permission tiers.

Binary admin-or-not breaks down as soon as the server grows: a moderator who
should be able to time someone out shouldn't also be able to delete channels.
Every action declares the minimum tier it requires in config.json
(`permissions.tier_actions`), so *what* a person may do is a config decision,
not a hardcoded check scattered across cogs.

Tiers, lowest to highest:

    member  — ordinary participants
    staff   — moderators: member/ message-level actions
    admin   — server structure: channels, roles, shell access

Discord's own permissions are honoured as well, so a server that never sets
role ids still behaves sensibly:

    Administrator                        → admin
    Moderate Members / Manage Messages /
    Kick Members / Manage Roles …        → staff
"""

import logging

import discord

import settings

log = logging.getLogger("permissions")

TIERS = ("member", "staff", "admin")
_RANK = {name: i for i, name in enumerate(TIERS)}

# Discord permissions that are enough evidence of staff standing when no
# explicit staff role is configured.
_STAFF_PERMS = ("moderate_members", "manage_messages", "kick_members",
                "ban_members", "manage_roles", "manage_channels")


def _tier_actions() -> dict:
    return settings.get("permissions.tier_actions", {}) or {}


def _role_ids(key: str) -> set[int]:
    out = set()
    for rid in settings.get_list(f"permissions.{key}", [], env=key.upper()):
        try:
            out.add(int(rid))
        except (TypeError, ValueError):
            continue
    return out


def tier_of(member: discord.Member) -> str:
    """Highest tier this member holds."""
    if member is None or not hasattr(member, "guild_permissions"):
        return "member"
    perms = member.guild_permissions
    if getattr(perms, "administrator", False):
        return "admin"
    if member.id and getattr(member.guild, "owner_id", None) == member.id:
        return "admin"
    roles = {r.id for r in getattr(member, "roles", []) or []}
    if roles & _role_ids("admin_role_ids"):
        return "admin"
    if roles & _role_ids("staff_role_ids"):
        return "staff"
    if any(getattr(perms, p, False) for p in _STAFF_PERMS):
        return "staff"
    return "member"


def required_tier(action: str) -> str:
    """Minimum tier an action needs (defaults to admin — safe by omission)."""
    mapping = _tier_actions()
    want = mapping.get(action, "admin")
    return want if want in _RANK else "admin"


# --- tools (tool_registry) ---
# Tools carry their own default tier in the spec; a server can override any
# of them in config.json (`permissions.tool_tiers`) without touching code.

def _tool_tiers() -> dict:
    return settings.get("permissions.tool_tiers", {}) or {}


def rank(tier: str) -> int:
    return _RANK.get(tier, 0)


def tool_tier(name: str, default: str = "member") -> str:
    """Minimum tier a tool needs: config override, else the spec default."""
    want = _tool_tiers().get(name, default)
    return want if want in _RANK else default


def tool_allows(member: discord.Member, name: str, default: str = "member") -> bool:
    return rank(tier_of(member)) >= rank(tool_tier(name, default))


def allows(member: discord.Member, action: str) -> bool:
    """True if this member may perform `action`."""
    return _RANK[tier_of(member)] >= _RANK[required_tier(action)]


def refusal(member: discord.Member, action: str) -> str | None:
    """None if allowed, else a short honest reason to show the user."""
    if allows(member, action):
        return None
    need = required_tier(action)
    held = tier_of(member)
    if need == "admin":
        return "that needs an admin — bug someone with the keys"
    if need == "staff":
        return "that's a staff action — you're not on the crew list for it"
    return f"'{action}' needs the {need} tier (you're {held})"


def is_staff(member: discord.Member) -> bool:
    return _RANK[tier_of(member)] >= _RANK["staff"]


def is_admin(member: discord.Member) -> bool:
    return tier_of(member) == "admin"
