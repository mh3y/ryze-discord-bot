"""GET /api/attendance — attendance records for the dashboard."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.api.auth import require_api_key
from bot.api.deps import get_db
from bot.api.schemas import AttendanceListResponse, AttendanceSchema
from bot.models.attendance import AttendanceRecord

router = APIRouter()


@router.get("/attendance", response_model=AttendanceListResponse)
async def list_attendance(
    lesson_id: int | None = Query(None),
    user_id: int | None = Query(None),
    status: str | None = Query(None, description="present | late | left_early | absent | unknown"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: str = Depends(require_api_key),
):
    q = (
        select(AttendanceRecord)
        .options(
            selectinload(AttendanceRecord.lesson),
            selectinload(AttendanceRecord.user),
        )
        .order_by(AttendanceRecord.id.desc())
    )
    if lesson_id:
        q = q.where(AttendanceRecord.lesson_id == lesson_id)
    if user_id:
        q = q.where(AttendanceRecord.user_id == user_id)
    if status:
        q = q.where(AttendanceRecord.status == status)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    records = result.scalars().all()

    items = [
        AttendanceSchema(
            id=r.id,
            lesson_id=r.lesson_id,
            lesson_title=r.lesson.title if r.lesson else "",
            user_id=r.user_id,
            student_name=r.user.full_name if r.user else "",
            status=r.status,
            joined_at=r.joined_at,
            left_at=r.left_at,
            duration_minutes=r.total_minutes or None,
        )
        for r in records
    ]

    return AttendanceListResponse(total=total, items=items)
