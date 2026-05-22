"""Tests for homework service — CRUD, upcoming due, and missing homework queries."""
import pytest
from datetime import datetime, timezone, timedelta

from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.homework import HomeworkSubmission, HomeworkTask, SubmissionStatus
from bot.models.user import User, UserRole
from bot.services.homework_service import (
    create_homework_task,
    get_homework_task,
    get_missing_homework,
    get_upcoming_homework_due,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _make_tutor(session, discord_id: int = 111_111_111) -> User:
    u = User(discord_user_id=discord_id, full_name="Tutor", role=UserRole.tutor, active=True)
    session.add(u)
    await session.flush()
    return u


async def _make_student(session, discord_id: int = 222_222_222) -> User:
    u = User(discord_user_id=discord_id, full_name="Student", role=UserRole.student, active=True)
    session.add(u)
    await session.flush()
    return u


async def _make_group(session, name: str = "HW Class") -> ClassGroup:
    g = ClassGroup(name=name, active=True)
    session.add(g)
    await session.flush()
    return g


async def _enrol(session, group: ClassGroup, user: User, role: MemberRole = MemberRole.student):
    m = ClassGroupMember(class_group_id=group.id, user_id=user.id, role=role, active=True)
    session.add(m)
    await session.flush()


async def _setup(session) -> tuple[User, User, ClassGroup]:
    tutor = await _make_tutor(session)
    student = await _make_student(session)
    group = await _make_group(session)
    await _enrol(session, group, student)
    return tutor, student, group


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestHomeworkCRUD:
    async def test_create_task_returns_persisted_row(self, db_session):
        tutor, student, group = await _setup(db_session)
        due = _utcnow() + timedelta(days=7)
        task = await create_homework_task(
            db_session, group.id, "Chapter 5 Problems", "Do Q1–10", due, tutor.id
        )
        assert task.id is not None
        assert task.title == "Chapter 5 Problems"
        assert task.class_group_id == group.id

    async def test_create_task_with_no_description(self, db_session):
        tutor, student, group = await _setup(db_session)
        task = await create_homework_task(
            db_session, group.id, "Quick Exercise", None, _utcnow() + timedelta(days=1), tutor.id
        )
        assert task.description is None

    async def test_get_homework_task_returns_correct_row(self, db_session):
        tutor, student, group = await _setup(db_session)
        task = await create_homework_task(
            db_session, group.id, "Fetch Me", None, _utcnow() + timedelta(days=2), tutor.id
        )
        fetched = await get_homework_task(db_session, task.id)
        assert fetched is not None
        assert fetched.id == task.id
        assert fetched.title == "Fetch Me"

    async def test_get_homework_task_nonexistent_returns_none(self, db_session):
        result = await get_homework_task(db_session, 999_999)
        assert result is None


# ---------------------------------------------------------------------------
# Upcoming homework
# ---------------------------------------------------------------------------

class TestUpcomingHomeworkDue:
    async def test_task_due_within_window_is_returned(self, db_session):
        tutor, student, group = await _setup(db_session)
        due_soon = _utcnow() + timedelta(hours=5)
        await create_homework_task(db_session, group.id, "Due Soon", None, due_soon, tutor.id)

        upcoming = await get_upcoming_homework_due(db_session, hours_ahead=25)
        titles = [t.title for t in upcoming]
        assert "Due Soon" in titles

    async def test_task_due_outside_window_is_excluded(self, db_session):
        tutor, student, group = await _setup(db_session)
        due_later = _utcnow() + timedelta(hours=30)
        await create_homework_task(db_session, group.id, "Due Way Later", None, due_later, tutor.id)

        upcoming = await get_upcoming_homework_due(db_session, hours_ahead=25)
        titles = [t.title for t in upcoming]
        assert "Due Way Later" not in titles

    async def test_past_due_task_not_in_upcoming(self, db_session):
        tutor, student, group = await _setup(db_session)
        overdue = _utcnow() - timedelta(hours=1)
        await create_homework_task(db_session, group.id, "Already Overdue", None, overdue, tutor.id)

        upcoming = await get_upcoming_homework_due(db_session, hours_ahead=25)
        titles = [t.title for t in upcoming]
        assert "Already Overdue" not in titles


# ---------------------------------------------------------------------------
# Missing homework
# ---------------------------------------------------------------------------

class TestMissingHomework:
    async def test_missing_homework_detected(self, db_session):
        tutor, student, group = await _setup(db_session)
        due = _utcnow() - timedelta(days=1)
        await create_homework_task(db_session, group.id, "Overdue Task", None, due, tutor.id)

        missing = await get_missing_homework(db_session, group.id)
        assert len(missing) == 1
        assert missing[0]["student"].id == student.id
        assert missing[0]["status"] == SubmissionStatus.missing

    async def test_submitted_homework_not_in_missing(self, db_session):
        tutor, student, group = await _setup(db_session)
        due = _utcnow() - timedelta(days=1)
        task = await create_homework_task(db_session, group.id, "Submitted Task", None, due, tutor.id)

        sub = HomeworkSubmission(
            homework_task_id=task.id,
            student_user_id=student.id,
            status=SubmissionStatus.submitted,
        )
        db_session.add(sub)
        await db_session.flush()

        missing = await get_missing_homework(db_session, group.id)
        assert len(missing) == 0

    async def test_late_submission_appears_in_missing(self, db_session):
        tutor, student, group = await _setup(db_session)
        due = _utcnow() - timedelta(days=1)
        task = await create_homework_task(db_session, group.id, "Late Task", None, due, tutor.id)

        sub = HomeworkSubmission(
            homework_task_id=task.id,
            student_user_id=student.id,
            status=SubmissionStatus.late,
        )
        db_session.add(sub)
        await db_session.flush()

        missing = await get_missing_homework(db_session, group.id)
        # Late submissions should appear (still outstanding concern)
        assert len(missing) == 1
        assert missing[0]["status"] == SubmissionStatus.late

    async def test_no_overdue_tasks_returns_empty_list(self, db_session):
        tutor, student, group = await _setup(db_session)
        missing = await get_missing_homework(db_session, group.id)
        assert missing == []

    async def test_future_task_not_in_missing(self, db_session):
        tutor, student, group = await _setup(db_session)
        due = _utcnow() + timedelta(days=3)
        await create_homework_task(db_session, group.id, "Future Task", None, due, tutor.id)

        missing = await get_missing_homework(db_session, group.id)
        assert missing == []

    async def test_inactive_student_excluded_from_missing(self, db_session):
        tutor = await _make_tutor(db_session, discord_id=333_333_333)
        inactive_student = await _make_student(db_session, discord_id=444_444_444)
        group = await _make_group(db_session, "Inactive Test Class")
        # Enrol then deactivate
        m = ClassGroupMember(class_group_id=group.id, user_id=inactive_student.id, role=MemberRole.student, active=False)
        db_session.add(m)
        await db_session.flush()

        due = _utcnow() - timedelta(days=1)
        await create_homework_task(db_session, group.id, "Task For Inactive", None, due, tutor.id)

        missing = await get_missing_homework(db_session, group.id)
        assert missing == []

    async def test_multiple_students_multiple_tasks(self, db_session):
        tutor = await _make_tutor(db_session, discord_id=555_555_555)
        student_a = await _make_student(db_session, discord_id=666_666_666)
        student_b = await _make_student(db_session, discord_id=777_777_777)
        group = await _make_group(db_session, "Multi Student Group")
        await _enrol(db_session, group, student_a)
        await _enrol(db_session, group, student_b)

        due = _utcnow() - timedelta(days=1)
        await create_homework_task(db_session, group.id, "Task 1", None, due, tutor.id)
        await create_homework_task(db_session, group.id, "Task 2", None, due, tutor.id)

        missing = await get_missing_homework(db_session, group.id)
        # 2 students × 2 tasks = 4 missing entries
        assert len(missing) == 4
