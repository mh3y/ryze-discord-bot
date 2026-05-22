"""GET /api/lessons — list lessons with optional filters."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.api.auth import require_api_key
from bot.api.deps import get_db
from bot.api.schemas import LessonListResponse, LessonSchema
from bot.models.lesson import Lesson, LessonStatus
from bot.models.class_group import ClassGroup

router = APIRouter()


@router.get("/lessons", response_model=LessonListResponse)
async def list_lessons(
    status: str | None = Query(None, description="scheduled | active | completed | cancelled"),
    class_group_id: int | None = Query(None),
    upcoming_only: bool = Query(False, description="Only lessons in the future"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: str = Depends(require_api_key),
):
    q = select(Lesson).options(selectinload(Lesson.class_group))
    if status:
        q = q.where(Lesson.status == status)
    if class_group_id:
        q = q.where(Lesson.class_group_id == class_group_id)
    if upcoming_only:
        q = q.where(Lesson.start_time >= datetime.now(timezone.utc))
    q = q.order_by(Lesson.start_time)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    lessons = result.scalars().all()

    items = [
        LessonSchema(
            id=l.id,
            class_group_id=l.class_group_id,
            class_group_name=l.class_group.name if l.class_group else "",
            title=l.title,
            description=l.description,
            start_time=l.start_time,
            end_time=l.end_time,
            location=l.location,
            meet_link=l.meet_link,
            status=l.status,
            discord_thread_id=l.discord_thread_id,
            google_event_id=l.google_event_id,
        )
        for l in lessons
    ]

    return LessonListResponse(total=total, items=items)
