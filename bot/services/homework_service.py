"""Homework task CRUD and missing homework queries."""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.homework import HomeworkTask, HomeworkSubmission, SubmissionStatus
from bot.models.lesson import Lesson
from bot.models.user import User
from bot.utils.time_utils import now_utc

log = logging.getLogger(__name__)


async def create_homework_task(
    session: AsyncSession,
    class_group_id: int,
    title: str,
    description: Optional[str],
    due_at: datetime,
    created_by_user_id: int,
    lesson_id: Optional[int] = None,
) -> HomeworkTask:
    task = HomeworkTask(
        class_group_id=class_group_id,
        lesson_id=lesson_id,
        title=title,
        description=description,
        due_at=due_at,
        created_by=created_by_user_id,
    )
    session.add(task)
    await session.flush()
    log.info("Created homework task %d: %r", task.id, title)
    return task


async def get_homework_task(session: AsyncSession, task_id: int) -> Optional[HomeworkTask]:
    result = await session.execute(
        select(HomeworkTask)
        .where(HomeworkTask.id == task_id)
        .options(selectinload(HomeworkTask.class_group))
    )
    return result.scalar_one_or_none()


async def get_upcoming_homework_due(session: AsyncSession, hours_ahead: int = 25) -> list[HomeworkTask]:
    """Return homework tasks due within the next *hours_ahead* hours."""
    from datetime import timedelta
    now = now_utc()
    cutoff = now + timedelta(hours=hours_ahead)
    result = await session.execute(
        select(HomeworkTask)
        .where(
            and_(
                HomeworkTask.due_at >= now,
                HomeworkTask.due_at <= cutoff,
            )
        )
        .options(selectinload(HomeworkTask.class_group))
    )
    return list(result.scalars().all())


async def get_missing_homework(
    session: AsyncSession, class_group_id: int
) -> list[dict]:
    """Return list of {student, task, submission_status} for outstanding homework."""
    now = now_utc()

    tasks_result = await session.execute(
        select(HomeworkTask)
        .where(
            and_(
                HomeworkTask.class_group_id == class_group_id,
                HomeworkTask.due_at <= now,
            )
        )
    )
    tasks = list(tasks_result.scalars().all())
    if not tasks:
        return []

    members_result = await session.execute(
        select(User)
        .join(ClassGroupMember, ClassGroupMember.user_id == User.id)
        .where(
            and_(
                ClassGroupMember.class_group_id == class_group_id,
                ClassGroupMember.role == MemberRole.student,
                ClassGroupMember.active.is_(True),
                User.active.is_(True),
            )
        )
    )
    students = list(members_result.scalars().all())

    missing = []
    for task in tasks:
        for student in students:
            sub_result = await session.execute(
                select(HomeworkSubmission).where(
                    and_(
                        HomeworkSubmission.homework_task_id == task.id,
                        HomeworkSubmission.student_user_id == student.id,
                    )
                )
            )
            sub = sub_result.scalar_one_or_none()
            status = sub.status if sub else SubmissionStatus.missing
            if status in (SubmissionStatus.missing, SubmissionStatus.late):
                missing.append({"student": student, "task": task, "status": status})

    return missing
