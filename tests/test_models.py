"""Integration tests — database model CRUD operations via in-memory SQLite."""
import pytest
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.models.attendance import AttendanceRecord, AttendanceStatus, MarkedBy
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.homework import HomeworkTask
from bot.models.lesson import Lesson, LessonStatus
from bot.models.reminder_log import ReminderChannel, ReminderLog
from bot.models.user import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _make_group(session, name: str = "Test Group") -> ClassGroup:
    g = ClassGroup(name=name, active=True)
    session.add(g)
    await session.flush()
    return g


async def _make_lesson(session, group: ClassGroup, title: str = "Lesson") -> Lesson:
    now = _utcnow()
    l = Lesson(
        class_group_id=group.id,
        title=title,
        start_time=now,
        end_time=now + timedelta(hours=1),
        status=LessonStatus.scheduled,
    )
    session.add(l)
    await session.flush()
    return l


async def _make_user(session, discord_id: int, name: str = "User", role: UserRole = UserRole.student) -> User:
    u = User(discord_user_id=discord_id, full_name=name, role=role, active=True)
    session.add(u)
    await session.flush()
    return u


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class TestUserModel:
    async def test_create_student(self, db_session):
        user = await _make_user(db_session, 111222333444555666, "Alice", UserRole.student)
        assert user.id is not None
        assert user.role == UserRole.student
        assert user.active is True

    async def test_create_tutor(self, db_session):
        user = await _make_user(db_session, 222333444555666777, "Jake Tongko", UserRole.tutor)
        assert user.role == UserRole.tutor

    async def test_update_user_role(self, db_session):
        user = await _make_user(db_session, 333444555666777888, "Bob")
        user.role = UserRole.tutor
        await db_session.flush()
        assert user.role == UserRole.tutor

    async def test_user_defaults_active(self, db_session):
        user = User(discord_user_id=444555666777888999, full_name="Carol", role=UserRole.student)
        db_session.add(user)
        await db_session.flush()
        assert user.active is True

    async def test_discord_id_unique_constraint(self, db_session):
        await _make_user(db_session, 999111222333444555, "User A")
        db_session.add(User(discord_user_id=999111222333444555, full_name="User B", role=UserRole.student))
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# ClassGroup model
# ---------------------------------------------------------------------------

class TestClassGroupModel:
    async def test_create_class_group(self, db_session):
        group = ClassGroup(name="Year 11 Advanced", subject="Mathematics", active=True)
        db_session.add(group)
        await db_session.flush()
        assert group.id is not None
        assert group.active is True

    async def test_class_group_defaults(self, db_session):
        group = ClassGroup(name="Year 10")
        db_session.add(group)
        await db_session.flush()
        assert group.google_calendar_id is None
        assert group.discord_role_id is None
        assert group.discord_text_channel_id is None

    async def test_class_group_member_enrolment(self, db_session):
        user = await _make_user(db_session, 555111222333444555, "Student X")
        group = await _make_group(db_session, "Group A")
        member = ClassGroupMember(
            class_group_id=group.id,
            user_id=user.id,
            role=MemberRole.student,
            active=True,
        )
        db_session.add(member)
        await db_session.flush()
        assert member.id is not None
        assert member.role == MemberRole.student

    async def test_class_group_member_role_update(self, db_session):
        user = await _make_user(db_session, 666111222333444555, "Student Y")
        group = await _make_group(db_session, "Group B")
        member = ClassGroupMember(class_group_id=group.id, user_id=user.id, role=MemberRole.student, active=True)
        db_session.add(member)
        await db_session.flush()
        member.role = MemberRole.tutor
        await db_session.flush()
        assert member.role == MemberRole.tutor


# ---------------------------------------------------------------------------
# Lesson model
# ---------------------------------------------------------------------------

