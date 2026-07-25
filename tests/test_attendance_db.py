"""DB-integrated tests for voice session tracking and attendance finalisation."""
import pytest
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, and_

from bot.models.attendance import AttendanceRecord, AttendanceStatus, VoiceSession
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User, UserRole
from bot.services.attendance_service import (
    close_open_sessions_for_lesson,
    close_voice_session,
    finalise_lesson_attendance,
    get_or_create_attendance_record,
    open_voice_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHANNEL = 888_777_666_555  # Fake Discord voice channel ID


async def _make_student(session, discord_id: int, name: str = "Student") -> User:
    u = User(discord_user_id=discord_id, full_name=name, role=UserRole.student, active=True)
    session.add(u)
    await session.flush()
    return u


async def _make_group(session, name: str = "Test Class") -> ClassGroup:
    g = ClassGroup(name=name, active=True)
    session.add(g)
    await session.flush()
    return g


async def _make_lesson(session, group: ClassGroup) -> Lesson:
    now = datetime.now(timezone.utc)
    l = Lesson(
        class_group_id=group.id,
        title="Test Lesson",
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=55),
        status=LessonStatus.active,
    )
    session.add(l)
    await session.flush()
    return l


async def _enrol(session, group: ClassGroup, user: User, role: MemberRole = MemberRole.student) -> ClassGroupMember:
    m = ClassGroupMember(class_group_id=group.id, user_id=user.id, role=role, active=True)
    session.add(m)
    await session.flush()
    return m


async def _get_attendance(session, lesson_id: int, user_id: int):
    result = await session.execute(
        select(AttendanceRecord).where(
            and_(AttendanceRecord.lesson_id == lesson_id, AttendanceRecord.user_id == user_id)
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Voice session open / close
# ---------------------------------------------------------------------------

class TestVoiceSessionTracking:
    async def test_open_session_for_known_user_creates_record(self, db_session):
        student = await _make_student(db_session, 100_200_300, "Alice")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)

        vs = await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson)

        assert vs is not None
        assert vs.user_id == student.id
        assert vs.lesson_id == lesson.id

    async def test_open_session_for_unknown_discord_user_returns_none(self, db_session):
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)

        vs = await open_voice_session(db_session, 999_888_777_666, _CHANNEL, lesson)
        assert vs is None

    async def test_open_session_creates_attendance_record(self, db_session):
        student = await _make_student(db_session, 200_300_400, "Bob")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)

        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson)

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record is not None
        assert record.joined_at is not None

    async def test_close_session_calculates_duration(self, db_session):
        student = await _make_student(db_session, 300_400_500, "Carol")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        joined_at = datetime.now(timezone.utc)
        left_at = joined_at + timedelta(minutes=45)

        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=joined_at)
        vs = await close_voice_session(db_session, student.discord_user_id, _CHANNEL, left_at=left_at)

        assert vs is not None
        assert vs.duration_minutes == 45
        assert vs.left_at is not None

    async def test_close_session_accumulates_total_minutes(self, db_session):
        student = await _make_student(db_session, 400_500_600, "Dave")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        now = datetime.now(timezone.utc)

        # First session: 20 minutes
        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=now)
        await close_voice_session(db_session, student.discord_user_id, _CHANNEL, left_at=now + timedelta(minutes=20))

        # Second session (rejoin): 25 minutes
        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=now + timedelta(minutes=25))
        await close_voice_session(db_session, student.discord_user_id, _CHANNEL, left_at=now + timedelta(minutes=50))

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record is not None
        assert record.total_minutes == 45  # 20 + 25

    async def test_open_session_without_lesson_does_not_crash(self, db_session):
        student = await _make_student(db_session, 500_600_700, "Eve")
        vs = await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson=None)
        assert vs is not None
        assert vs.lesson_id is None

    async def test_close_session_for_no_open_session_returns_none(self, db_session):
        student = await _make_student(db_session, 600_700_800, "Frank")
        result = await close_voice_session(db_session, student.discord_user_id, _CHANNEL)
        assert result is None


# ---------------------------------------------------------------------------
# get_or_create_attendance_record
# ---------------------------------------------------------------------------

