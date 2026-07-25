"""Cog: Google Calendar sync — scheduled every N minutes + manual slash command.

When a lesson transitions to 'cancelled', enrolled students and the tutor are
notified automatically:
  • A DM is sent to each member.
  • A channel post with @mentions is sent to the class text channel.
  • If a lesson thread exists, a note is posted there too.

Notifications are deduplicated via reminder_log (reminder_type='cancelled')
so re-running the sync never double-sends.
"""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks

from bot import config
from bot.database import get_session
from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson
from bot.models.reminder_log import ReminderChannel
from bot.services.discord_service import safe_send_dm, get_discord_member
from bot.services.lesson_service import sync_all_calendars_hydrated as sync_all_calendars
from bot.services.portal_api import PortalAPIClient
from bot.services.reminder_service import (
    build_cancellation_dm,
    build_cancellation_channel_message,
    get_lesson_members,
    has_reminder_been_sent,
    record_reminder,
)

log = logging.getLogger(__name__)

_CANCELLED_RTYPE = "cancelled"


class CalendarSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._sync_loop.start()

    def cog_unload(self) -> None:
        self._sync_loop.cancel()

    # ── Scheduled sync ───────────────────────────────────────────────────── #

    @tasks.loop(minutes=config.CALENDAR_SYNC_INTERVAL)
    async def _sync_loop(self) -> None:
        await self.bot.wait_until_ready()
        log.info("Running scheduled calendar sync…")
        started = datetime.now(timezone.utc).isoformat()
        try:
            counts = await sync_all_calendars()
            completed = datetime.now(timezone.utc).isoformat()
            log.info("Calendar sync complete: %s", {k: v for k, v in counts.items() if k != "newly_cancelled"})
            await self._handle_cancellations(counts.get("newly_cancelled", []))
            # New classes were just mirrored locally — enrol their Discord-role
            # holders now so reminders have recipients before the next fire,
            # rather than waiting for the members cog's 6-hourly loop. [DEF-2]
            if counts.get("hydrated_groups_created", 0) > 0:
                await self._refresh_member_enrollment()
            # Push sync audit log to portal
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
                    )
        except Exception as exc:
            log.exception("Calendar sync failed.")
            if config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
                try:
                    async with PortalAPIClient() as client:
                        await client.push_sync_log(
                            sync_type="lessons",
                            status="failed",
                            started_at=started,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            error_message=str(exc),
                        )
                except Exception:
                    pass

    @_sync_loop.before_loop
    async def _before_sync(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_member_enrollment(self) -> None:
        """Enrol Discord-role holders into freshly-hydrated class groups now, so
        reminders/cancellations have recipients before the next fire instead of
        waiting up to 6 hours for the members cog's own loop. Best-effort."""
        members_cog = self.bot.cogs.get("MembersCog")
        if members_cog is None:
            return
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if guild is None:
            return
        try:
            await members_cog._sync_all_members(guild)
            log.info("Refreshed member enrollment after hydrating new class group(s).")
        except Exception:
            log.exception("Post-hydration member enrollment refresh failed.")

    # ── Cancellation notifications ────────────────────────────────────────── #

    async def _handle_cancellations(
        self, newly_cancelled: list[tuple[Lesson, ClassGroup]]
    ) -> None:
        if not newly_cancelled:
            return

        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            log.warning("Cancellation notifications skipped — guild not found.")
            return

        for lesson, class_group in newly_cancelled:
            try:
                await self._notify_cancellation(guild, lesson, class_group)
            except Exception:
                log.exception(
                    "Failed to send cancellation notifications for lesson %d.", lesson.id
                )

    async def _notify_cancellation(
        self,
        guild: discord.Guild,
        lesson: Lesson,
        class_group: ClassGroup,
    ) -> None:
        async with get_session() as session:
            members = await get_lesson_members(session, class_group.id)
            # Include tutor if not already in member list
            if class_group.tutor_user_id and class_group.tutor:
                if not any(m.id == class_group.tutor.id for m in members):
                    members.append(class_group.tutor)

            # ── Channel notification ──────────────────────────────────────
            channel_sent = await has_reminder_been_sent(
                session, lesson.id, _CANCELLED_RTYPE, ReminderChannel.class_channel
            )
            if not channel_sent and class_group.discord_text_channel_id:
                channel = guild.get_channel(class_group.discord_text_channel_id)
                if isinstance(channel, discord.TextChannel):
                    user_mentions = [f"<@{u.discord_user_id}>" for u in members] or None
                    msg = build_cancellation_channel_message(lesson, class_group, user_mentions)
                    success = False
                    error = None
                    try:
                        await channel.send(msg)
                        success = True
                        log.info(
                            "Cancellation notice posted to channel for lesson %d (%r).",
                            lesson.id, lesson.title,
                        )
                    except discord.HTTPException as exc:
                        error = str(exc)
                        log.warning("Channel cancellation notice failed for lesson %d: %s", lesson.id, exc)
                    await record_reminder(
                        session, lesson.id, _CANCELLED_RTYPE,
                        ReminderChannel.class_channel, success, error_message=error,
                    )

            # ── Thread notification (if a thread was created for this lesson) ──
            if class_group.discord_text_channel_id and lesson.discord_thread_id:
                thread = guild.get_thread(lesson.discord_thread_id)
                if thread is None:
                    try:
                        channel = guild.get_channel(class_group.discord_text_channel_id)
                        if isinstance(channel, discord.TextChannel):
                            thread = await channel.fetch_message(lesson.discord_thread_id)
                    except Exception:
                        thread = None
                if isinstance(thread, discord.Thread):
                    try:
                        await thread.send(
                            f"⚠️  This lesson has been **cancelled**. "
                            f"Please disregard the earlier thread — your tutor will be in touch."
                        )
                    except discord.HTTPException as exc:
                        log.warning("Could not post cancellation in thread %d: %s", lesson.discord_thread_id, exc)

            # ── DM each member ────────────────────────────────────────────
            for db_user in members:
                already_sent = await has_reminder_been_sent(
                    session, lesson.id, _CANCELLED_RTYPE,
                    ReminderChannel.dm, user_id=db_user.id,
                )
                if already_sent:
                    continue

                discord_member = await get_discord_member(guild, db_user.discord_user_id)
                success = False
                error = None
                if discord_member:
                    dm_text = build_cancellation_dm(lesson, class_group)
                    success = await safe_send_dm(discord_member, dm_text)
                    if not success:
                        error = "DM failed (Forbidden or HTTPException)"
                    else:
                        log.info(
                            "Cancellation DM sent to %s (user_id=%d) for lesson %d.",
                            db_user.full_name, db_user.id, lesson.id,
                        )
                else:
                    error = "Member not found in guild"

                await record_reminder(
                    session, lesson.id, _CANCELLED_RTYPE,
                    ReminderChannel.dm, success,
                    user_id=db_user.id, error_message=error,
                )

    # ── /sync_calendar command ────────────────────────────────────────────── #

    @app_commands.command(name="sync_calendar", description="Manually sync all Google Calendars.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def sync_calendar(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            counts = await sync_all_calendars()
            newly_cancelled = counts.pop("newly_cancelled", [])

            had_errors = counts.get("errors", 0) > 0
            colour = discord.Colour.brand_red() if had_errors else discord.Colour.brand_green()

            embed = discord.Embed(
                title="🗓️  Calendar Sync Complete" if not had_errors else "🗓️  Calendar Sync — Partial Errors",
                colour=colour,
            )
            embed.add_field(name="✅  Created",   value=str(counts["created"]),   inline=True)
            embed.add_field(name="🔄  Updated",   value=str(counts["updated"]),   inline=True)
            embed.add_field(name="❌  Cancelled", value=str(counts["cancelled"]), inline=True)
            embed.add_field(name="⏭️  Unchanged", value=str(counts["skipped"]),   inline=True)
            if newly_cancelled:
                embed.add_field(
                    name="📣  Notifying",
                    value=f"Sending cancellation alerts for {len(newly_cancelled)} lesson(s)…",
                    inline=False,
                )
            if had_errors:
                embed.add_field(
                    name="⚠️  Errors",
                    value=f"{counts['errors']} calendar(s) failed — check bot logs.",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

            # Fire notifications after responding so the slash command doesn't time out
            if newly_cancelled:
                guild = interaction.guild
                for lesson, class_group in newly_cancelled:
                    try:
                        await self._notify_cancellation(guild, lesson, class_group)
                    except Exception:
                        log.exception("Cancellation notification failed for lesson %d.", lesson.id)

        except Exception as exc:
            log.exception("Manual calendar sync failed.")
            embed = discord.Embed(
                title="❌  Calendar Sync Failed",
                description=f"```{exc}```\nCheck the bot logs for the full stack trace.",
                colour=discord.Colour.brand_red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CalendarSyncCog(bot))
