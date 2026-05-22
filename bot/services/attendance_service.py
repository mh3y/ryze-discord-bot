"""Attendance tracking logic — voice session management and status calculation."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database import get_session
from bot.models.attendance import AttendanceRecord, AttendanceStatus, MarkedBy, VoiceSession
from bot.models.class_group import ClassGroupMember, MemberRole
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User
from bot import config
from bot.utils.time_utils import now_utc

log = logging.getLogger(__name__)


def calculate_attendance_status(
    lesson: Lesson,
    first_joined_at: datetime,
    total_minutes: int,
) -> AttendanceStatus:
    """Determine attendance status from timing and duration.

    Normalises timezone info so that naive datetimes (e.g. from SQLite) are
    treated as UTC and remain comparable with aware datetimes.
    """
    # Normalise lesson times (may be naive in SQLite)
    start = lesson.start_time
    end = lesson.end_time
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    # Normalise join time
    if first_joined_at.tzinfo is None:
        first_joined_at = first_joined_at.replace(tzinfo=timezone.utc)

    lesson_duration = max(1, int((end - start).total_seconds() / 60))
    threshold_minutes = lesson_duration * config.ATTENDANCE_PRESENT_THRESHOLD

    late_cutoff = start + timedelta(minutes=config.ATTENDANCE_LATE_MINUTES)

    is_late = first_joined_at > late_cutoff
    attended_enough = total_minutes >= threshold_minutes

    if attended_enough and not is_late:
        return AttendanceStatus.present
    if attended_enough and is_late:
        return AttendanceStatus.late
    return AttendanceStatus.left_early


async def get_or_create_attendance_record(
    session: AsyncSession, lesson_id: int, user_id: int
) -> AttendanceRecord:
    result = await session.execute(
        select(AttendanceRecord).where(
            and_(
                AttendanceRecord.lesson_id == lesson_id,
                AttendanceRecord.user_id == user_id,
            )
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        record = AttendanceRecord(
            lesson_id=lesson_id,
            user_id=user_id,
            status=AttendanceStatus.unknown,
            total_minutes=0,
        )
        session.add(record)
        await session.flush()
    return record


async def open_voice_session(
    session: AsyncSession,
    discord_user_id: int,
    channel_id: int,
    lesson: Optional[Lesson],
    joined_at: Optional[datetime] = None,
) -> Optional[VoiceSession]:
    """Called when a user joins a voice channel."""
    joined_at = joined_at or now_utc()

    user_result = await session.execute(
        select(User).where(User.discord_user_id == discord_user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        log.debug("Voice join from unknown discord user %d — no record created.", discord_user_id)
        return None

    vs = VoiceSession(
        lesson_id=lesson.id if lesson else None,
        user_id=user.id,
        discord_voice_channel_id=channel_id,
        joined_at=joined_at,
    )
    session.add(vs)
    await session.flush()

    if lesson:
        record = await get_or_create_attendance_record(session, lesson.id, user.id)
        if record.joined_at is None:
            record.joined_at = joined_at
            record.status = AttendanceStatus.unknown
            record.updated_at = now_utc()

    return vs


async def close_voice_session(
    session: AsyncSession,
    discord_user_id: int,
    channel_id: int,
    left_at: Optional[datetime] = None,
) -> Optional[VoiceSession]:
    """Called when a user leaves a voice channel."""
    left_at = left_at or now_utc()

    user_result = await session.execute(
        select(User).where(User.discord_user_id == discord_user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        return None

    # Find the open session for this user in this channel
    vs_result = await session.execute(
        select(VoiceSession)
        .where(
            and_(
                VoiceSession.user_id == user.id,
                VoiceSession.discord_voice_channel_id == channel_id,
                VoiceSession.left_at.is_(None),
            )
        )
        .order_by(VoiceSession.joined_at.desc())
        .limit(1)
    )
    vs = vs_result.scalar_one_or_none()
    if not vs:
        return None

    # SQLite returns naive datetimes; normalise to UTC before subtraction
    # so this is safe in both SQLite (tests) and PostgreSQL (production).
    joined = vs.joined_at
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    duration = max(0, int((left_at - joined).total_seconds() / 60))
    vs.left_at = left_at
    vs.duration_minutes = duration

    if vs.lesson_id:
        record = await get_or_create_attendance_record(session, vs.lesson_id, user.id)
        record.total_minutes = (record.total_minutes or 0) + duration
        # Normalise record.left_at to UTC if SQLite returned a naive datetime
        existing_left = record.left_at
        if existing_left is not None and existing_left.tzinfo is None:
            existing_left = existing_left.replace(tzinfo=timezone.utc)
        if existing_left is None or left_at > existing_left:
            record.left_at = left_at
        record.updated_at = now_utc()

    return vs


async def finalise_lesson_attendance(session: AsyncSession, lesson: Lesson) -> int:
    """
    Called at lesson end. Calculates final statuses and creates absent records
    for all enrolled members (students AND tutors) who never joined.
    Returns count of records processed.
    """
    # Get all active enrolled members (students + tutors)
    members_result = await session.execute(
        select(ClassGroupMember).where(
            and_(
                ClassGroupMember.class_group_id == lesson.class_group_id,
                ClassGroupMember.active.is_(True),
            )
        )
    )
    members = list(members_result.scalars().all())

    count = 0
    for member in members:
        record = await get_or_create_attendance_record(session, lesson.id, member.user_id)

        if record.joined_at is None:
            record.status = AttendanceStatus.absent
        else:
            record.status = calculate_attendance_status(
                lesson, record.joined_at, record.total_minutes or 0
            )
        record.marked_by = MarkedBy.system
        record.updated_at = now_utc()
        count += 1

    lesson.status = LessonStatus.completed
    lesson.updated_at = now_utc()
    log.info("Finalised attendance for lesson %d — %d records", lesson.id, count)
    return count


async def manual_mark_attendance(
    session: AsyncSession,
    lesson_id: int,
    student_discord_id: int,
    status: AttendanceStatus,
    marked_by: MarkedBy = MarkedBy.tutor,
    notes: Optional[str] = None,
) -> Optional[AttendanceRecord]:
    user_result = await session.execute(
        select(User).where(User.discord_user_id == student_discord_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        return None

    record = await get_or_create_attendance_record(session, lesson_id, user.id)
    record.status = status
    record.marked_by = marked_by
    record.notes = notes
    record.updated_at = now_utc()
    return record