class TestLessonModel:
    async def test_create_lesson(self, db_session):
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group, "Algebra Intro")
        assert lesson.id is not None
        assert lesson.status == LessonStatus.scheduled

    async def test_update_lesson_status(self, db_session):
        group = await _make_group(db_session)
        lesson = await _make_lesson(db_session, group)
        lesson.status = LessonStatus.active
        await db_session.flush()
        assert lesson.status == LessonStatus.active

    async def test_lesson_all_statuses(self, db_session):
        group = await _make_group(db_session)
        for status in LessonStatus:
            now = _utcnow()
            l = Lesson(
                class_group_id=group.id,
                title=f"Lesson {status.value}",
                start_time=now,
                end_time=now + timedelta(hours=1),
                status=status,
            )
            db_session.add(l)
        await db_session.flush()

    async def test_google_event_id_unique(self, db_session):
        group = await _make_group(db_session)
        now = _utcnow()
        l1 = Lesson(
            class_group_id=group.id, google_event_id="evt_dup_001",
            title="L1", start_time=now, end_time=now + timedelta(hours=1),
            status=LessonStatus.scheduled,
        )
        db_session.add(l1)
        await db_session.flush()

        l2 = Lesson(
            class_group_id=group.id, google_event_id="evt_dup_001",
            title="L2", start_time=now, end_time=now + timedelta(hours=1),
            status=LessonStatus.scheduled,
        )
        db_session.add(l2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_lesson_with_meet_link(self, db_session):
        group = await _make_group(db_session)
        now = _utcnow()
        lesson = Lesson(
            class_group_id=group.id, title="Online Lesson",
            start_time=now, end_time=now + timedelta(hours=1),
            status=LessonStatus.scheduled,
            meet_link="https://meet.google.com/abc-def-ghi",
        )
        db_session.add(lesson)
        await db_session.flush()
        assert lesson.meet_link == "https://meet.google.com/abc-def-ghi"


# ---------------------------------------------------------------------------
# AttendanceRecord model
# ---------------------------------------------------------------------------

class TestAttendanceModel:
    async def _setup(self, session, discord_id: int = 777888999000111222):
        user = await _make_user(session, discord_id, "Student A")
        group = await _make_group(session, "Attendance Group")
        lesson = await _make_lesson(session, group)
        return user, group, lesson

    async def test_create_attendance_record(self, db_session):
        user, group, lesson = await self._setup(db_session, 777888999000111001)
        record = AttendanceRecord(
            lesson_id=lesson.id, user_id=user.id,
            status=AttendanceStatus.present, total_minutes=55,
        )
        db_session.add(record)
        await db_session.flush()
        assert record.id is not None

    async def test_update_attendance_status(self, db_session):
        user, group, lesson = await self._setup(db_session, 777888999000111002)
        record = AttendanceRecord(
            lesson_id=lesson.id, user_id=user.id,
            status=AttendanceStatus.unknown, total_minutes=0,
        )
        db_session.add(record)
        await db_session.flush()

        record.status = AttendanceStatus.late
        record.total_minutes = 40
        await db_session.flush()
        assert record.status == AttendanceStatus.late
        assert record.total_minutes == 40

    async def test_all_attendance_statuses_persist(self, db_session):
        user, group, lesson = await self._setup(db_session, 777888999000111003)
        # Distinct user_id per row: the uq_attendance_lesson_user constraint (M1)
        # allows only one attendance row per (lesson, user), so vary the user to
        # exercise every status value. (FK enforcement is OFF in the harness.)
        statuses = list(AttendanceStatus)
        for i, status in enumerate(statuses):
            r = AttendanceRecord(lesson_id=lesson.id, user_id=user.id + i, status=status, total_minutes=i * 10)
            db_session.add(r)
        await db_session.flush()
        rows = (
            await db_session.execute(
                select(AttendanceRecord).where(AttendanceRecord.lesson_id == lesson.id)
            )
        ).scalars().all()
        assert {r.status for r in rows} == set(statuses)

    async def test_marked_by_tutor(self, db_session):
        user, group, lesson = await self._setup(db_session, 777888999000111004)
        record = AttendanceRecord(
            lesson_id=lesson.id, user_id=user.id,
            status=AttendanceStatus.absent, total_minutes=0,
            marked_by=MarkedBy.tutor,
        )
        db_session.add(record)
        await db_session.flush()
        assert record.marked_by == MarkedBy.tutor


# ---------------------------------------------------------------------------
# HomeworkTask model
# ---------------------------------------------------------------------------

class TestHomeworkModel:
    async def test_create_homework_task(self, db_session):
        tutor = await _make_user(db_session, 888999000111222333, "Tutor T", UserRole.tutor)
        group = await _make_group(db_session, "HW Group")
        due = _utcnow() + timedelta(days=7)
        task = HomeworkTask(
            class_group_id=group.id,
            title="Practice Problems Ch.5",
            description="Questions 1–20",
            due_at=due,
            created_by=tutor.id,
        )
        db_session.add(task)
        await db_session.flush()
        assert task.id is not None
        assert task.title == "Practice Problems Ch.5"

    async def test_homework_attached_to_lesson(self, db_session):
        tutor = await _make_user(db_session, 999000111222333444, "Tutor U", UserRole.tutor)
        group = await _make_group(db_session, "HW Group 2")
        lesson = await _make_lesson(db_session, group)
        due = _utcnow() + timedelta(days=3)
        task = HomeworkTask(
            class_group_id=group.id,
            lesson_id=lesson.id,
            title="Post-lesson exercise",
            due_at=due,
            created_by=tutor.id,
        )
        db_session.add(task)
        await db_session.flush()
        assert task.lesson_id == lesson.id


# ---------------------------------------------------------------------------
# ReminderLog model
# ---------------------------------------------------------------------------

class TestReminderLogModel:
    async def test_create_reminder_log(self, db_session):
        """ReminderLog can be persisted (FK enforcement is off in tests)."""
        now = _utcnow()
        log = ReminderLog(
            lesson_id=999,  # FK not enforced in SQLite test mode
            reminder_type="15min",
            channel=ReminderChannel.dm,
            sent_at=now,
            success=True,
        )
        db_session.add(log)
        await db_session.flush()
        assert log.id is not None

    async def test_failed_reminder_stores_error(self, db_session):
        now = _utcnow()
        log = ReminderLog(
            lesson_id=999,
            reminder_type="1h",
            channel=ReminderChannel.dm,
            sent_at=now,
            success=False,
            error_message="DMs disabled by user",
        )
        db_session.add(log)
        await db_session.flush()
        assert log.success is False
        assert log.error_message == "DMs disabled by user"

    async def test_both_channels_can_be_stored(self, db_session):
        now = _utcnow()
        for channel in ReminderChannel:
            log = ReminderLog(
                lesson_id=999,
                reminder_type="24h",
                channel=channel,
                sent_at=now,
                success=True,
            )
            db_session.add(log)
        await db_session.flush()
