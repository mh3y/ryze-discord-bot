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
    "bot.cogs.bot_jobs",          # polls admin portal for queued sync jobs
    "bot.cogs.heartbeat",         # pings a dead-man's-switch so an outage is visible
]


def _print_startup_health() -> None:
    """
    Print a clear startup health check to logs so you can immediately
    confirm the bot is pointing at the right services.

    Any ❌ here means the bot will not sync to the portal — check the deploy env vars.
    """
    sep = "=" * 60

    log.info(sep)
    log.info("  RYZE EDUCATION BOT — STARTUP HEALTH CHECK")
    log.info(sep)

    # ── Discord ──────────────────────────────────────────────────────────────
    log.info("[discord] Guild ID:        %s", config.DISCORD_GUILD_ID)

    # ── Portal API sync ──────────────────────────────────────────────────────
    api_url  = config.PORTAL_API_URL or ""
    api_key  = config.DASHBOARD_API_KEY or ""
    sync_ok  = bool(api_url and api_key)

    log.info("[portal]  API URL:         %s", api_url or "❌ NOT SET  ← add PORTAL_API_URL to .env")
    log.info("[portal]  API key set:     %s", "✓ yes" if api_key else "❌ NO  ← add DASHBOARD_API_KEY to .env")

    if not api_url:
        log.warning(
            "[portal]  ⚠️  PORTAL_API_URL is not set. "
            "Bot will NOT push members/lessons to the CRM portal. "
            "Add: PORTAL_API_URL=https://ryze-portal-api.onrender.com"
        )
    if not api_key:
        log.warning(
            "[portal]  ⚠️  DASHBOARD_API_KEY is not set. "
            "Bot will NOT push data to the CRM portal. "
            "Add: DASHBOARD_API_KEY=<value of BOT_API_SECRET on Render>"
        )
    if sync_ok:
        log.info("[portal]  ✅ Portal sync ENABLED — data will flow to Supabase via Render")
    else:
        log.warning("[portal]  ❌ Portal sync DISABLED — fix the variables above, then restart")

    # ── Google Calendar ───────────────────────────────────────────────────────
    gcal_ok = all([
        config.GOOGLE_CLIENT_ID,
        config.GOOGLE_CLIENT_SECRET,
        config.GOOGLE_REFRESH_TOKEN,
    ])
    log.info("[gcal]    Calendar sync:   %s", "✓ configured" if gcal_ok else "❌ NOT configured — check GOOGLE_* env vars")

    # ── Database ──────────────────────────────────────────────────────────────
    log.info("[db]      DATABASE_URL:    %s", "✓ set" if config.DATABASE_URL else "❌ NOT SET")

    log.info(sep)


async def _run_startup_probe() -> None:
    """Real connectivity probe — env-var PRESENCE is not health. [M9]

    DB is fail-fast (a bot with no database can do nothing; exiting non-zero
    mirrors the fail-closed migration posture and makes the deploy visibly
    fail). Portal and Google failures are loud but non-fatal: the bot can still
    capture voice state and self-heal when they recover.
    """
    from sqlalchemy import text

    # ── Database: SELECT 1, fail fast ────────────────────────────────────────
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("[probe] database:  ✅ PASS")
    except Exception:
        log.critical(
            "[probe] database:  ❌ FAIL — DATABASE_URL is unreachable; refusing to start.",
            exc_info=True,
        )
        raise

    # ── Portal API: authenticated GET (proves URL + key together) ────────────
    if config.PORTAL_API_URL and config.DASHBOARD_API_KEY:
        try:
            from bot.services.portal_api import PortalAPIClient
            async with PortalAPIClient() as client:
                await client.get_classes()
            log.info("[probe] portal:    ✅ PASS")
        except Exception:
            log.error(
                "[probe] portal:    ❌ FAIL — CRM unreachable or DASHBOARD_API_KEY wrong "
                "(must equal BOT_API_SECRET on Render). Sync will not work until fixed.",
                exc_info=True,
            )
    else:
        log.warning("[probe] portal:    ⚠️ SKIPPED — PORTAL_API_URL / DASHBOARD_API_KEY not set.")

    # ── Google Calendar: credential refresh (blocking libs → executor) ───────
    try:
        from google.auth.transport.requests import Request
        from bot.services.google_calendar_service import _build_credentials
        creds = _build_credentials()
        await asyncio.get_running_loop().run_in_executor(None, creds.refresh, Request())
        log.info("[probe] google:    ✅ PASS")
    except Exception:
        log.error(
            "[probe] google:    ❌ FAIL — calendar sync will not work (check GOOGLE_* env vars).",
            exc_info=True,
        )


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
    _print_startup_health()
    await _run_startup_probe()  # fail-fast on DB, loud on portal/Google [M9]
    bot = RyzeBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
