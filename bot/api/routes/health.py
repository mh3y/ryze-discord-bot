"""GET /api/health — bot + DB liveness check."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import require_api_key
from bot.api.schemas import HealthResponse
from bot.api.deps import get_db
from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User, UserRole

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(
    session: AsyncSession = Depends(get_db),
    _: str = Depends(require_api_key),
):
    db_ok = True
    student_count = 0
    class_count = 0
    upcoming_lessons = 0
    try:
        student_count = (
            await session.scalar(
                select(func.count()).where(
                    User.role == UserRole.student, User.active.is_(True)
                )
            )
        ) or 0
        class_count = (
            await session.scalar(
                select(func.count()).where(ClassGroup.active.is_(True))
            )
        ) or 0
        now = datetime.now(timezone.utc)
        upcoming_lessons = (
            await session.scalar(
                select(func.count()).where(
                    Lesson.start_time >= now,
                    Lesson.status == LessonStatus.scheduled,
                )
            )
        ) or 0
    except Exception:
        db_ok = False

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db_ok=db_ok,
        student_count=student_count,
        class_count=class_count,
        upcoming_lessons=upcoming_lessons,
        timestamp=datetime.now(timezone.utc),
    )
