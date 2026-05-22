"""Bot entry point — creates the RyzeBot instance and starts it."""
import asyncio
import logging

import discord
from discord.ext import commands

from bot import config
from bot.database import engine, SessionLocal
from bot.utils.logging_utils import setup_logging

log = logging.getLogger(__name__)

COGS = [
    "bot.cogs.class_discovery",   # runs first — provisions new classes from Google Calendar
    "bot.cogs.calendar_sync",     # syncs events into provisioned classes
    "bot.cogs.reminders",
    "bot.cogs.attendance",
    "bot.cogs.lesson_threads",
    "bot.cogs.homework",
    "bot.cogs.admin",
    "bot.cogs.members",
]


class RyzeBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True

        super().__init__(
            command_prefix="!",  # Prefix commands disabled; slash commands only
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception:
                log.exception("Failed to load cog: %s", cog)

        guild = discord.Object(id=config.DISCORD_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d slash commands to guild %d.", len(synced), config.DISCORD_GUILD_ID)

    async def on_ready(self) -> None:
        log.info(
            "Ryze Education Bot ready — logged in as %s (ID: %d)",
            self.user,
            self.user.id,
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Ryze Education classes",
            )
        )

    async def on_command_error(self, ctx, error) -> None:
        log.warning("Command error: %s", error)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        log.exception("App command error in %r", interaction.command)
        msg = f"An error occurred: {error}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            log.exception("Could not send error response to interaction.")


async def main() -> None:
    setup_logging()
    bot = RyzeBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
