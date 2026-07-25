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
from sqlalchemy import select, and_

from bot import config
from bot.database import get_session
from bot.models.class_group import ClassGroup, ClassGroupMember
from bot.models.user import User
from bot.services.portal_api import PortalAPIClient
from bot.utils.task_safety import install_loop_restart

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
        # Logged, never raised: a failed callback leaves the job stuck in
        # 'processing' on the portal — that must be visible, not silent. [M20]
        log.warning("[bot_jobs] Could not mark job #%d complete — it will look stuck in the portal.", job_id, exc_info=True)


async def _fail_job(job_id: int, error: str) -> None:
    try:
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.patch(f"{_BASE}/api/bot/jobs/{job_id}/fail", json={"error": error})
    except Exception:
        log.warning("[bot_jobs] Could not mark job #%d failed — it will look stuck in the portal.", job_id, exc_info=True)


class BotJobsCog(commands.Cog):
    """Polls for admin-triggered jobs and executes them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        install_loop_restart(self._poll_loop, "bot_jobs.poll", self.bot)
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
            # Everything — including reading the job's fields — stays inside the
            # try: a malformed job (e.g. missing "id") must fail THAT job, not
            # crash the loop and silently halt all job processing. [DEF-18/H10]
            job_id: int | None = None
            try:
                job_id   = job["id"]
                job_type = job.get("job_type", "")
                log.info("[bot_jobs] Executing job #%d: %s", job_id, job_type)
                await self._execute(job_type, job)
                await _complete_job(job_id)
                log.info("[bot_jobs] Job #%d (%s) completed.", job_id, job_type)
            except Exception as exc:
                log.exception("[bot_jobs] Job %s failed.", f"#{job_id}" if job_id is not None else repr(job))
                if job_id is not None:
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

        # Normalise EVERY snowflake in the payload to int up front, so a grant/
        # revoke comparison can never be defeated by a JSON str-vs-number mismatch
        # (`123 in ["123"]` is False). [review: revoke skip-guard str/int mismatch]
        try:
            channel_id = int(channel_id_str)
        except (TypeError, ValueError):
            log.warning("[bot_jobs] discord_permission_sync: non-numeric channel_id %r — skipping.", channel_id_str)
            return

        grant_ids: list[int] = self._coerce_snowflakes(payload.get("grant_discord_ids"))
        revoke_ids: list[int] = self._coerce_snowflakes(payload.get("revoke_discord_ids"))

        # Also ensure the tutor has access (validated like every other grant)
        tutor_discord_id = payload.get("tutor_discord_id")
        if tutor_discord_id is not None:
            try:
                tutor_id_int = int(tutor_discord_id)
                if tutor_id_int not in grant_ids:
                    grant_ids = [tutor_id_int, *grant_ids]
            except (TypeError, ValueError):
                log.warning("[bot_jobs] discord_permission_sync: non-numeric tutor_discord_id %r — ignoring.", tutor_discord_id)

        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            raise RuntimeError(f"Guild {config.DISCORD_GUILD_ID} not in cache")

        channel = guild.get_channel(channel_id)
        if not channel:
            raise RuntimeError(f"Channel {channel_id} not found in guild")
        # DEF-15: only ever apply overwrites to a real text channel. A class
        # record whose discord_text_channel_id points at a CATEGORY (misconfig or
        # malicious CRM data) would otherwise cascade the grant onto every
        # permission-synced child channel. [review: channel type never checked]
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(
                f"discord_permission_sync: channel {channel_id} is a "
                f"{type(channel).__name__}, not a text channel — refusing. [DEF-15]"
            )

        # ── DEF-15: validate BEFORE touching any permission ────────────────
        # The payload arrives over the shared-secret bot API with no human in
        # the loop. Never apply it blindly: the channel must be a class channel
        # the bot knows, and every GRANT must map to someone actually enrolled
        # in (or tutoring) that class. Anything else is skipped and logged —
        # a malformed or malicious job must not be able to put a student in a
        # staff channel or an outsider in a minors' class channel.
        # Always validate the channel is a known active class channel (raises for
        # an unknown channel) — this guards REVOKES too, not just grants. [DEF-15]
        authorised_grant_ids = await self._authorised_member_ids_for_channel(channel_id)

        # Permissions granted to enrolled tutors/students:
        #   view_channel=True, send_messages=True, read_message_history=True
        allow_overwrite = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
        # Revoking explicitly hides the channel for that member.
        deny_overwrite = discord.PermissionOverwrite(view_channel=False)

        granted = 0
        still_refused: list[int] = []
        if not config.PERMISSION_SYNC_GRANTS_ENABLED:
            # Grant application is OFF (default until the owner enables it after the
            # soak). Log what WOULD be granted but apply nothing; revokes below
            # still run. still_refused stays empty, so the job COMPLETES — a
            # deliberate config hold is not a failure. [charter grant-flag / soak]
            if grant_ids:
                log.warning(
                    "[bot_jobs] discord_permission_sync: grant application DISABLED "
                    "(PERMISSION_SYNC_GRANTS_ENABLED unset) — NOT applying %d grant(s) on #%s; "
                    "revokes still processed. Enable after the 7-day soak.",
                    len(grant_ids), channel,
                )
        else:
            # A refused grant is most often a legitimate member whose local
            # membership row does not exist yet (right after a CRM enrolment, or on
            # relaunch when the local mirror was rebuilt from Discord state). Run one
            # member sync and re-validate before giving up, so a real new student is
            # not silently locked out. [review: job completes 'successfully' when every grant refused]
            if any(s not in authorised_grant_ids for s in grant_ids):
                log.info(
                    "[bot_jobs] discord_permission_sync: one or more grants not yet locally "
                    "enrolled — running a member sync and re-validating once."
                )
                try:
                    await self._trigger_member_sync()
                except Exception:
                    log.exception("[bot_jobs] discord_permission_sync: member-sync retry failed (non-fatal).")
                authorised_grant_ids = await self._authorised_member_ids_for_channel(channel_id)

            for snowflake in grant_ids:
                if snowflake not in authorised_grant_ids:
                    still_refused.append(snowflake)
                    log.warning(
                        "[bot_jobs] discord_permission_sync: REFUSED grant for %s on #%s — "
                        "not an active enrolled member/tutor of that class. [DEF-15]",
                        snowflake, channel,
                    )
                    continue
                member = guild.get_member(snowflake)
                if member:
                    await channel.set_permissions(member, overwrite=allow_overwrite, reason="CRM discord_permission_sync — enrol")
                    granted += 1
                    log.debug("[bot_jobs] Granted %s access to #%s", member, channel)
                else:
                    still_refused.append(snowflake)
                    log.warning("[bot_jobs] discord_permission_sync: member %s not in guild — skipping grant.", snowflake)

        # Revokes are the SAFE direction (deny access) and legitimately target
        # people who no longer have a membership row (they were just unenrolled),
        # so they are not membership-validated — but the channel itself was, and
        # the admin/owner guards below still apply.
        revoked = 0
        for snowflake in revoke_ids:
            # Safety: never revoke the tutor or anyone already being granted access
            if snowflake in grant_ids:
                log.info("[bot_jobs] discord_permission_sync: %s is in grant list — skipping revoke.", snowflake)
                continue
            member = guild.get_member(snowflake)
            if member:
                # Safety: never strip permissions from server admins or server owner
                if member.guild_permissions.administrator or member == guild.owner:
                    log.warning("[bot_jobs] discord_permission_sync: skipping revoke for admin/owner %s", member)
                    continue
                await channel.set_permissions(member, overwrite=deny_overwrite, reason="CRM discord_permission_sync — unenrol")
                revoked += 1
                log.debug("[bot_jobs] Revoked %s access to #%s", member, channel)
            else:
                log.debug("[bot_jobs] discord_permission_sync: member %s not in guild — skipping revoke.", snowflake)

        log.info(
            "[bot_jobs] discord_permission_sync: channel=%s granted=%d/%d revoked=%d/%d",
            channel_id, granted, len(grant_ids), revoked, len(revoke_ids),
        )

        # A grant still refused after the member-sync retry is a real, visible
        # problem (that student cannot see their class channel). Fail the job so
        # it re-queues — bounded by max_attempts CRM-side, then surfaced on the
        # failed-jobs view — instead of reporting false success. The grants that
        # DID apply above are idempotent, so a retry re-applies them safely.
        # [review: job completes 'successfully' even when every grant refused]
        if still_refused:
            raise RuntimeError(
                f"discord_permission_sync: {len(still_refused)} grant(s) could not be "
                f"authorised for channel {channel_id} after a member sync "
                f"(ids={still_refused}) — not enrolled locally, or not in the guild. [DEF-15]"
            )

    @staticmethod
    def _coerce_snowflakes(raw: Any) -> list[int]:
        """Normalise a payload list of snowflakes to ints, dropping non-numeric
        entries with a warning. Keeps every grant/revoke comparison in int space."""
        out: list[int] = []
        for item in raw or []:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                log.warning("[bot_jobs] discord_permission_sync: ignoring non-numeric snowflake %r.", item)
        return out

    @staticmethod
    async def _authorised_member_ids_for_channel(channel_id: int) -> set[int]:
        """Return the Discord user IDs allowed to be GRANTED access to a class
        channel: the active enrolled members of EVERY active class that owns the
        channel, plus each such class's assigned tutor. Raises if no active class
        owns the channel — an unknown channel must never have permissions
        applied. [DEF-15]

        Two active groups may legitimately share one channel (two CRM classes
        pointed at the same Discord channel); we union all their rosters rather
        than picking one arbitrarily with .first(), so neither class's students
        are refused. [review: duplicate class groups resolved by unordered .first()]

        KNOWN LIMITATION (deferred to Wave B2) — the local membership mirror is
        deactivated by /delete_user, /unenrol and (now) never re-created for an
        inactive user, but it is NOT reconciled against the CRM's own enrolment
        truth: a student WITHDRAWN in the CRM keeps an active local membership
        row, so a replayed/crafted grant job could still re-admit them. Closing
        this fully requires the bot to read per-class rosters from the CRM, but
        GET /api/bot/classes exposes only class metadata (no enrolments) — the
        reconcile therefore needs a new CRM endpoint and ships with the Wave B2
        CRM PR. [review: DEF-15 substrate ratchet]
        """
        async with get_session() as session:
            grp_result = await session.execute(
                select(ClassGroup).where(
                    and_(
                        ClassGroup.discord_text_channel_id == channel_id,
                        ClassGroup.active.is_(True),
                    )
                )
            )
            groups = grp_result.scalars().all()
            if not groups:
                raise RuntimeError(
                    f"discord_permission_sync: channel {channel_id} is not a known active "
                    "class channel — refusing to apply permission changes. [DEF-15] "
                    "(If the class was just provisioned, the local mirror may not have "
                    "hydrated yet; the job will be retried.)"
                )

            group_ids = [g.id for g in groups]
            tutor_user_ids = [g.tutor_user_id for g in groups if g.tutor_user_id]

            rows = await session.execute(
                select(User.discord_user_id)
                .join(ClassGroupMember, ClassGroupMember.user_id == User.id)
                .where(
                    and_(
                        ClassGroupMember.class_group_id.in_(group_ids),
                        ClassGroupMember.active.is_(True),
                        User.active.is_(True),
                    )
                )
            )
            authorised: set[int] = {row[0] for row in rows.all() if row[0] is not None}

            # Tutor path mirrors the member query: a soft-deactivated (offboarded)
            # tutor must NOT stay grantable. [review: DEF-15 tutor path skips User.active]
            if tutor_user_ids:
                tutor_rows = await session.execute(
                    select(User.discord_user_id).where(
                        and_(
                            User.id.in_(tutor_user_ids),
                            User.active.is_(True),
                        )
                    )
                )
                authorised.update(r[0] for r in tutor_rows.all() if r[0] is not None)

        return authorised

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
        # Call the cog's sync loop body directly (same code path as scheduled):
        # hydrate local class groups from the portal, then run the local-first sync.
        from bot.services.lesson_service import sync_all_calendars_hydrated as sync_all_calendars
        from datetime import datetime, timezone
        started = datetime.now(timezone.utc).isoformat()
        counts = await sync_all_calendars()
        completed = datetime.now(timezone.utc).isoformat()
        await cog._handle_cancellations(counts.get("newly_cancelled", []))
        # Newly-mirrored classes need their Discord-role holders enrolled now so
        # reminders have recipients before the next fire. Best-effort. [DEF-2]
        if counts.get("hydrated_groups_created", 0) > 0:
            try:
                await self._trigger_member_sync()
            except Exception:
                log.exception("[bot_jobs] Post-sync member enrollment refresh failed.")
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
