"""GET /api/classes — list class groups with member counts."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.api.auth import require_api_key
from bot.api.deps import get_db
from bot.api.schemas import ClassGroupListResponse, ClassGroupSchema, TutorSummary
from bot.models.class_group import ClassGroup, ClassGroupMember

router = APIRouter()


@router.get("/classes", response_model=ClassGroupListResponse)
async def list_classes(
    active: bool | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    _: str = Depends(require_api_key),
):
    q = select(ClassGroup).options(selectinload(ClassGroup.tutor))
    if active is not None:
        q = q.where(ClassGroup.active.is_(active))
    q = q.order_by(ClassGroup.name)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    groups = result.scalars().all()

    # Fetch active member counts in one query
    group_ids = [g.id for g in groups]
    count_rows = await session.execute(
        select(ClassGroupMember.class_group_id, func.count().label("cnt"))
        .where(
            ClassGroupMember.class_group_id.in_(group_ids),
            ClassGroupMember.active.is_(True),
        )
        .group_by(ClassGroupMember.class_group_id)
    )
    member_counts: dict[int, int] = {row.class_group_id: row.cnt for row in count_rows}

    items = []
    for g in groups:
        tutor_data = None
        if g.tutor:
            tutor_data = TutorSummary(
                id=g.tutor.id,
                full_name=g.tutor.full_name,
                discord_user_id=g.tutor.discord_user_id,
            )
        items.append(
            ClassGroupSchema(
                id=g.id,
                name=g.name,
                year_level=g.year_level,
                subject=g.subject,
                active=g.active,
                tutor=tutor_data,
                member_count=member_counts.get(g.id, 0),
                created_at=g.created_at,
            )
        )

    return ClassGroupListResponse(total=total, items=items)
