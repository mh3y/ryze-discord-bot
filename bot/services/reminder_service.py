"""Reminder sending logic with deduplication via reminder_logs."""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import get_session
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.lesson import Lesson
from bot.models.reminder_log import ReminderLog, ReminderChannel
from bot.models.user import User
from bot.utils.time_utils import format_sydney, now_utc

log = logging.getLogger(__name__)

# Reminder type labels keyed by minutes-before-lesson
REMINDER_LABELS: dict[int, str] = {
    24 * 60: "24h",
    60: "1h",
    15: "15min",
}


def _reminder_type(offset_minutes: int) -> str:
    return REMINDER_LABELS.get(offset_minutes, f"{offset_minutes}min")


async def has_reminder_been_sent(
    session: AsyncSession,
    lesson_id: int,
    reminder_type: str,
    channel: ReminderChannel,
    user_id: Optional[int] = None,
) -> bool:
    conditions = [
        ReminderLog.lesson_id == lesson_id,
        ReminderLog.reminder_type == reminder_type,
        ReminderLog.channel == channel,
        ReminderLog.success.is_(True),
    ]
    if user_id is not None:
        conditions.append(ReminderLog.user_id == user_id)
    else:
        conditions.append(ReminderLog.user_id.is_(None))

    result = await session.execute(select(ReminderLog).where(and_(*conditions)).limit(1))
    return result.scalar_one_or_none() is not None


async def record_reminder(
    session: AsyncSession,
    lesson_id: int,
    reminder_type: str,
    channel: ReminderChannel,
    success: bool,
    user_id: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    log_entry = ReminderLog(
        lesson_id=lesson_id,
        user_id=user_id,
        reminder_type=reminder_type,
        channel=channel,
        sent_at=now_utc(),
        success=success,
        error_message=error_message,
    )
    session.add(log_entry)
    await session.flush()


def build_dm_message(lesson: Lesson, class_group: ClassGroup, offset_minutes: int) -> str:
    label_map = {24 * 60: "24 hours", 60: "1 hour", 15: "15 minutes"}
    time_label = label_map.get(offset_minutes, f"{offset_minutes} minutes")
    lesson_time = format_sydney(lesson.start_time, "%I:%M %p")

    lines = [
        f"Your **{class_group.name}** lesson with Ryze Education begins in **{time_label}**.",
        "",
        f"Lesson time: {lesson_time}",
    ]
    if lesson.meet_link:
        lines += ["", f"Google Meet: {lesson.meet_link}"]
    elif lesson.location:
        lines += ["", f"Location: {lesson.location}"]
    lines += [
        "",
        "Please prepare your workbook, calculator, notes, and any homework questions.",
    ]
    return "\n".join(lines)


def build_channel_message(
    lesson: Lesson,
    class_group: ClassGroup,
    offset_minutes: int,
    user_mentions: Optional[list[str]] = None,
) -> str:
    """Build the channel reminder message.

    ``user_mentions`` is a list of Discord mention strings (e.g. ``["<@123>", "<@456>"]``)
    for every student and tutor enrolled in the class.  If provided they are
    placed at the top of the message so Discord notifies each person directly.
    Falls back to the class name in bold when no members are supplied.
    """
    label_map = {24 * 60: "24 hours", 60: "1 hour", 15: "15 minutes"}
    time_label = label_map.get(offset_minutes, f"{offset_minutes} minutes")
    lesson_time = format_sydney(lesson.start_time, "%I:%M %p")

    if user_mentions:
        mention_line = " ".join(user_mentions)
    else:
        mention_line = f"**{class_group.name}**"

    lines = [
        mention_line,
        "",
        f"**{class_group.name}** lesson begins in **{time_label}** ({lesson_time}).",
        "",
        "Please join on time and prepare your workbook, calculator, notes, and any homework questions.",
    ]
    if lesson.meet_link:
        lines += ["", f"Google Meet: {lesson.meet_link}"]
    return "\n".join(lines)


def build_cancellation_dm(lesson: "Lesson", class_group: "ClassGroup") -> str:
    """DM sent to each enrolled member when a lesson is cancelled."""
    lesson_time = format_sydney(lesson.start_time, "%I:%M %p")
    lesson_date = format_sydney(lesson.start_time, "%A, %d %B %Y")
    lines = [
        f"Your **{class_group.name}** lesson scheduled for **{lesson_date}** at **{lesson_time}** "
        f"has been **cancelled**.",
        "",
        "Please check with your tutor if you have any questions.",
    ]
    return "\n".join(lines)


def build_cancellation_channel_message(
    lesson: "Lesson",
    class_group: "ClassGroup",
    user_mentions: Optional[list[str]] = None,
) -> str:
    """Channel post sent to the class text channel when a lesson is cancelled."""
    lesson_time = format_sydney(lesson.start_time, "%I:%M %p")
    lesson_date = format_sydney(lesson.start_time, "%A, %d %B %Y")

    mention_line = " ".join(user_mentions) if user_mentions else f"**{class_group.name}**"
    lines = [
        mention_line,
        "",
        f"⚠️  The **{class_group.name}** lesson on **{lesson_date}** at **{lesson_time}** "
        f"has been **cancelled**.",
        "",
        "Please check with your tutor for further details.",
    ]
    return "\n".join(lines)


async def get_lesson_members(
    session: AsyncSession, class_group_id: int
) -> list[User]:
    result = await session.execute(
        select(User)
        .join(ClassGroupMember, ClassGroupMember.user_id == User.id)
        .where(
            and_(
                ClassGroupMember.class_group_id == class_group_id,
                ClassGroupMember.active.is_(True),
                User.active.is_(True),
            )
        )
    )
    return list(result.scalars().all())
