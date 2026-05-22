"""GET /api/students — list all users."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import require_api_key
from bot.api.deps import get_db
from bot.api.schemas import UserListResponse, UserSchema
from bot.models.user import User

router = APIRouter()


@router.get("/students", response_model=UserListResponse)
async def list_students(
    role: str | None = Query(None, description="Filter by role: student, tutor, admin"),
    active: bool | None = Query(None, description="Filter by active status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: str = Depends(require_api_key),
):
    q = select(User)
    if role:
        q = q.where(User.role == role)
    if active is not None:
        q = q.where(User.active.is_(active))
    q = q.order_by(User.full_name)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    users = result.scalars().all()

    return UserListResponse(
        total=total,
        items=[UserSchema.model_validate(u) for u in users],
    )
