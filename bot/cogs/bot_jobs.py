"""
Cog: Admin-triggered bot job poller.

The admin portal queues jobs via POST /api/admin/bot-jobs.
This cog polls GET /api/bot/jobs/pending every 30 seconds and executes
recognised job types immediately.

Supported job types:
  sync_members            — runs the same member sync as the 6-hour scheduled loop
  sync_classes            — runs the same class discovery as the periodic loop
  sync_lessons            — runs the same calendar sync as the periodic loop
  sync_attendance         — no-op (attendance captured via real-time voice events)
  discord_permission_sync — apply/revoke per-channel permission overwrites for
                            a class's enrolled students and tutor
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord
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
                await self._execute(job_type, job)
                await _complete_job(job_id)
                log.info("[bot_jobs] Job #%d (%s) completed.", job_id, job_type)
            except Exception as exc:
                log.exception("[bot_jobs] Job #%d (%s) failed.", job_id, job_type)
                await _fail_job(job_id, str(exc))

    @_poll_loop.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _execute(self, job_type: str, job: dict[str, Any]) -> None:
        if job_type == "sync_members":
            await self._trigger_member_sync()
        elif job_type == "sync_classes":
            await self._trigger_class_discovery()
        elif job_type == "sync_lessons":
            await self._trigger_calendar_sync()
        elif job_type == "sync_attendance":
            log.info("[bot_jobs] sync_attendance: attendance captured in real-time — no manual trigger needed.")
        elif job_type == "discord_permission_sync":
            payload = job.get("payload") or {}
            await self._trigger_discord_permission_sync(payload)
        else:
            log.warning("[bot_jobs] Unknown job type: %r — skipping.", job_type)

    async def _trigger_discord_permission_sync(self, payload: dict[str, Any]) -> None:
        """
        Apply channel permission overwrites from a CRM discord_permission_sync job.

        Payload fields (all optional — skip gracefully if absent):
          channel_id         — Discord channel snowflake (string)
          tutor_discord_id   — Discord user snowflake for the tutor (string | None)
          grant_discord_ids  — list of snowflake strings to grant view+send access
          revoke_discord_ids — list of snowflake strings to revoke access
        """
        channel_id_str: str | None = payload.get("channel_id")
        if not channel_id_str:
            log.warning("[bot_jobs] discord_permission_sync: no channel_id in payload — skipping.")
            return

        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            raise RuntimeError(f"Guild {config.DISCORD_GUILD_ID} not in cache")

        channel = guild.get_channel(int(channel_id_str))
        if not channel:
            raise RuntimeError(f"Channel {channel_id_str} not found in guild")

        # Permissions granted to enrolled tutors/students:
        #   view_channel=True, send_messages=True, read_message_history=True
        allow_overwrite = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
        # Revoking explicitly hides the channel for that member.
        deny_overwrite = discord.PermissionOverwrite(view_channel=False)

        grant_ids: list[str] = payload.get("grant_discord_ids") or []
        revoke_ids: list[str] = payload.get("revoke_discord_ids") or []

        # Also ensure the tutor has access
        tutor_discord_id: str | None = payload.get("tutor_discord_id")
        if tutor_discord_id and tutor_discord_id not in grant_ids:
            grant_ids = [tutor_discord_id, *grant_ids]

        for snowflake in grant_ids:
            member = guild.get_member(int(snowflake))
            if member:
                await channel.set_permissions(member, overwrite=allow_overwrite, reason="CRM discord_permission_sync — enrol")
                log.debug("[bot_jobs] Granted %s access to #%s", member, channel)
            else:
                log.warning("[bot_jobs] discord_permission_sync: member %s not in guild — skipping grant.", snowflake)

        for snowflake in revoke_ids:
            # Safety: never revoke the tutor or anyone already being granted access
            if snowflake in grant_ids:
                log.info("[bot_jobs] discord_permission_sync: %s is in grant list — skipping revoke.", snowflake)
                continue
            member = guild.get_member(int(snowflake))
            if member:
                # Safety: never strip permissions from server admins or server owner
                if member.guild_permissions.administrator or member == guild.owner:
                    log.warning("[bot_jobs] discord_permission_sync: skipping revoke for admin/owner %s", member)
                    continue
                await channel.set_permissions(member, overwrite=deny_overwrite, reason="CRM discord_permission_sync — unenrol")
                log.debug("[bot_jobs] Revoked %s access to #%s", member, channel)
            else:
                log.debug("[bot_jobs] discord_permission_sync: member %s not in guild — skipping revoke.", snowflake)

        log.info(
            "[bot_jobs] discord_permission_sync: channel=%s granted=%d revoked=%d",
            channel_id_str, len(grant_ids), len(revoke_ids),
        )

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
