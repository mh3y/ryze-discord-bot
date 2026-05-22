"""Tests for AttendanceCog slash commands and voice event handlers."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from bot.cogs.attendance import AttendanceCog
from bot.models.attendance import AttendanceRecord, AttendanceStatus, MarkedBy, VoiceSession
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User, UserRole

from tests.conftest import (
    make_bot, make_guild, make_member, make_voice_channel, make_interaction,
    session_patch,
)


# ── Helpers ────────────────────────────────────────────────────────────────── #

def _get_embed(interaction):
    call = interaction.followup.send.call_args
    return call.kwargs.get("embed") or (call.kwargs.get("embeds") or [None])[0]


def _get_text(interaction):
    call = interaction.followup.send.call_args
    if call.args:
        return call.args[0]
    return call.kwargs.get("content", "")


async def _seed_class(db_session, class_name="Test Class", voice_channel_id=777):
    tutor = User(discord_user_id=10001, full_name="Test Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(
        name=class_name,
        discord_voice_channel_id=voice_channel_id,
        active=True,
    )
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id
    await db_session.flush()
    return tutor, group


async def _seed_lesson(db_session, group, offset_hours=0, duration_hours=1, status=LessonStatus.active):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=abs(offset_hours)) if offset_hours <= 0 else now + timedelta(hours=offset_hours)
    lesson = Lesson(
        class_group_id=group.id,
        title=f"Lesson for {group.name}",
        start_time=start,
        end_time=start + timedelta(hours=duration_hours),
        status=status,
    )
    db_session.add(lesson)
    await db_session.flush()
    return lesson


async def _seed_student(db_session, discord_id=111222333, name="Test Student"):
    student = User(discord_user_id=discord_id, full_name=name, role=UserRole.student, active=True)
    db_session.add(student)
    await db_session.flush()
    return student


# ── /attendance ──────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_attendance_cmd_lesson_not_found(db_session, bot):
    """A non-existent lesson ID should produce a 'not found' response."""
    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.attendance.callback(cog, interaction, 99999)

    text = _get_text(interaction)
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_attendance_cmd_no_records(db_session, bot):
    """A lesson with no attendance records should return a 'No attendance records yet' message."""
    _, group = await _seed_class(db_session)
    lesson = await _seed_lesson(db_session, group)

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.attendance.callback(cog, interaction, lesson.id)

    text = _get_text(interaction)
    assert "No attendance records" in text


@pytest.mark.asyncio
async def test_attendance_cmd_with_records(db_session, bot):
    """A lesson with attendance records should return an embed whose title contains the class name."""
    _, group = await _seed_class(db_session, class_name="Year 11 Maths")
    lesson = await _seed_lesson(db_session, group)
    student = await _seed_student(db_session)

    record = AttendanceRecord(
        lesson_id=lesson.id,
        user_id=student.id,
        status=AttendanceStatus.present,
        total_minutes=60,
        marked_by=MarkedBy.system,
    )
    db_session.add(record)
    await db_session.flush()

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.attendance.callback(cog, interaction, lesson.id)

    embed = _get_embed(interaction)
    assert embed is not None
    assert "Year 11 Maths" in embed.title


# ── /voice_attendance ────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_voice_attendance_invalid_date(db_session, bot):
    """An invalid date string should produce an error message about format."""
    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.voice_attendance.callback(cog, interaction, "notadate", None)

    text = _get_text(interaction)
    assert "Invalid date" in text or "YYYY-MM-DD" in text


@pytest.mark.asyncio
async def test_voice_attendance_no_lessons(db_session, bot):
    """A valid date with no lessons should return 'No lessons found'."""
    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.voice_attendance.callback(cog, interaction, "2020-01-01", None)

    text = _get_text(interaction)
    assert "No lessons found" in text


@pytest.mark.asyncio
async def test_voice_attendance_with_sessions(db_session, bot):
    """A lesson with voice sessions and attendance records should return an embed with the class name."""
    _, group = await _seed_class(db_session, class_name="Year 10 Science")
    now = datetime.now(timezone.utc)
    lesson = Lesson(
        class_group_id=group.id,
        title="Science Today",
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(minutes=30),
        status=LessonStatus.active,
    )
    db_session.add(lesson)
    await db_session.flush()

    student = await _seed_student(db_session, discord_id=300300300)
    vs = VoiceSession(
        lesson_id=lesson.id,
        user_id=student.id,
        discord_voice_channel_id=777,
        joined_at=now - timedelta(minutes=50),
        left_at=now - timedelta(minutes=5),
        duration_minutes=45,
    )
    rec = AttendanceRecord(
        lesson_id=lesson.id,
        user_id=student.id,
        status=AttendanceStatus.present,
        total_minutes=45,
        marked_by=MarkedBy.system,
    )
    db_session.add_all([vs, rec])
    await db_session.flush()

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    interaction = make_interaction()

    date_str = now.strftime("%Y-%m-%d")
    with session_patch("bot.cogs.attendance", db_session):
        await cog.voice_attendance.callback(cog, interaction, date_str, lesson.id)

    call_kwargs = interaction.followup.send.call_args.kwargs
    embeds = call_kwargs.get("embeds") or [call_kwargs.get("embed")]
    assert embeds and embeds[0] is not None
    assert "Year 10 Science" in embeds[0].title


# ── /mark_attendance ─────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_mark_attendance_user_not_linked(db_session, bot):
    """Marking attendance for a member not in the DB should respond with 'No linked student record found'."""
    _, group = await _seed_class(db_session)
    lesson = await _seed_lesson(db_session, group)

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    student_member = make_member(discord_id=987654321)
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.mark_attendance.callback(cog, interaction, lesson.id, student_member, "present")

    text = _get_text(interaction)
    assert "No linked student record found" in text


@pytest.mark.asyncio
async def test_mark_attendance_success(db_session, bot):
    """Marking attendance for a linked user should succeed and update the DB record."""
    _, group = await _seed_class(db_session)
    lesson = await _seed_lesson(db_session, group)
    student = await _seed_student(db_session, discord_id=456456456)

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    student_member = make_member(discord_id=456456456, display_name="Test Student")
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.mark_attendance.callback(cog, interaction, lesson.id, student_member, "present")

    text = _get_text(interaction)
    assert "present" in text.lower()

    result = await db_session.execute(
        select(AttendanceRecord).where(
            AttendanceRecord.lesson_id == lesson.id,
            AttendanceRecord.user_id == student.id,
        )
    )
    rec = result.scalar_one_or_none()
    assert rec is not None
    assert rec.status == AttendanceStatus.present
    assert rec.marked_by == MarkedBy.tutor


# ── /student_attendance ───────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_student_attendance_not_linked(db_session, bot):
    """Looking up attendance for a member not in the DB should return 'No linked student record found'."""
    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=111111111)
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.student_attendance.callback(cog, interaction, member)

    text = _get_text(interaction)
    assert "No linked student record found" in text


@pytest.mark.asyncio
async def test_student_attendance_no_records(db_session, bot):
    """A linked user with no attendance records should return 'No attendance records found'."""
    student = await _seed_student(db_session, discord_id=222222222)

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=222222222)
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.student_attendance.callback(cog, interaction, member)

    text = _get_text(interaction)
    assert "No attendance records found" in text


@pytest.mark.asyncio
async def test_student_attendance_with_records(db_session, bot):
    """A user with records should return an embed whose title contains their full_name."""
    _, group = await _seed_class(db_session)
    lesson = await _seed_lesson(db_session, group)
    student = await _seed_student(db_session, discord_id=333333333, name="Alice Wonderland")

    record = AttendanceRecord(
        lesson_id=lesson.id,
        user_id=student.id,
        status=AttendanceStatus.present,
        total_minutes=55,
        marked_by=MarkedBy.system,
    )
    db_session.add(record)
    await db_session.flush()

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=333333333)
    interaction = make_interaction()

    with session_patch("bot.cogs.attendance", db_session):
        await cog.student_attendance.callback(cog, interaction, member)

    embed = _get_embed(interaction)
    assert embed is not None
    assert "Alice Wonderland" in embed.title


# ── Voice event handlers ────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_voice_join_unknown_user(db_session, bot):
    """A voice join from an unknown discord user should not crash and create no VoiceSession."""
    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=999111999)
    channel = make_voice_channel(channel_id=5555)
    now = datetime.now(timezone.utc)

    with session_patch("bot.cogs.attendance", db_session):
        await cog._handle_join(member, channel, now)

    result = await db_session.execute(select(VoiceSession))
    sessions = result.scalars().all()
    assert len(sessions) == 0


@pytest.mark.asyncio
async def test_voice_join_creates_session(db_session, bot):
    """A voice join from a known user with no active lesson should create a VoiceSession with lesson_id=None."""
    # No ClassGroup with voice channel 6666 so no lesson will be found
    student = await _seed_student(db_session, discord_id=444444444)
    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=444444444)
    channel = make_voice_channel(channel_id=6666)
    now = datetime.now(timezone.utc)

    with session_patch("bot.cogs.attendance", db_session):
        await cog._handle_join(member, channel, now)

    result = await db_session.execute(select(VoiceSession))
    sessions = result.scalars().all()
    assert len(sessions) == 1
    assert sessions[0].lesson_id is None
    assert sessions[0].user_id == student.id


@pytest.mark.asyncio
async def test_voice_join_links_lesson(db_session, bot):
    """A voice join during an active lesson should create a VoiceSession with lesson_id set and an AttendanceRecord."""
    _, group = await _seed_class(db_session, voice_channel_id=8888)
    now = datetime.now(timezone.utc)
    lesson = Lesson(
        class_group_id=group.id,
        title="Active Lesson",
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=55),
        status=LessonStatus.scheduled,
    )
    db_session.add(lesson)
    student = await _seed_student(db_session, discord_id=555555555)
    await db_session.flush()

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=555555555)
    channel = make_voice_channel(channel_id=8888)

    with session_patch("bot.cogs.attendance", db_session):
        await cog._handle_join(member, channel, now)

    vs_result = await db_session.execute(select(VoiceSession))
    sessions = vs_result.scalars().all()
    assert len(sessions) == 1
    assert sessions[0].lesson_id == lesson.id

    ar_result = await db_session.execute(
        select(AttendanceRecord).where(
            AttendanceRecord.lesson_id == lesson.id,
            AttendanceRecord.user_id == student.id,
        )
    )
    record = ar_result.scalar_one_or_none()
    assert record is not None


@pytest.mark.asyncio
async def test_voice_leave_closes_session(db_session, bot):
    """A voice leave should close the open VoiceSession and accumulate total_minutes on the AttendanceRecord."""
    _, group = await _seed_class(db_session, voice_channel_id=9999)
    now = datetime.now(timezone.utc)
    lesson = Lesson(
        class_group_id=group.id,
        title="Closing Lesson",
        start_time=now - timedelta(minutes=60),
        end_time=now + timedelta(minutes=0),
        status=LessonStatus.active,
    )
    db_session.add(lesson)
    student = await _seed_student(db_session, discord_id=666666666)
    await db_session.flush()

    join_time = now - timedelta(minutes=30)
    vs = VoiceSession(
        lesson_id=lesson.id,
        user_id=student.id,
        discord_voice_channel_id=9999,
        joined_at=join_time,
    )
    record = AttendanceRecord(
        lesson_id=lesson.id,
        user_id=student.id,
        status=AttendanceStatus.unknown,
        total_minutes=0,
        marked_by=MarkedBy.system,
        joined_at=join_time,
    )
    db_session.add_all([vs, record])
    await db_session.flush()

    cog = AttendanceCog(bot)
    cog._finalise_loop.cancel()
    member = make_member(discord_id=666666666)
    channel = make_voice_channel(channel_id=9999)

    with session_patch("bot.cogs.attendance", db_session):
        await cog._handle_leave(member, channel, now)

    vs_result = await db_session.execute(select(VoiceSession).where(VoiceSession.id == vs.id))
    closed_vs = vs_result.scalar_one_or_none()
    assert closed_vs.left_at is not None
    assert closed_vs.duration_minutes == 30

    ar_result = await db_session.execute(
        select(AttendanceRecord).where(
            AttendanceRecord.lesson_id == lesson.id,
            AttendanceRecord.user_id == student.id,
        )
    )
    updated_record = ar_result.scalar_one_or_none()
    assert updated_record.total_minutes == 30
