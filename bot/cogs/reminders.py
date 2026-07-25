"""Cog: Lesson reminders — runs every minute, sends DMs and channel messages."""
import logging
from datetime import timedelta, timezone

import discord
from discord.ext import commands, tasks

from bot import config
from bot.database import get_session
from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson, LessonStatus
from bot.models.reminder_log import ReminderChannel
from bot.models.user import User, UserRole
from bot.services.lesson_service import get_upcoming_lessons
from bot.services.reminder_service import (
    build_channel_message,
    build_dm_message,
    get_lesson_members,
    has_reminder_been_sent,
    record_reminder,
    _reminder_type,
)
from bot.services.discord_service import safe_send_dm, get_discord_member
from bot.utils.time_utils import now_utc
from bot.utils.task_safety import install_loop_restart

log = logging.getLogger(__name__)


class RemindersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        install_loop_restart(self._reminder_loop, "reminders.reminder", self.bot)
        self._reminder_loop.start()

    def cog_unload(self) -> None:
        self._reminder_loop.cancel()

    @tasks.loop(minutes=1)
    async def _reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            return
        try:
            await self._process_reminders(guild)
        except Exception:
            log.exception("Reminder loop error.")

    @_reminder_loop.before_loop
    async def _before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_reminders(self, guild: discord.Guild) -> None:
        now = now_utc()
        async with get_session() as session:
            lessons = await get_upcoming_lessons(session, hours_ahead=25)

            for lesson in lessons:
                class_group = lesson.class_group
                # Normalise start_time: SQLite returns naive datetimes; treat as UTC
                start = lesson.start_time
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                for offset in config.REMINDER_OFFSETS_MINUTES:
                    fire_at = start - timedelta(minutes=offset)
                    # Fire within the current minute window
                    if not (fire_at <= now < fire_at + timedelta(minutes=1)):
                        continue

                    rtype = _reminder_type(offset)
                    await self._send_lesson_reminders(
                        session, guild, lesson, class_group, offset, rtype
                    )

    async def _send_lesson_reminders(
        self,
        session,
        guild: discord.Guild,
        lesson: Lesson,
        class_group: ClassGroup,
        offset: int,
        rtype: str,
    ) -> None:
        # --- Fetch enrolled members once (used for both channel mentions and DMs) ---
        members = await get_lesson_members(session, class_group.id)
        # Include the assigned tutor if not already in the member list
        if class_group.tutor_user_id and class_group.tutor:
            tutor_in_list = any(m.id == class_group.tutor.id for m in members)
            if not tutor_in_list:
                members.append(class_group.tutor)

        # No recipients yet (a freshly-hydrated class whose Discord-role enrollment
        # hasn't populated, or an empty class): skip entirely rather than post a
        # mention-less reminder and record it as sent — recording would dedup it so
        # it never re-fires once members are enrolled seconds/minutes later. [DEF-2]
        if not members:
            return

        # --- Channel reminder ---
        channel_sent = await has_reminder_been_sent(
            session, lesson.id, rtype, ReminderChannel.class_channel
        )
        if not channel_sent and class_group.discord_text_channel_id:
            channel = guild.get_channel(class_group.discord_text_channel_id)
            if isinstance(channel, discord.TextChannel):
                # Build individual @mentions for every enrolled student and tutor
                # so Discord notifies exactly the right people — no role ping needed.
                user_mentions = [
                    f"<@{u.discord_user_id}>" for u in members
                ]
                msg = build_channel_message(lesson, class_group, offset, user_mentions or None)
                success = False
                error = None
                try:
                    await channel.send(msg)
                    success = True
                except discord.HTTPException as exc:
                    error = str(exc)
                    log.warning("Channel reminder failed for lesson %d: %s", lesson.id, exc)

                await record_reminder(
                    session, lesson.id, rtype, ReminderChannel.class_channel, success, error_message=error
                )

        for db_user in members:
            already_sent = await has_reminder_been_sent(
                session, lesson.id, rtype, ReminderChannel.dm, user_id=db_user.id
            )
            if already_sent:
                continue

            discord_member = await get_discord_member(guild, db_user.discord_user_id)
            success = False
            error = None
            if discord_member:
                dm_text = build_dm_message(lesson, class_group, offset)
                success = await safe_send_dm(discord_member, dm_text)
                if not success:
                    error = "DM failed (Forbidden or HTTPException)"
            else:
                error = "Member not found in guild"

            await record_reminder(
                session, lesson.id, rtype, ReminderChannel.dm,
                success, user_id=db_user.id, error_message=error
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RemindersCog(bot))
