"""Parent portal endpoints.

GET /api/parent/portal       Comprehensive dashboard payload for the logged-in parent.
GET /api/parent/children     List of linked children with upcoming lessons.
GET /api/parent/children/{student_id}   One child's full detail.

All routes require a parent JWT (role == "parent").
"""
import datetime as _dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.api.auth import AuthUser, require_parent
from bot.api.deps import get_db
from bot.models.portal_auth import StudentParentLink, ParentProfile
from bot.models.user import User
from bot.models.class_group import ClassGroup, ClassGroupMember
from bot.models.lesson import Lesson, LessonStatus
from bot.models.attendance import AttendanceRecord
from bot.models.payment import StudentPayment, PaymentStatus
from bot.models.operations import ProgressReport, ReportStatus

router = APIRouter(prefix="/parent", tags=["Parent Portal"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)


def _format_payment_status(s) -> str:
    return s.value if hasattr(s, 'value') else str(s)


# ---------------------------------------------------------------------------
# GET /api/parent/portal  — one-shot dashboard payload
# ---------------------------------------------------------------------------

@router.get("/portal")
async def parent_portal(
    current: AuthUser = Depends(require_parent),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return a comprehensive payload for the parent dashboard:
    - Parent profile
    - Linked children with their classes, upcoming lessons, recent attendance,
      payment summary, and visible progress reports.
    """
    parent_id = current.parent_profile_id

    # Fetch parent profile
    parent = await session.get(ParentProfile, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent profile not found.")

    # Fetch linked student IDs
    link_rows = await session.execute(
        select(StudentParentLink).where(StudentParentLink.parent_profile_id == parent_id)
    )
    links = link_rows.scalars().all()
    student_ids = [lnk.student_user_id for lnk in links]

    if not student_ids:
        return {
            "parent": {
                "id": parent.id,
                "full_name": parent.full_name,
                "email": parent.email,
                "phone": parent.phone,
            },
            "children": [],
        }

    # Fetch all students
    student_rows = await session.execute(
        select(User)
        .options(
            selectinload(User.class_memberships).selectinload(ClassGroupMember.class_group),
        )
        .where(User.id.in_(student_ids), User.active.is_(True))
    )
    students = {s.id: s for s in student_rows.scalars().all()}

    # Upcoming lessons for all linked students (next 14 days)
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now + _dt.timedelta(days=14)

    # Get class group IDs for all students' active memberships
    all_class_ids: list[int] = []
    for student in students.values():
        for m in (student.class_memberships or []):
            if m.active:
                all_class_ids.append(m.class_group_id)

    upcoming_lessons: list[Lesson] = []
    if all_class_ids:
        lesson_rows = await session.execute(
            select(Lesson)
            .options(selectinload(Lesson.class_group))
            .where(
                Lesson.class_group_id.in_(all_class_ids),
                Lesson.start_time >= now,
                Lesson.start_time <= cutoff,
                Lesson.status.in_([LessonStatus.scheduled, LessonStatus.active]),
            )
            .order_by(Lesson.start_time)
            .limit(20)
        )
        upcoming_lessons = lesson_rows.scalars().all()

    # Recent attendance (last 30 days) for all students
    thirty_ago = now - _dt.timedelta(days=30)
    attendance_rows = await session.execute(
        select(AttendanceRecord)
        .options(selectinload(AttendanceRecord.lesson))
        .where(
            AttendanceRecord.user_id.in_(student_ids),
            AttendanceRecord.lesson.has(Lesson.start_time >= thirty_ago),
        )
        .order_by(AttendanceRecord.id.desc())
        .limit(50)
    )
    attendance_records = attendance_rows.scalars().all()
    attendance_by_student: dict[int, list] = {sid: [] for sid in student_ids}
    for ar in attendance_records:
        if ar.user_id in attendance_by_student:
            attendance_by_student[ar.user_id].append({
                "id": ar.id,
                "lesson_title": ar.lesson.title if ar.lesson else "Unknown",
                "lesson_start": _iso(ar.lesson.start_time) if ar.lesson else None,
                "status": ar.status.value if hasattr(ar.status, 'value') else str(ar.status),
            })

    # Payment records for all students
    payment_rows = await session.execute(
        select(StudentPayment)
        .where(StudentPayment.student_user_id.in_(student_ids))
        .order_by(StudentPayment.created_at.desc())
        .limit(50)
    )
    payments = payment_rows.scalars().all()
    payments_by_student: dict[int, list] = {sid: [] for sid in student_ids}
    for p in payments:
        if p.student_user_id in payments_by_student:
            payments_by_student[p.student_user_id].append({
                "id": p.id,
                "term": p.term,
                "amount_due": str(p.amount_due),
                "amount_paid": str(p.amount_paid),
                "amount_remaining": str(p.amount_remaining),
                "status": _format_payment_status(p.status),
                "due_date": _iso(p.due_date),
                "paid_at": _iso(p.paid_at),
            })

    # Visible progress reports for all students
    report_rows = await session.execute(
        select(ProgressReport)
        .where(
            ProgressReport.student_user_id.in_(student_ids),
            ProgressReport.visible_to_parent.is_(True),
        )
        .order_by(ProgressReport.submitted_at.desc())
        .limit(20)
    )
    reports = report_rows.scalars().all()
    reports_by_student: dict[int, list] = {sid: [] for sid in student_ids}
    for r in reports:
        if r.student_user_id in reports_by_student:
            reports_by_student[r.student_user_id].append({
                "id": r.id,
                "tutor_name": r.tutor_name if hasattr(r, 'tutor_name') else None,
                "class_name": r.class_name if hasattr(r, 'class_name') else None,
                "summary": r.summary,
                "status": r.status.value if hasattr(r.status, 'value') else str(r.status),
                "submitted_at": _iso(r.submitted_at),
            })

    # Build per-child upcoming lessons (only lessons relevant to their classes)
    child_class_ids: dict[int, set[int]] = {}
    for student in students.values():
        child_class_ids[student.id] = {
            m.class_group_id for m in (student.class_memberships or []) if m.active
        }

    upcoming_by_student: dict[int, list] = {sid: [] for sid in student_ids}
    for ls in upcoming_lessons:
        for sid, cids in child_class_ids.items():
            if ls.class_group_id in cids:
                upcoming_by_student[sid].append({
                    "id": ls.id,
                    "title": ls.title,
                    "class_name": ls.class_group.name if ls.class_group else None,
                    "start_time": _iso(ls.start_time),
                    "end_time": _iso(ls.end_time),
                    "status": ls.status.value,
                    "meet_link": ls.meet_link,
                    "location": ls.location,
                })

    # Assemble children
    children = []
    link_map = {lnk.student_user_id: lnk for lnk in links}
    for sid in student_ids:
        student = students.get(sid)
        if not student:
            continue
        lnk = link_map[sid]
        classes = []
        for m in (student.class_memberships or []):
            if m.active and m.class_group:
                classes.append({
                    "id": m.class_group.id,
                    "name": m.class_group.name,
                    "year_level": m.class_group.year_level,
                    "subject": m.class_group.subject,
                    "enrollment_status": m.enrollment_status.value if hasattr(m.enrollment_status, 'value') else str(m.enrollment_status),
                })

        children.append({
            "link_id": lnk.id,
            "relationship": lnk.relationship,
            "is_primary_contact": lnk.is_primary_contact,
            "student": {
                "id": student.id,
                "full_name": student.full_name,
                "email": student.email,
            },
            "classes": classes,
            "upcoming_lessons": upcoming_by_student.get(sid, []),
            "recent_attendance": attendance_by_student.get(sid, []),
            "payments": payments_by_student.get(sid, []),
            "progress_reports": reports_by_student.get(sid, []),
        })

    return {
        "parent": {
            "id": parent.id,
            "full_name": parent.full_name,
            "email": parent.email,
            "phone": parent.phone,
        },
        "children": children,
    }
