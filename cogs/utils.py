"""Small utilities: /remind, /slowmode."""

import asyncio
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from helpers import is_admin

log = logging.getLogger("cogs.utils")

_DUR = re.compile(r"(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?|d|days?|w|weeks?)", re.I)
MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int | None:
    m = _DUR.search(text)
    if not m:
        return None
    return int(m.group(1)) * MULT[m.group(2)[0].lower()]


class UtilsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reminders: dict[int, dict] = {}

    @app_commands.command(name="remind", description="Set a reminder (in-memory: survives until the bot restarts)")
    @app_commands.describe(when="e.g. 10m, 2h, 3d", text="what to remind you about")
    async def remind(self, interaction: discord.Interaction, when: str, text: str):
        seconds = parse_duration(when)
        if not seconds or seconds > 14 * 86400:
            return await interaction.response.send_message(
                "when should look like `10m`, `2h`, `3d` (max 2 weeks)", ephemeral=True)

        async def fire():
            await asyncio.sleep(seconds)
            try:
                await interaction.channel.send(
                    f"⏰ {interaction.user.mention} reminder: {text[:500]}",
                    allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
            self._reminders.pop(id(fire), None)

        self._reminders[id(fire)] = {"until": time.time() + seconds, "text": text}
        self.bot.loop.create_task(fire())
        h, m = divmod(seconds, 3600)
        due = f"{h}h {m}m" if h else f"{m}m"
        await interaction.response.send_message(f"⏰ reminding you in {due}: {text[:100]}", ephemeral=True)

    @app_commands.command(name="slowmode", description="Set channel slowmode (admin)")
    @app_commands.describe(seconds="0 to turn off, max 21600")
    async def slowmode(self, interaction: discord.Interaction,
                       seconds: app_commands.Range[int, 0, 21600]):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("admins only.", ephemeral=True)
        await interaction.channel.edit(slowmode_delay=seconds)
        await interaction.response.send_message(
            f"slowmode → {seconds}s" if seconds else "slowmode off", ephemeral=True)

    @app_commands.command(name="tools",
                          description="What i can look up for you, and who can use what")
    async def tools(self, interaction: discord.Interaction):
        """The human-facing view of the tool registry — same source of truth
        the model's prompt is generated from, so it can never advertise a
        capability that isn't installed."""
        import permissions
        import tool_registry
        tier = permissions.tier_of(interaction.user)
        await interaction.response.send_message(
            f"my lookup tools (you're **{tier}**):\n\n"
            f"{tool_registry.listing(tier)}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(UtilsCog(bot))
