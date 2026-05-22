"""GET /api/reminders — reminder log entries."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.api.auth import require_api_key
from bot.api.deps import get_db
from bot.api.schemas import ReminderLogListResponse, ReminderLogSchema
from bot.models.reminder_log import ReminderLog

router = APIRouter()


@router.get("/reminders", response_model=ReminderLogListResponse)
async def list_reminders(
    lesson_id: int | None = Query(None),
    reminder_type: str | None = Query(None, description="24h | 1h | 15min | cancelled"),
    success: bool | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: str = Depends(require_api_key),
):
    q = (
        select(ReminderLog)
        .options(
            selectinload(ReminderLog.lesson),
            selectinload(ReminderLog.user),
        )
        .order_by(ReminderLog.sent_at.desc())
    )
    if lesson_id:
        q = q.where(ReminderLog.lesson_id == lesson_id)
    if reminder_type:
        q = q.where(ReminderLog.reminder_type == reminder_type)
    if success is not None:
        q = q.where(ReminderLog.success.is_(success))

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    logs = result.scalars().all()

    items = [
        ReminderLogSchema(
            id=r.id,
            lesson_id=r.lesson_id,
            lesson_title=r.lesson.title if r.lesson else None,
            user_id=r.user_id,
            reminder_type=r.reminder_type,
            channel=r.channel,
            sent_at=r.sent_at,
            success=r.success,
            error_message=r.error_message,
        )
        for r in logs
    ]

    return ReminderLogListResponse(total=total, items=items)
