"""
Cog: Admin-triggered bot job poller.

The admin portal queues jobs via POST /api/admin/bot-jobs.
This cog polls GET /api/bot/jobs/pending every 30 seconds and executes
recognised job types immediately.

Supported job types (queued from Bot Health page):
  sync_members   — runs the same member sync as the 6-hour scheduled loop
  sync_classes   — runs the same class discovery as the periodic loop
  sync_lessons   — runs the same calendar sync as the periodic loop
  sync_attendance — future (no-op for now, logged)
"""
from __future__ import annotations

import logging

import aiohttp
from discord.ext import commands, tasks

from bot import config
from bot.services.portal_api import PortalAPIClient

log = logging.getLogger(__name__)

_BASE = (config.PORTAL_API_URL or "").rstrip("/")
_HEADERS = {"Authorization": f"Bearer {config.DASHBOARD_API_KEY or ''}", "Content-Type": "application/json"}


async def _claim_pending_jobs() -> list[dict]:
    """Poll the portal for pending jobs the bot should process."""
    if not config.DASHBOARD_API_KEY or not config.PORTAL_API_URL:
        return []
    try:
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{_BASE}/api/bot/jobs/pending") as resp:
                if resp.ok:
                    return await resp.json()
                return []
    except Exception as exc:
        log.debug("[bot_jobs] Poll failed (non-fatal): %s", exc)
        return []


async def _complete_job(job_id: int) -> None:
    try:
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.patch(f"{_BASE}/api/bot/jobs/{job_id}/complete")
    except Exception:
        pass


async def _fail_job(job_id: int, error: str) -> None:
    try:
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.patch(f"{_BASE}/api/bot/jobs/{job_id}/fail", json={"error": error})
    except Exception:
        pass


class BotJobsCog(commands.Cog):
    """Polls for admin-triggered jobs and executes them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._poll_loop.start()

    def cog_unload(self) -> None:
        self._poll_loop.cancel()

    @tasks.loop(seconds=30)
    async def _poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        jobs = await _claim_pending_jobs()
        if not jobs:
            return

        for job in jobs:
            job_id   = job["id"]
            job_type = job.get("job_type", "")
            log.info("[bot_jobs] Executing job #%d: %s", job_id, job_type)

            try:
                await self._execute(job_type)
                await _complete_job(job_id)
                log.info("[bot_jobs] Job #%d (%s) completed.", job_id, job_type)
            except Exception as exc:
                log.exception("[bot_jobs] Job #%d (%s) failed.", job_id, job_type)
                await _fail_job(job_id, str(exc))

    @_poll_loop.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _execute(self, job_type: str) -> None:
        if job_type == "sync_members":
            await self._trigger_member_sync()
        elif job_type == "sync_classes":
            await self._trigger_class_discovery()
        elif job_type == "sync_lessons":
            await self._trigger_calendar_sync()
        elif job_type == "sync_attendance":
            log.info("[bot_jobs] sync_attendance: attendance is captured in real-time via voice events — no manual trigger needed.")
        else:
            log.warning("[bot_jobs] Unknown job type: %r — skipping.", job_type)

    async def _trigger_member_sync(self) -> None:
        cog = self.bot.cogs.get("MembersCog")
        if not cog:
            raise RuntimeError("MembersCog not loaded — cannot run member sync")
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            raise RuntimeError(f"Guild {config.DISCORD_GUILD_ID} not found")
        await cog._sync_all_members(guild)

    async def _trigger_class_discovery(self) -> None:
        cog = self.bot.cogs.get("ClassDiscoveryCog")
        if not cog:
            raise RuntimeError("ClassDiscoveryCog not loaded — cannot run class discovery")
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            raise RuntimeError(f"Guild {config.DISCORD_GUILD_ID} not found")
        await cog._run_discovery(guild)

    async def _trigger_calendar_sync(self) -> None:
        cog = self.bot.cogs.get("CalendarSyncCog")
        if not cog:
            raise RuntimeError("CalendarSyncCog not loaded — cannot run calendar sync")
        # Call the cog's sync loop body directly (same code path as scheduled)
        from bot.services.lesson_service import sync_all_calendars_portal as sync_all_calendars
        from datetime import datetime, timezone
        started = datetime.now(timezone.utc).isoformat()
        counts = await sync_all_calendars()
        completed = datetime.now(timezone.utc).isoformat()
        await cog._handle_cancellations(counts.get("newly_cancelled", []))
        # Log the admin-triggered sync
        if config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
            async with PortalAPIClient() as client:
                await client.push_sync_log(
                    sync_type="lessons",
                    status="failed" if counts.get("errors", 0) > 0 else "success",
                    started_at=started,
                    completed_at=completed,
                    records_created=counts.get("created", 0),
                    records_updated=counts.get("updated", 0) + counts.get("cancelled", 0),
                    records_failed=counts.get("errors", 0),
                    source="admin_triggered",
                )
        log.info("[bot_jobs] Admin-triggered calendar sync: %s", {k: v for k, v in counts.items() if k != "newly_cancelled"})


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotJobsCog(bot))
