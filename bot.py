"""discord-bot — AI-powered server setup, automod, and community companion.

Run with:  python -m bot   (or: python bot.py)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

from config import DEFAULTS
import settings

# load .env from the project dir if present (KEY=value lines).
# .env WINS over the ambient environment: the launcher writes /bot/.env and the
# supervisor process may be hours old, so setdefault() meant model/config edits
# silently never took effect (the bot kept running the pre-edit fallback chain).
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip().strip('"').strip("'")
            if _v:
                os.environ[_k.strip()] = _v

logging.basicConfig(
    level=settings.log_level(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

REQUIRED_ENV = ["DISCORD_BOT_TOKEN"]


def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True   # needs to be enabled in the dev portal too
    intents.members = True           # needs "Server Members Intent" enabled in the portal

    bot = commands.Bot(
        command_prefix=commands.when_mentioned,  # prefix is basically unused; slash commands only
        intents=intents,
        help_command=None,
    )

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (id %s)", bot.user, bot.user.id)
        # Per-guild sync: instant, unlike global sync (which can take up to an
        # hour to propagate). Wrapped in a timeout so a stuck API call can't
        # block startup forever.
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced = await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=30)
                log.info("Synced %d commands to guild %s", len(synced), guild.id)
            except Exception:
                log.exception("Failed to sync commands to guild %s", guild.id)
        perms = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True,
            read_message_history=True, add_reactions=True, manage_messages=True,
            manage_roles=True, manage_channels=True, manage_guild=True,
            kick_members=True, ban_members=True, moderate_members=True,
        )
        log.info("Invite URL: %s", discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot", "applications.commands")))

    @bot.event
    async def on_guild_join(guild: discord.Guild):
        """Sync commands the moment we're added to a new server."""
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=30)
            log.info("Joined and synced %d commands to guild %s", len(synced), guild.id)
        except Exception:
            log.exception("Failed to sync commands to guild %s", guild.id)

    @bot.event
    async def on_command_error(ctx, error):
        # everything real is slash commands; don't spam logs for prefix attempts
        if isinstance(error, commands.CommandNotFound):
            return
        log.warning("command error: %s", error)

    @bot.tree.error
    async def on_app_command_error(interaction, error):
        # global net: a failing slash command logs AND tells the user, instead
        # of Discord's "this application did not respond"
        cmd = getattr(interaction.command, "name", "?")
        log.exception("slash /%s failed: %s", cmd, error)
        msg = "⚠️ that command hit an error — it's been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    @bot.event
    async def on_app_command_completion(interaction, command):
        log.info("slash /%s by %s — ok", command.name, interaction.user)

    return bot


# Held for the life of the process; closing it (or dying) releases the lock.
_LOCK_HANDLE = None


def claim_singleton() -> None:
    """Refuse to open a second gateway session on this box.

    Discord fans every event out to every session of a token, so a second
    process does not add capacity — it answers the same message again in
    different words. Exiting here is the whole fix for "double bots".
    """
    global _LOCK_HANDLE
    from single_instance import AlreadyRunning, claim

    path = os.environ.get("BOT_LOCK_PATH", "/tmp/inferolli-bot.lock")
    try:
        _LOCK_HANDLE = claim(path)
    except AlreadyRunning as e:
        log.error("another inferolli already owns this box — %s", e)
        log.error("not starting a second gateway session: every message would "
                  "be answered twice. exiting with 3.")
        sys.exit(3)


async def main() -> None:
    claim_singleton()
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log.error("Missing required env vars: %s (see .env.example)", ", ".join(missing))
        sys.exit(1)

    token = os.environ["DISCORD_BOT_TOKEN"]

    # model routing at a glance — this is how you confirm config.json won
    log.info("model routing: chat=%s fallbacks=%s (source: %s)",
             settings.ai_model(), settings.ai_fallbacks(),
             settings.CONFIG_PATH.name)

    bot = make_bot()

    # Load every cog in cogs/ so dropping a new file there is enough.
    import importlib
    import pkgutil
    import cogs

    for mod in pkgutil.iter_modules(cogs.__path__):
        if mod.name.startswith("_"):
            continue
        await bot.load_extension(f"cogs.{mod.name}")
        log.info("Loaded cog: %s", mod.name)

    # surface the defaults so /config show can describe them even before any guild config exists
    bot.guild_config_defaults = DEFAULTS

    await bot.start(token)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