class TestGetOrCreateAttendanceRecord:
    async def test_creates_record_when_missing(self, db_session):
        student = await _make_student(db_session, 700_800_900, "George")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)

        record = await get_or_create_attendance_record(db_session, lesson.id, student.id)
        assert record.id is not None
        assert record.status == AttendanceStatus.unknown

    async def test_returns_existing_record_on_second_call(self, db_session):
        student = await _make_student(db_session, 800_900_000, "Hannah")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)

        r1 = await get_or_create_attendance_record(db_session, lesson.id, student.id)
        r2 = await get_or_create_attendance_record(db_session, lesson.id, student.id)
        assert r1.id == r2.id


# ---------------------------------------------------------------------------
# Finalise lesson attendance
# ---------------------------------------------------------------------------

class TestFinaliseAttendance:
    async def test_absent_student_marked_absent(self, db_session):
        student = await _make_student(db_session, 900_000_111, "Imelda")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        await _enrol(db_session, group, student)

        # Student never joins → should be marked absent
        count = await finalise_lesson_attendance(db_session, lesson)
        assert count == 1

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.status == AttendanceStatus.absent

    async def test_student_with_enough_time_marked_present(self, db_session):
        student = await _make_student(db_session, 111_000_222, "James")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        await _enrol(db_session, group, student)

        # Attend 50 of 60-minute lesson (> 70% threshold)
        start = lesson.start_time
        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=start)
        await close_voice_session(db_session, student.discord_user_id, _CHANNEL, left_at=start + timedelta(minutes=50))

        await finalise_lesson_attendance(db_session, lesson)

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.status == AttendanceStatus.present

    async def test_student_joined_late_marked_late(self, db_session):
        student = await _make_student(db_session, 222_000_333, "Kate")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        await _enrol(db_session, group, student)

        # Join 20 minutes late, stay 42 of 60 minutes (> 70%) — should be "late"
        start = lesson.start_time
        joined_late = start + timedelta(minutes=20)
        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=joined_late)
        await close_voice_session(db_session, student.discord_user_id, _CHANNEL, left_at=joined_late + timedelta(minutes=42))

        await finalise_lesson_attendance(db_session, lesson)

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.status == AttendanceStatus.late

    async def test_student_left_early_marked_left_early(self, db_session):
        student = await _make_student(db_session, 333_000_444, "Liam")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        await _enrol(db_session, group, student)

        # On time but only attended 20 of 60 minutes (< 70%)
        start = lesson.start_time
        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=start)
        await close_voice_session(db_session, student.discord_user_id, _CHANNEL, left_at=start + timedelta(minutes=20))

        await finalise_lesson_attendance(db_session, lesson)

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.status == AttendanceStatus.left_early

    async def test_present_student_still_in_channel_at_end_marked_present(self, db_session):
        """DEF-8 regression: a student still in voice when the lesson ends has an
        OPEN session (left_at is None, total_minutes still 0). Finalise must close
        it and credit the time, marking them present — not left_early."""
        student = await _make_student(db_session, 666_000_777, "Olivia")
        group = await _make_group(db_session)
        now = datetime.now(timezone.utc)
        # Lesson ran 60 minutes and ended 5 minutes ago; still marked active.
        lesson = Lesson(
            class_group_id=group.id,
            title="Ended lesson",
            start_time=now - timedelta(minutes=65),
            end_time=now - timedelta(minutes=5),
            status=LessonStatus.active,
        )
        db_session.add(lesson)
        await db_session.flush()
        await _enrol(db_session, group, student)

        # Joins on time and NEVER leaves — the leave event was missed / bot restart.
        await open_voice_session(
            db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=lesson.start_time
        )

        await finalise_lesson_attendance(db_session, lesson)

        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.status == AttendanceStatus.present
        # Credited from join (start) to the lesson end — the full 60 minutes.
        assert record.total_minutes == 60

        # The dangling open session was closed at the lesson end.
        vs_result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.lesson_id == lesson.id)
        )
        vs = vs_result.scalar_one()
        assert vs.left_at is not None
        assert vs.duration_minutes == 60

    async def test_still_present_after_prior_rejoin_folds_both_segments(self, db_session):
        """A student who left, rejoined, and is still in the channel at end gets
        both the closed segment and the still-open segment counted."""
        student = await _make_student(db_session, 999_000_111, "Rita")
        group = await _make_group(db_session)
        now = datetime.now(timezone.utc)
        lesson = Lesson(
            class_group_id=group.id,
            title="Ended lesson",
            start_time=now - timedelta(minutes=65),
            end_time=now - timedelta(minutes=5),
            status=LessonStatus.active,
        )
        db_session.add(lesson)
        await db_session.flush()
        await _enrol(db_session, group, student)
        start = lesson.start_time

        # First segment: 20 minutes, closed normally.
        await open_voice_session(db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=start)
        await close_voice_session(
            db_session, student.discord_user_id, _CHANNEL, left_at=start + timedelta(minutes=20)
        )
        # Rejoined 25 minutes in and never left.
        await open_voice_session(
            db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=start + timedelta(minutes=25)
        )

        await finalise_lesson_attendance(db_session, lesson)

        record = await _get_attendance(db_session, lesson.id, student.id)
        # 20 (closed) + 35 (min 25 → end at min 60) = 55 of 60 → present.
        assert record.total_minutes == 55
        assert record.status == AttendanceStatus.present

    async def test_lesson_status_set_to_completed(self, db_session):
        student = await _make_student(db_session, 444_000_555, "Mia")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        await _enrol(db_session, group, student)

        await finalise_lesson_attendance(db_session, lesson)
        assert lesson.status == LessonStatus.completed

    async def test_no_enrolled_students_returns_zero(self, db_session):
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        # No members enrolled
        count = await finalise_lesson_attendance(db_session, lesson)
        assert count == 0

    async def test_unenrolled_member_not_counted_as_absent(self, db_session):
        """A user whose ClassGroupMember.active=False should not appear in the report."""
        student = await _make_student(db_session, 555_000_666, "Noah")
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        # Enrol but then deactivate
        m = await _enrol(db_session, group, student)
        m.active = False
        await db_session.flush()

        count = await finalise_lesson_attendance(db_session, lesson)
        assert count == 0


# ---------------------------------------------------------------------------
# close_open_sessions_for_lesson (DEF-8 helper)
# ---------------------------------------------------------------------------

class TestCloseOpenSessions:
    async def test_returns_zero_when_no_open_sessions(self, db_session):
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        closed = await close_open_sessions_for_lesson(
            db_session, lesson, datetime.now(timezone.utc)
        )
        assert closed == 0

    async def test_caps_credit_at_close_at_not_beyond(self, db_session):
        """A still-open session is credited only up to close_at (min(now, end)),
        never for time the student has not yet been present."""
        student = await _make_student(db_session, 777_000_888, "Pete")
        group = await _make_group(db_session)
        now = datetime.now(timezone.utc)
        lesson = Lesson(
            class_group_id=group.id,
            title="Ongoing lesson",
            start_time=now - timedelta(minutes=30),
            end_time=now + timedelta(minutes=30),  # ends in the future
            status=LessonStatus.active,
        )
        db_session.add(lesson)
        await db_session.flush()
        await open_voice_session(
            db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=lesson.start_time
        )

        # Close mid-lesson at `now`: 30 minutes elapsed, not the 60-min full run.
        closed = await close_open_sessions_for_lesson(db_session, lesson, now)
        assert closed == 1
        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.total_minutes == 30

    async def test_close_before_join_credits_zero_not_negative(self, db_session):
        """If close_at precedes joined_at (clock skew), duration is clamped to 0."""
        student = await _make_student(db_session, 888_000_999, "Quinn")
        group = await _make_group(db_session)
        now = datetime.now(timezone.utc)
        lesson = Lesson(
            class_group_id=group.id,
            title="Lesson",
            start_time=now - timedelta(minutes=60),
            end_time=now,
            status=LessonStatus.active,
        )
        db_session.add(lesson)
        await db_session.flush()
        # Joined at `now`, but we close at now - 10min.
        await open_voice_session(
            db_session, student.discord_user_id, _CHANNEL, lesson, joined_at=now
        )

        closed = await close_open_sessions_for_lesson(
            db_session, lesson, now - timedelta(minutes=10)
        )
        assert closed == 1
        record = await _get_attendance(db_session, lesson.id, student.id)
        assert record.total_minutes == 0
