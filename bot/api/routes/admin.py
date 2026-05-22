"""Admin write endpoints for the Ryze Education portal.

All routes require JWT authentication.
- Admin-only endpoints: Depends(require_admin)
- Admin + Tutor endpoints: Depends(require_admin_or_tutor)

Prefix: /api/admin  (mounted in app.py)
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.api.auth import AuthUser, require_admin, require_admin_or_tutor
from bot.api.deps import get_db
from bot import config
from bot.models.user import User, UserRole
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole, EnrollmentStatus
from bot.models.portal_auth import ParentProfile, PortalCredential, StudentParentLink, StudentProfile
from bot.models.payment import (
    StudentPayment,
    StudentPaymentLog,
    TutorPayment,
    TutorPaymentItem,
    PaymentStatus,
    TutorPaymentStatus,
)
from bot.models.operations import (
    ProgressReport,
    ReportStatus,
    SystemAlert,
    AlertStatus,
    AlertSeverity,
    Announcement,
    AnnouncementStatus,
    Resource,
    ResourceType,
)
from bot.models.lesson import Lesson, LessonStatus
from bot.models.attendance import AttendanceRecord, AttendanceStatus, DiscordVerificationStatus, MarkedBy
from bot.models.homework import HomeworkTask, HomeworkSubmission, SubmissionStatus

router = APIRouter()

utc = timezone.utc


# ===========================================================================
# 1. PARENT MANAGEMENT (admin only)
# ===========================================================================

class CreateParentRequest(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    notes: Optional[str] = None


class UpdateParentRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    active: Optional[bool] = None


class LinkStudentRequest(BaseModel):
    student_user_id: int
    relationship: Optional[str] = "guardian"
    is_primary_contact: Optional[bool] = False


@router.post("/parents", status_code=status.HTTP_201_CREATED)
async def create_parent(
    body: CreateParentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a parent profile and generate an invite link."""
    # Check email uniqueness
    existing = await session.scalar(
        select(ParentProfile).where(ParentProfile.email == body.email)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A parent with email '{body.email}' already exists.",
        )

    parent = ParentProfile(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        phone=body.phone,
        notes=body.notes,
    )
    session.add(parent)
    await session.flush()  # get parent.id before creating credential

    invite_token = secrets.token_urlsafe(32)
    invite_expires_at = datetime.now(utc) + timedelta(hours=48)

    cred = PortalCredential(
        parent_profile_id=parent.id,
        invite_token=invite_token,
        invite_expires_at=invite_expires_at,
    )
    session.add(cred)
    await session.flush()

    invite_link = f"{config.PORTAL_BASE_URL}/auth/invite?token={invite_token}"

    return {
        "id": parent.id,
        "full_name": parent.full_name,
        "email": parent.email,
        "phone": parent.phone,
        "active": parent.active,
        "created_at": parent.created_at.isoformat(),
        "invite_link": invite_link,
        "invite_expires_at": invite_expires_at.isoformat(),
    }


@router.get("/parents")
async def list_parents(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    active: Optional[bool] = Query(None),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List all parent profiles (paginated)."""
    q = select(ParentProfile).options(selectinload(ParentProfile.credential))
    if active is not None:
        q = q.where(ParentProfile.active.is_(active))
    q = q.order_by(ParentProfile.last_name, ParentProfile.first_name)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    parents = result.scalars().all()

    items = [
        {
            "id": p.id,
            "full_name": p.full_name,
            "email": p.email,
            "phone": p.phone,
            "active": p.active,
            "created_at": p.created_at.isoformat(),
            "invite_pending": p.credential.invite_pending if p.credential else False,
            "has_set_password": p.credential.has_set_password if p.credential else False,
        }
        for p in parents
    ]
    return {"total": total, "items": items}


@router.get("/parents/{parent_id}")
async def get_parent(
    parent_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Get a parent profile with their linked students."""
    result = await session.execute(
        select(ParentProfile)
        .options(
            selectinload(ParentProfile.credential),
            selectinload(ParentProfile.student_links).selectinload(StudentParentLink.student),
        )
        .where(ParentProfile.id == parent_id)
    )
    parent = result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found.")

    students = [
        {
            "link_id": link.id,
            "student_user_id": link.student_user_id,
            "student_name": link.student.full_name if link.student else None,
            "relationship": link.relationship_type.value if link.relationship_type else None,
            "is_primary_contact": link.is_primary_contact,
        }
        for link in parent.student_links
    ]

    cred = parent.credential
    return {
        "id": parent.id,
        "full_name": parent.full_name,
        "first_name": parent.first_name,
        "last_name": parent.last_name,
        "email": parent.email,
        "phone": parent.phone,
        "notes": parent.notes,
        "active": parent.active,
        "created_at": parent.created_at.isoformat(),
        "updated_at": parent.updated_at.isoformat(),
        "invite_pending": cred.invite_pending if cred else False,
        "has_set_password": cred.has_set_password if cred else False,
        "last_login_at": cred.last_login_at.isoformat() if cred and cred.last_login_at else None,
        "students": students,
    }


@router.patch("/parents/{parent_id}")
async def update_parent(
    parent_id: int,
    body: UpdateParentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update parent profile fields."""
    parent = await session.get(ParentProfile, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found.")

    if body.first_name is not None:
        parent.first_name = body.first_name
    if body.last_name is not None:
        parent.last_name = body.last_name
    if body.email is not None:
        # Check uniqueness excluding self
        conflict = await session.scalar(
            select(ParentProfile).where(
                and_(ParentProfile.email == body.email, ParentProfile.id != parent_id)
            )
        )
        if conflict:
            raise HTTPException(status_code=409, detail="Email already in use by another parent.")
        parent.email = body.email
    if body.phone is not None:
        parent.phone = body.phone
    if body.notes is not None:
        parent.notes = body.notes
    if body.active is not None:
        parent.active = body.active

    parent.updated_at = datetime.now(utc)
    await session.flush()

    return {"id": parent.id, "full_name": parent.full_name, "updated": True}


@router.post("/parents/{parent_id}/resend-invite", status_code=status.HTTP_200_OK)
async def resend_parent_invite(
    parent_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Generate a fresh invite token for a parent."""
    result = await session.execute(
        select(ParentProfile)
        .options(selectinload(ParentProfile.credential))
        .where(ParentProfile.id == parent_id)
    )
    parent = result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found.")

    invite_token = secrets.token_urlsafe(32)
    invite_expires_at = datetime.now(utc) + timedelta(hours=48)

    if parent.credential:
        cred = parent.credential
        cred.invite_token = invite_token
        cred.invite_expires_at = invite_expires_at
        # Resetting invite clears any previous accepted state
        cred.invite_accepted_at = None
    else:
        cred = PortalCredential(
            parent_profile_id=parent.id,
            invite_token=invite_token,
            invite_expires_at=invite_expires_at,
        )
        session.add(cred)

    await session.flush()
    invite_link = f"{config.PORTAL_BASE_URL}/auth/invite?token={invite_token}"

    return {
        "parent_id": parent.id,
        "invite_link": invite_link,
        "invite_expires_at": invite_expires_at.isoformat(),
    }


@router.post("/parents/{parent_id}/link-student", status_code=status.HTTP_201_CREATED)
async def link_student_to_parent(
    parent_id: int,
    body: LinkStudentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Link a student user to a parent profile."""
    parent = await session.get(ParentProfile, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found.")

    student = await session.get(User, body.student_user_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student user not found.")

    # Check for duplicate link
    existing_link = await session.scalar(
        select(StudentParentLink).where(
            and_(
                StudentParentLink.student_user_id == body.student_user_id,
                StudentParentLink.parent_profile_id == parent_id,
            )
        )
    )
    if existing_link:
        raise HTTPException(status_code=409, detail="Student is already linked to this parent.")

    from bot.models.portal_auth import ParentRelationship
    try:
        rel = ParentRelationship(body.relationship or "guardian")
    except ValueError:
        rel = ParentRelationship.guardian

    link = StudentParentLink(
        student_user_id=body.student_user_id,
        parent_profile_id=parent_id,
        relationship=rel,
        is_primary_contact=body.is_primary_contact or False,
    )
    session.add(link)
    await session.flush()

    return {
        "link_id": link.id,
        "parent_id": parent_id,
        "student_user_id": body.student_user_id,
        "relationship": rel.value,
        "is_primary_contact": link.is_primary_contact,
    }


@router.delete(
    "/parents/{parent_id}/students/{student_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_student_from_parent(
    parent_id: int,
    student_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Remove the link between a student and a parent."""
    link = await session.scalar(
        select(StudentParentLink).where(
            and_(
                StudentParentLink.parent_profile_id == parent_id,
                StudentParentLink.student_user_id == student_id,
            )
        )
    )
    if not link:
        raise HTTPException(status_code=404, detail="Student-parent link not found.")
    await session.delete(link)


# ===========================================================================
# 2. STUDENT MANAGEMENT (admin only)
# ===========================================================================

class CreateStudentRequest(BaseModel):
    discord_user_id: int
    full_name: str
    email: Optional[str] = None
    role: Optional[str] = "student"
    # Optional profile fields
    preferred_name: Optional[str] = None
    school: Optional[str] = None
    year_level: Optional[str] = None
    notes: Optional[str] = None


class UpdateStudentRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    # Profile fields
    preferred_name: Optional[str] = None
    school: Optional[str] = None
    year_level: Optional[str] = None
    notes: Optional[str] = None


class EnrollStudentRequest(BaseModel):
    class_group_id: int
    enrollment_status: Optional[str] = "active"
    role: Optional[str] = "student"


@router.post("/students", status_code=status.HTTP_201_CREATED)
async def create_student(
    body: CreateStudentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a student user record (and optional profile)."""
    existing = await session.scalar(
        select(User).where(User.discord_user_id == body.discord_user_id)
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A user with Discord ID {body.discord_user_id} already exists.",
        )

    try:
        role = UserRole(body.role or "student")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: '{body.role}'.")

    user = User(
        discord_user_id=body.discord_user_id,
        full_name=body.full_name,
        email=body.email,
        role=role,
    )
    session.add(user)
    await session.flush()  # populate user.id

    profile = None
    if any(
        v is not None
        for v in [body.preferred_name, body.school, body.year_level, body.notes]
    ):
        profile = StudentProfile(
            user_id=user.id,
            preferred_name=body.preferred_name,
            school=body.school,
            year_level=body.year_level,
            notes=body.notes,
        )
        session.add(profile)
        await session.flush()

    return {
        "id": user.id,
        "discord_user_id": user.discord_user_id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.value,
        "active": user.active,
        "created_at": user.created_at.isoformat(),
        "profile": {
            "preferred_name": profile.preferred_name if profile else None,
            "school": profile.school if profile else None,
            "year_level": profile.year_level if profile else None,
        },
    }


@router.get("/students")
async def list_students_admin(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    role: Optional[str] = Query(None, description="Filter by role (student, tutor, admin)"),
    active: Optional[bool] = Query(None),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List users (filterable by role), paginated."""
    q = select(User).options(selectinload(User.student_profile))
    if role:
        try:
            q = q.where(User.role == UserRole(role))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid role filter: '{role}'.")
    else:
        # Default to students only when no role filter given
        q = q.where(User.role == UserRole.student)
    if active is not None:
        q = q.where(User.active.is_(active))
    q = q.order_by(User.full_name)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    users = result.scalars().all()

    items = [
        {
            "id": u.id,
            "discord_user_id": u.discord_user_id,
            "full_name": u.full_name,
            "email": u.email,
            "role": u.role.value,
            "active": u.active,
            "created_at": u.created_at.isoformat(),
            "preferred_name": u.student_profile.preferred_name if u.student_profile else None,
            "school": u.student_profile.school if u.student_profile else None,
            "year_level": u.student_profile.year_level if u.student_profile else None,
        }
        for u in users
    ]
    return {"total": total, "items": items}


@router.get("/students/{student_id}")
async def get_student(
    student_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Get a student with profile, class memberships, and payments."""
    result = await session.execute(
        select(User)
        .options(
            selectinload(User.student_profile),
            selectinload(User.class_memberships).selectinload(ClassGroupMember.class_group),
            selectinload(User.payments),
            selectinload(User.parent_links).selectinload(StudentParentLink.parent),
        )
        .where(User.id == student_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Student not found.")

    memberships = [
        {
            "id": m.id,
            "class_group_id": m.class_group_id,
            "class_group_name": m.class_group.name if m.class_group else None,
            "role": m.role.value,
            "enrollment_status": m.enrollment_status.value,
            "active": m.active,
        }
        for m in user.class_memberships
    ]

    payments = [
        {
            "id": p.id,
            "term": p.term,
            "amount_due": float(p.amount_due),
            "amount_paid": float(p.amount_paid),
            "status": p.status.value,
            "due_date": p.due_date.isoformat() if p.due_date else None,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        }
        for p in user.payments
    ]

    parents = [
        {
            "link_id": lnk.id,
            "parent_id": lnk.parent_profile_id,
            "parent_name": lnk.parent.full_name if lnk.parent else None,
            "relationship": lnk.relationship_type.value if lnk.relationship_type else None,
            "is_primary_contact": lnk.is_primary_contact,
        }
        for lnk in user.parent_links
    ]

    prof = user.student_profile
    return {
        "id": user.id,
        "discord_user_id": user.discord_user_id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.value,
        "active": user.active,
        "created_at": user.created_at.isoformat(),
        "profile": {
            "preferred_name": prof.preferred_name if prof else None,
            "school": prof.school if prof else None,
            "year_level": prof.year_level if prof else None,
            "notes": prof.notes if prof else None,
        },
        "class_memberships": memberships,
        "payments": payments,
        "parents": parents,
    }


@router.patch("/students/{student_id}")
async def update_student(
    student_id: int,
    body: UpdateStudentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update user and/or student profile fields."""
    result = await session.execute(
        select(User)
        .options(selectinload(User.student_profile))
        .where(User.id == student_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Student not found.")

    if body.full_name is not None:
        user.full_name = body.full_name
    if body.email is not None:
        user.email = body.email
    if body.role is not None:
        try:
            user.role = UserRole(body.role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid role: '{body.role}'.")
    if body.active is not None:
        user.active = body.active

    # Profile fields — create profile if it doesn't exist
    profile_fields = {
        "preferred_name": body.preferred_name,
        "school": body.school,
        "year_level": body.year_level,
        "notes": body.notes,
    }
    if any(v is not None for v in profile_fields.values()):
        if user.student_profile is None:
            prof = StudentProfile(user_id=user.id)
            session.add(prof)
            await session.flush()
            # Re-fetch to attach
            user.student_profile = prof
        else:
            prof = user.student_profile

        if body.preferred_name is not None:
            prof.preferred_name = body.preferred_name
        if body.school is not None:
            prof.school = body.school
        if body.year_level is not None:
            prof.year_level = body.year_level
        if body.notes is not None:
            prof.notes = body.notes
        prof.updated_at = datetime.now(utc)

    await session.flush()
    return {"id": user.id, "full_name": user.full_name, "updated": True}


@router.post("/students/{student_id}/enroll", status_code=status.HTTP_201_CREATED)
async def enroll_student_in_class(
    student_id: int,
    body: EnrollStudentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Enroll a student (or tutor) in a class group."""
    user = await session.get(User, student_id)
    if not user:
        raise HTTPException(status_code=404, detail="Student not found.")

    class_group = await session.get(ClassGroup, body.class_group_id)
    if not class_group:
        raise HTTPException(status_code=404, detail="Class group not found.")

    # Check for existing active membership
    existing = await session.scalar(
        select(ClassGroupMember).where(
            and_(
                ClassGroupMember.user_id == student_id,
                ClassGroupMember.class_group_id == body.class_group_id,
                ClassGroupMember.active.is_(True),
            )
        )
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Student is already actively enrolled in this class.",
        )

    try:
        member_role = MemberRole(body.role or "student")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid member role: '{body.role}'.")

    try:
        enroll_status = EnrollmentStatus(body.enrollment_status or "active")
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid enrollment status: '{body.enrollment_status}'."
        )

    member = ClassGroupMember(
        class_group_id=body.class_group_id,
        user_id=student_id,
        role=member_role,
        enrollment_status=enroll_status,
        active=True,
    )
    session.add(member)
    await session.flush()

    return {
        "id": member.id,
        "class_group_id": body.class_group_id,
        "class_group_name": class_group.name,
        "user_id": student_id,
        "role": member_role.value,
        "enrollment_status": enroll_status.value,
    }


@router.delete(
    "/students/{student_id}/classes/{class_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def withdraw_student_from_class(
    student_id: int,
    class_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Withdraw a student from a class (soft-delete: set active=False, status=withdrawn)."""
    member = await session.scalar(
        select(ClassGroupMember).where(
            and_(
                ClassGroupMember.user_id == student_id,
                ClassGroupMember.class_group_id == class_id,
                ClassGroupMember.active.is_(True),
            )
        )
    )
    if not member:
        raise HTTPException(
            status_code=404,
            detail="Active class membership not found for this student.",
        )
    member.active = False
    member.enrollment_status = EnrollmentStatus.withdrawn


# ===========================================================================
# 3. ATTENDANCE CORRECTION (admin + tutor)
# ===========================================================================

class UpdateAttendanceRequest(BaseModel):
    tutor_marked_status: Optional[str] = None
    discord_verification_status: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/attendance/{attendance_id}")
async def update_attendance(
    attendance_id: int,
    body: UpdateAttendanceRequest,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update tutor-marked status, discord verification status, or notes on an attendance record."""
    record = await session.get(AttendanceRecord, attendance_id)
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found.")

    if body.tutor_marked_status is not None:
        try:
            record.tutor_marked_status = AttendanceStatus(body.tutor_marked_status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid attendance status: '{body.tutor_marked_status}'.",
            )
        record.tutor_marked_at = datetime.now(utc)
        record.marked_by = MarkedBy.tutor if current.is_tutor else MarkedBy.admin

    if body.discord_verification_status is not None:
        try:
            record.discord_verification_status = DiscordVerificationStatus(
                body.discord_verification_status
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid discord verification status: '{body.discord_verification_status}'.",
            )

    if body.notes is not None:
        record.notes = body.notes

    record.updated_at = datetime.now(utc)
    await session.flush()

    return {
        "id": record.id,
        "lesson_id": record.lesson_id,
        "user_id": record.user_id,
        "status": record.status.value,
        "tutor_marked_status": record.tutor_marked_status.value if record.tutor_marked_status else None,
        "discord_verification_status": record.discord_verification_status.value,
        "notes": record.notes,
        "updated_at": record.updated_at.isoformat(),
    }


# ===========================================================================
# 4. PAYMENT MANAGEMENT (admin only)
# ===========================================================================

class CreateStudentPaymentRequest(BaseModel):
    student_user_id: int
    parent_profile_id: Optional[int] = None
    term: str
    amount_due: float
    amount_paid: Optional[float] = 0.0
    due_date: Optional[datetime] = None
    status: Optional[str] = "pending"
    payment_method: Optional[str] = None
    notes: Optional[str] = None


class UpdateStudentPaymentRequest(BaseModel):
    status: Optional[str] = None
    amount_paid: Optional[float] = None
    payment_method: Optional[str] = None
    notes: Optional[str] = None
    paid_at: Optional[datetime] = None
    due_date: Optional[datetime] = None


class CreateTutorPaymentRequest(BaseModel):
    tutor_user_id: int
    pay_period_start: datetime
    pay_period_end: datetime
    notes: Optional[str] = None


class UpdateTutorPaymentRequest(BaseModel):
    status: Optional[str] = None
    amount_paid: Optional[float] = None
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None


class AddTutorPaymentItemRequest(BaseModel):
    lesson_id: int
    hours: float
    rate: float


@router.post("/payments", status_code=status.HTTP_201_CREATED)
async def create_student_payment(
    body: CreateStudentPaymentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a student payment record."""
    student = await session.get(User, body.student_user_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")

    if body.parent_profile_id:
        parent = await session.get(ParentProfile, body.parent_profile_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent profile not found.")

    try:
        pay_status = PaymentStatus(body.status or "pending")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid payment status: '{body.status}'.")

    payment = StudentPayment(
        student_user_id=body.student_user_id,
        parent_profile_id=body.parent_profile_id,
        term=body.term,
        amount_due=body.amount_due,
        amount_paid=body.amount_paid or 0.0,
        due_date=body.due_date,
        status=pay_status,
        notes=body.notes,
    )

    if body.payment_method:
        from bot.models.payment import PaymentMethod
        try:
            payment.payment_method = PaymentMethod(body.payment_method)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid payment method: '{body.payment_method}'."
            )

    session.add(payment)
    await session.flush()

    # Audit log entry for creation
    if current.user_id:
        log_entry = StudentPaymentLog(
            payment_id=payment.id,
            action="created",
            new_status=pay_status.value,
            performed_by=current.user_id,
        )
        session.add(log_entry)

    return {
        "id": payment.id,
        "student_user_id": payment.student_user_id,
        "term": payment.term,
        "amount_due": float(payment.amount_due),
        "amount_paid": float(payment.amount_paid),
        "status": payment.status.value,
        "due_date": payment.due_date.isoformat() if payment.due_date else None,
        "created_at": payment.created_at.isoformat(),
    }


@router.get("/payments")
async def list_student_payments(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    student_id: Optional[int] = Query(None),
    payment_status: Optional[str] = Query(None, alias="status"),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List student payments (filterable by status and student)."""
    q = select(StudentPayment).options(selectinload(StudentPayment.student))
    if student_id:
        q = q.where(StudentPayment.student_user_id == student_id)
    if payment_status:
        try:
            q = q.where(StudentPayment.status == PaymentStatus(payment_status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status filter: '{payment_status}'.")
    q = q.order_by(StudentPayment.created_at.desc())

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    payments = result.scalars().all()

    items = [
        {
            "id": p.id,
            "student_user_id": p.student_user_id,
            "student_name": p.student.full_name if p.student else None,
            "term": p.term,
            "amount_due": float(p.amount_due),
            "amount_paid": float(p.amount_paid),
            "amount_remaining": p.amount_remaining,
            "status": p.status.value,
            "due_date": p.due_date.isoformat() if p.due_date else None,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "created_at": p.created_at.isoformat(),
        }
        for p in payments
    ]
    return {"total": total, "items": items}


@router.patch("/payments/{payment_id}")
async def update_student_payment(
    payment_id: int,
    body: UpdateStudentPaymentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update a student payment record."""
    payment = await session.get(StudentPayment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found.")

    old_status = payment.status.value

    if body.status is not None:
        try:
            payment.status = PaymentStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: '{body.status}'.")
    if body.amount_paid is not None:
        payment.amount_paid = body.amount_paid
    if body.payment_method is not None:
        from bot.models.payment import PaymentMethod
        try:
            payment.payment_method = PaymentMethod(body.payment_method)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid payment method: '{body.payment_method}'."
            )
    if body.notes is not None:
        payment.notes = body.notes
    if body.paid_at is not None:
        payment.paid_at = body.paid_at
    if body.due_date is not None:
        payment.due_date = body.due_date

    payment.updated_at = datetime.now(utc)

    # Audit log if status changed
    if body.status is not None and current.user_id:
        log_entry = StudentPaymentLog(
            payment_id=payment.id,
            action="status_changed",
            old_status=old_status,
            new_status=payment.status.value,
            performed_by=current.user_id,
            notes=body.notes,
        )
        session.add(log_entry)

    await session.flush()
    return {
        "id": payment.id,
        "status": payment.status.value,
        "amount_paid": float(payment.amount_paid),
        "updated": True,
    }


@router.post("/tutor-payments", status_code=status.HTTP_201_CREATED)
async def create_tutor_payment(
    body: CreateTutorPaymentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a tutor pay period."""
    tutor = await session.get(User, body.tutor_user_id)
    if not tutor:
        raise HTTPException(status_code=404, detail="Tutor user not found.")

    payment = TutorPayment(
        tutor_user_id=body.tutor_user_id,
        pay_period_start=body.pay_period_start,
        pay_period_end=body.pay_period_end,
        amount_due=0,
        amount_paid=0,
        status=TutorPaymentStatus.unpaid,
        notes=body.notes,
    )
    session.add(payment)
    await session.flush()

    return {
        "id": payment.id,
        "tutor_user_id": payment.tutor_user_id,
        "tutor_name": tutor.full_name,
        "pay_period_start": payment.pay_period_start.isoformat(),
        "pay_period_end": payment.pay_period_end.isoformat(),
        "amount_due": float(payment.amount_due),
        "status": payment.status.value,
        "created_at": payment.created_at.isoformat(),
    }


@router.get("/tutor-payments")
async def list_tutor_payments(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    tutor_id: Optional[int] = Query(None),
    payment_status: Optional[str] = Query(None, alias="status"),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List tutor pay periods."""
    q = select(TutorPayment).options(selectinload(TutorPayment.tutor))
    if tutor_id:
        q = q.where(TutorPayment.tutor_user_id == tutor_id)
    if payment_status:
        try:
            q = q.where(TutorPayment.status == TutorPaymentStatus(payment_status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status filter: '{payment_status}'.")
    q = q.order_by(TutorPayment.pay_period_start.desc())

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    payments = result.scalars().all()

    items = [
        {
            "id": p.id,
            "tutor_user_id": p.tutor_user_id,
            "tutor_name": p.tutor.full_name if p.tutor else None,
            "pay_period_start": p.pay_period_start.isoformat(),
            "pay_period_end": p.pay_period_end.isoformat(),
            "amount_due": float(p.amount_due),
            "amount_paid": float(p.amount_paid),
            "status": p.status.value,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        }
        for p in payments
    ]
    return {"total": total, "items": items}


@router.patch("/tutor-payments/{payment_id}")
async def update_tutor_payment(
    payment_id: int,
    body: UpdateTutorPaymentRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update tutor payment status."""
    payment = await session.get(TutorPayment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Tutor payment not found.")

    if body.status is not None:
        try:
            payment.status = TutorPaymentStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: '{body.status}'.")
    if body.amount_paid is not None:
        payment.amount_paid = body.amount_paid
    if body.paid_at is not None:
        payment.paid_at = body.paid_at
    if body.notes is not None:
        payment.notes = body.notes

    payment.updated_at = datetime.now(utc)
    await session.flush()

    return {"id": payment.id, "status": payment.status.value, "updated": True}


@router.post("/tutor-payments/{payment_id}/items", status_code=status.HTTP_201_CREATED)
async def add_tutor_payment_item(
    payment_id: int,
    body: AddTutorPaymentItemRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Add a lesson item to a tutor pay period and recalculate amount_due."""
    payment = await session.get(TutorPayment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Tutor payment not found.")

    lesson = await session.get(Lesson, body.lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found.")

    # Check for duplicate item
    existing_item = await session.scalar(
        select(TutorPaymentItem).where(
            and_(
                TutorPaymentItem.tutor_payment_id == payment_id,
                TutorPaymentItem.lesson_id == body.lesson_id,
            )
        )
    )
    if existing_item:
        raise HTTPException(
            status_code=409,
            detail="This lesson is already included in the pay period.",
        )

    amount = round(body.hours * body.rate, 2)
    item = TutorPaymentItem(
        tutor_payment_id=payment_id,
        lesson_id=body.lesson_id,
        hours=body.hours,
        rate=body.rate,
        amount=amount,
    )
    session.add(item)

    # Recalculate amount_due
    payment.amount_due = float(payment.amount_due) + amount
    payment.updated_at = datetime.now(utc)

    await session.flush()

    return {
        "id": item.id,
        "tutor_payment_id": payment_id,
        "lesson_id": body.lesson_id,
        "lesson_title": lesson.title,
        "hours": float(item.hours),
        "rate": float(item.rate),
        "amount": float(item.amount),
        "payment_amount_due": float(payment.amount_due),
    }


# ===========================================================================
# 5. PROGRESS REPORTS (admin + tutor)
# ===========================================================================

class CreateProgressReportRequest(BaseModel):
    student_user_id: int
    lesson_id: int
    tutor_user_id: int
    class_group_id: int
    summary: Optional[str] = None
    strengths: Optional[str] = None
    areas_for_improvement: Optional[str] = None
    next_steps: Optional[str] = None
    homework_set: Optional[str] = None
    report_url: Optional[str] = None
    status: Optional[str] = "draft"
    visible_to_parent: Optional[bool] = False
    visible_to_student: Optional[bool] = False


class UpdateProgressReportRequest(BaseModel):
    summary: Optional[str] = None
    strengths: Optional[str] = None
    areas_for_improvement: Optional[str] = None
    next_steps: Optional[str] = None
    homework_set: Optional[str] = None
    report_url: Optional[str] = None
    status: Optional[str] = None
    visible_to_parent: Optional[bool] = None
    visible_to_student: Optional[bool] = None


@router.post("/progress-reports", status_code=status.HTTP_201_CREATED)
async def create_progress_report(
    body: CreateProgressReportRequest,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a progress report for a student/lesson."""
    # Verify referenced entities exist
    student = await session.get(User, body.student_user_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    lesson = await session.get(Lesson, body.lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found.")

    try:
        report_status = ReportStatus(body.status or "draft")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid report status: '{body.status}'.")

    report = ProgressReport(
        student_user_id=body.student_user_id,
        lesson_id=body.lesson_id,
        tutor_user_id=body.tutor_user_id,
        class_group_id=body.class_group_id,
        summary=body.summary,
        strengths=body.strengths,
        areas_for_improvement=body.areas_for_improvement,
        next_steps=body.next_steps,
        homework_set=body.homework_set,
        report_url=body.report_url,
        status=report_status,
        visible_to_parent=body.visible_to_parent or False,
        visible_to_student=body.visible_to_student or False,
    )

    if report_status == ReportStatus.submitted:
        report.submitted_at = datetime.now(utc)

    session.add(report)
    await session.flush()

    return {
        "id": report.id,
        "student_user_id": report.student_user_id,
        "student_name": student.full_name,
        "lesson_id": report.lesson_id,
        "lesson_title": lesson.title,
        "status": report.status.value,
        "created_at": report.created_at.isoformat(),
    }


@router.get("/progress-reports")
async def list_progress_reports(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    lesson_id: Optional[int] = Query(None),
    student_id: Optional[int] = Query(None),
    report_status: Optional[str] = Query(None, alias="status"),
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List progress reports with optional filters."""
    q = (
        select(ProgressReport)
        .options(
            selectinload(ProgressReport.student),
            selectinload(ProgressReport.tutor),
            selectinload(ProgressReport.lesson),
        )
        .order_by(ProgressReport.created_at.desc())
    )
    if lesson_id:
        q = q.where(ProgressReport.lesson_id == lesson_id)
    if student_id:
        q = q.where(ProgressReport.student_user_id == student_id)
    if report_status:
        try:
            q = q.where(ProgressReport.status == ReportStatus(report_status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status filter: '{report_status}'.")

    # Tutors only see their own reports
    if current.is_tutor and current.user_id:
        q = q.where(ProgressReport.tutor_user_id == current.user_id)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    reports = result.scalars().all()

    items = [
        {
            "id": r.id,
            "student_user_id": r.student_user_id,
            "student_name": r.student.full_name if r.student else None,
            "lesson_id": r.lesson_id,
            "lesson_title": r.lesson.title if r.lesson else None,
            "tutor_user_id": r.tutor_user_id,
            "tutor_name": r.tutor.full_name if r.tutor else None,
            "status": r.status.value,
            "visible_to_parent": r.visible_to_parent,
            "visible_to_student": r.visible_to_student,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in reports
    ]
    return {"total": total, "items": items}


@router.patch("/progress-reports/{report_id}")
async def update_progress_report(
    report_id: int,
    body: UpdateProgressReportRequest,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update content, status, or visibility of a progress report."""
    report = await session.get(ProgressReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Progress report not found.")

    # Tutors can only update their own reports
    if current.is_tutor and current.user_id and report.tutor_user_id != current.user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only update your own progress reports.",
        )

    if body.summary is not None:
        report.summary = body.summary
    if body.strengths is not None:
        report.strengths = body.strengths
    if body.areas_for_improvement is not None:
        report.areas_for_improvement = body.areas_for_improvement
    if body.next_steps is not None:
        report.next_steps = body.next_steps
    if body.homework_set is not None:
        report.homework_set = body.homework_set
    if body.report_url is not None:
        report.report_url = body.report_url
    if body.visible_to_parent is not None:
        report.visible_to_parent = body.visible_to_parent
    if body.visible_to_student is not None:
        report.visible_to_student = body.visible_to_student

    if body.status is not None:
        try:
            new_status = ReportStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: '{body.status}'.")
        if new_status == ReportStatus.submitted and report.status == ReportStatus.draft:
            report.submitted_at = datetime.now(utc)
        report.status = new_status

    report.updated_at = datetime.now(utc)
    await session.flush()

    return {"id": report.id, "status": report.status.value, "updated": True}


# ===========================================================================
# 6. SYSTEM ALERTS (admin only)
# ===========================================================================

class UpdateAlertRequest(BaseModel):
    status: Optional[str] = None
    assigned_to: Optional[int] = None


@router.get("/alerts")
async def list_alerts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    severity: Optional[str] = Query(None, description="low | medium | high | critical"),
    alert_type: Optional[str] = Query(None),
    alert_status: Optional[str] = Query(None, alias="status", description="open | resolved | dismissed"),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List system alerts with optional filters."""
    q = select(SystemAlert).order_by(SystemAlert.created_at.desc())

    # Default to open alerts when no status filter is given
    if alert_status:
        try:
            q = q.where(SystemAlert.status == AlertStatus(alert_status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid alert status: '{alert_status}'.")
    else:
        q = q.where(SystemAlert.status == AlertStatus.open)

    if severity:
        try:
            q = q.where(SystemAlert.severity == AlertSeverity(severity))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid severity: '{severity}'.")
    if alert_type:
        q = q.where(SystemAlert.alert_type == alert_type)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    alerts = result.scalars().all()

    items = [
        {
            "id": a.id,
            "alert_type": a.alert_type,
            "severity": a.severity.value,
            "title": a.title,
            "message": a.message,
            "related_entity_type": a.related_entity_type,
            "related_entity_id": a.related_entity_id,
            "status": a.status.value,
            "assigned_to": a.assigned_to,
            "created_at": a.created_at.isoformat(),
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        }
        for a in alerts
    ]
    return {"total": total, "items": items}


@router.patch("/alerts/{alert_id}")
async def update_alert(
    alert_id: int,
    body: UpdateAlertRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Resolve or dismiss a system alert."""
    alert = await session.get(SystemAlert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")

    if body.status is not None:
        try:
            new_status = AlertStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid alert status: '{body.status}'.")
        alert.status = new_status
        if new_status in (AlertStatus.resolved, AlertStatus.dismissed):
            alert.resolved_at = datetime.now(utc)

    if body.assigned_to is not None:
        alert.assigned_to = body.assigned_to

    await session.flush()
    return {"id": alert.id, "status": alert.status.value, "updated": True}


@router.post("/alerts/generate", status_code=status.HTTP_200_OK)
async def generate_alerts(
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Run alert generation logic.

    Checks for:
    - Completed lessons with no progress report (medium severity)
    - Overdue student payments (high severity)
    - Attendance records with discord_verification_status=mismatch (low severity)

    Skips creating an alert if an open alert already exists for the same entity.
    """
    now = datetime.now(utc)
    created_count = 0

    # ── Helper: check if open alert already exists for entity ──────────── #
    async def alert_exists(entity_type: str, entity_id: int, a_type: str) -> bool:
        count = await session.scalar(
            select(func.count()).select_from(
                select(SystemAlert)
                .where(
                    and_(
                        SystemAlert.related_entity_type == entity_type,
                        SystemAlert.related_entity_id == entity_id,
                        SystemAlert.alert_type == a_type,
                        SystemAlert.status == AlertStatus.open,
                    )
                )
                .subquery()
            )
        )
        return (count or 0) > 0

    # ── 1. Completed lessons with no progress report ───────────────────── #
    completed_lessons_result = await session.execute(
        select(Lesson).where(Lesson.status == LessonStatus.completed)
    )
    completed_lessons = completed_lessons_result.scalars().all()

    for lesson in completed_lessons:
        report_count = await session.scalar(
            select(func.count()).select_from(
                select(ProgressReport)
                .where(ProgressReport.lesson_id == lesson.id)
                .subquery()
            )
        )
        if (report_count or 0) == 0:
            if not await alert_exists("lesson", lesson.id, "missing_progress_report"):
                alert = SystemAlert(
                    alert_type="missing_progress_report",
                    severity=AlertSeverity.medium,
                    title="Missing Progress Report",
                    message=(
                        f"Lesson '{lesson.title}' (ID: {lesson.id}) completed on "
                        f"{lesson.end_time.strftime('%Y-%m-%d')} has no progress report."
                    ),
                    related_entity_type="lesson",
                    related_entity_id=lesson.id,
                    status=AlertStatus.open,
                )
                session.add(alert)
                created_count += 1

    # ── 2. Overdue student payments ────────────────────────────────────── #
    overdue_payments_result = await session.execute(
        select(StudentPayment)
        .options(selectinload(StudentPayment.student))
        .where(StudentPayment.status == PaymentStatus.overdue)
    )
    overdue_payments = overdue_payments_result.scalars().all()

    for payment in overdue_payments:
        if not await alert_exists("student_payment", payment.id, "payment_overdue"):
            student_name = payment.student.full_name if payment.student else f"User #{payment.student_user_id}"
            alert = SystemAlert(
                alert_type="payment_overdue",
                severity=AlertSeverity.high,
                title="Payment Overdue",
                message=(
                    f"Student {student_name} has an overdue payment of "
                    f"${float(payment.amount_due):.2f} for term '{payment.term}'."
                ),
                related_entity_type="student_payment",
                related_entity_id=payment.id,
                status=AlertStatus.open,
            )
            session.add(alert)
            created_count += 1

    # ── 3. Attendance mismatch records ────────────────────────────────── #
    mismatch_result = await session.execute(
        select(AttendanceRecord).where(
            AttendanceRecord.discord_verification_status == DiscordVerificationStatus.mismatch
        )
    )
    mismatch_records = mismatch_result.scalars().all()

    for record in mismatch_records:
        if not await alert_exists("attendance_record", record.id, "attendance_mismatch"):
            alert = SystemAlert(
                alert_type="attendance_mismatch",
                severity=AlertSeverity.low,
                title="Attendance Mismatch",
                message=(
                    f"Attendance record #{record.id} (lesson {record.lesson_id}, "
                    f"user {record.user_id}) has a mismatch between tutor-marked "
                    f"status and Discord voice activity."
                ),
                related_entity_type="attendance_record",
                related_entity_id=record.id,
                status=AlertStatus.open,
            )
            session.add(alert)
            created_count += 1

    await session.flush()

    return {
        "alerts_created": created_count,
        "generated_at": now.isoformat(),
    }


# ===========================================================================
# 7. ANNOUNCEMENTS (admin only)
# ===========================================================================

class CreateAnnouncementRequest(BaseModel):
    title: str
    body: str
    target_role: Optional[str] = None
    target_class_group_id: Optional[int] = None
    target_program_id: Optional[int] = None
    status: Optional[str] = "draft"


class UpdateAnnouncementRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    target_role: Optional[str] = None
    target_class_group_id: Optional[int] = None
    target_program_id: Optional[int] = None
    status: Optional[str] = None


@router.post("/announcements", status_code=status.HTTP_201_CREATED)
async def create_announcement(
    body: CreateAnnouncementRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new announcement."""
    if current.user_id is None:
        raise HTTPException(
            status_code=400,
            detail="Announcements must be created by a Discord-authenticated admin user.",
        )

    try:
        ann_status = AnnouncementStatus(body.status or "draft")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: '{body.status}'.")

    announcement = Announcement(
        title=body.title,
        body=body.body,
        created_by=current.user_id,
        target_role=body.target_role,
        target_class_group_id=body.target_class_group_id,
        target_program_id=body.target_program_id,
        status=ann_status,
    )

    if ann_status == AnnouncementStatus.published:
        announcement.published_at = datetime.now(utc)

    session.add(announcement)
    await session.flush()

    return {
        "id": announcement.id,
        "title": announcement.title,
        "status": announcement.status.value,
        "created_at": announcement.created_at.isoformat(),
    }


@router.get("/announcements")
async def list_announcements(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    ann_status: Optional[str] = Query(None, alias="status"),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List announcements."""
    q = select(Announcement).options(selectinload(Announcement.author))
    if ann_status:
        try:
            q = q.where(Announcement.status == AnnouncementStatus(ann_status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status filter: '{ann_status}'.")
    q = q.order_by(Announcement.created_at.desc())

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    announcements = result.scalars().all()

    items = [
        {
            "id": a.id,
            "title": a.title,
            "body": a.body,
            "status": a.status.value,
            "target_role": a.target_role,
            "target_class_group_id": a.target_class_group_id,
            "author_name": a.author.full_name if a.author else None,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in announcements
    ]
    return {"total": total, "items": items}


@router.patch("/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: int,
    body: UpdateAnnouncementRequest,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update, publish, or archive an announcement."""
    announcement = await session.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found.")

    if body.title is not None:
        announcement.title = body.title
    if body.body is not None:
        announcement.body = body.body
    if body.target_role is not None:
        announcement.target_role = body.target_role
    if body.target_class_group_id is not None:
        announcement.target_class_group_id = body.target_class_group_id
    if body.target_program_id is not None:
        announcement.target_program_id = body.target_program_id

    if body.status is not None:
        try:
            new_status = AnnouncementStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: '{body.status}'.")
        if (
            new_status == AnnouncementStatus.published
            and announcement.status != AnnouncementStatus.published
        ):
            announcement.published_at = datetime.now(utc)
        announcement.status = new_status

    announcement.updated_at = datetime.now(utc)
    await session.flush()

    return {"id": announcement.id, "status": announcement.status.value, "updated": True}


# ===========================================================================
# 8. RESOURCES (admin + tutor)
# ===========================================================================

class CreateResourceRequest(BaseModel):
    title: str
    resource_url: str
    resource_type: Optional[str] = "link"
    description: Optional[str] = None
    class_group_id: Optional[int] = None
    program_id: Optional[int] = None
    student_user_id: Optional[int] = None


class UpdateResourceRequest(BaseModel):
    title: Optional[str] = None
    resource_url: Optional[str] = None
    resource_type: Optional[str] = None
    description: Optional[str] = None
    class_group_id: Optional[int] = None
    program_id: Optional[int] = None
    student_user_id: Optional[int] = None


@router.post("/resources", status_code=status.HTTP_201_CREATED)
async def create_resource(
    body: CreateResourceRequest,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a resource link."""
    if current.user_id is None:
        raise HTTPException(
            status_code=400,
            detail="Resources must be created by a Discord-authenticated user.",
        )

    try:
        res_type = ResourceType(body.resource_type or "link")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid resource type: '{body.resource_type}'.")

    resource = Resource(
        title=body.title,
        resource_url=body.resource_url,
        resource_type=res_type,
        description=body.description,
        class_group_id=body.class_group_id,
        program_id=body.program_id,
        student_user_id=body.student_user_id,
        created_by=current.user_id,
        active=True,
    )
    session.add(resource)
    await session.flush()

    return {
        "id": resource.id,
        "title": resource.title,
        "resource_url": resource.resource_url,
        "resource_type": resource.resource_type.value,
        "class_group_id": resource.class_group_id,
        "created_at": resource.created_at.isoformat(),
    }


@router.get("/resources")
async def list_resources(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    class_group_id: Optional[int] = Query(None),
    active: Optional[bool] = Query(None),
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List resources, optionally filtered by class group."""
    q = select(Resource).order_by(Resource.created_at.desc())
    if class_group_id is not None:
        q = q.where(Resource.class_group_id == class_group_id)
    if active is not None:
        q = q.where(Resource.active.is_(active))
    else:
        # Default to only active resources
        q = q.where(Resource.active.is_(True))

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    result = await session.execute(q.offset(skip).limit(limit))
    resources = result.scalars().all()

    items = [
        {
            "id": r.id,
            "title": r.title,
            "resource_url": r.resource_url,
            "resource_type": r.resource_type.value,
            "description": r.description,
            "class_group_id": r.class_group_id,
            "program_id": r.program_id,
            "student_user_id": r.student_user_id,
            "active": r.active,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat(),
        }
        for r in resources
    ]
    return {"total": total, "items": items}


@router.patch("/resources/{resource_id}")
async def update_resource(
    resource_id: int,
    body: UpdateResourceRequest,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update a resource."""
    resource = await session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found.")

    # Tutors can only update their own resources
    if current.is_tutor and current.user_id and resource.created_by != current.user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only update resources you created.",
        )

    if body.title is not None:
        resource.title = body.title
    if body.resource_url is not None:
        resource.resource_url = body.resource_url
    if body.resource_type is not None:
        try:
            resource.resource_type = ResourceType(body.resource_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid resource type: '{body.resource_type}'."
            )
    if body.description is not None:
        resource.description = body.description
    if body.class_group_id is not None:
        resource.class_group_id = body.class_group_id
    if body.program_id is not None:
        resource.program_id = body.program_id
    if body.student_user_id is not None:
        resource.student_user_id = body.student_user_id

    await session.flush()
    return {"id": resource.id, "title": resource.title, "updated": True}


@router.delete("/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resource(
    resource_id: int,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a resource by setting active=False."""
    resource = await session.get(Resource, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found.")

    # Tutors can only delete their own resources
    if current.is_tutor and current.user_id and resource.created_by != current.user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only delete resources you created.",
        )

    resource.active = False


# ===========================================================================
# 9. ADMIN OVERVIEW STATS
# ===========================================================================

@router.get("/overview-stats")
async def get_overview_stats(
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Return a single overview payload for the admin dashboard home page."""
    import datetime as _dt

    today = _dt.date.today()
    today_start = _dt.datetime.combine(today, _dt.time.min, tzinfo=utc)
    today_end   = _dt.datetime.combine(today, _dt.time.max, tzinfo=utc)

    # Active students
    total_students = (await session.scalar(
        select(func.count(User.id)).where(User.role == UserRole.student, User.active.is_(True))
    )) or 0

    # Active class groups
    active_classes = (await session.scalar(
        select(func.count(ClassGroup.id)).where(ClassGroup.active.is_(True))
    )) or 0

    # Lessons scheduled or active today
    today_lessons = (await session.scalar(
        select(func.count(Lesson.id)).where(
            Lesson.start_time >= today_start,
            Lesson.start_time <= today_end,
            Lesson.status.in_([LessonStatus.scheduled, LessonStatus.active]),
        )
    )) or 0

    # Open alerts
    open_alerts = (await session.scalar(
        select(func.count(SystemAlert.id)).where(SystemAlert.status == AlertStatus.open)
    )) or 0

    # Pending / overdue student payments
    pending_payments = (await session.scalar(
        select(func.count(StudentPayment.id)).where(
            StudentPayment.status.in_([PaymentStatus.pending, PaymentStatus.overdue])
        )
    )) or 0

    # Progress reports not yet submitted (draft with a lesson assigned)
    missing_reports = (await session.scalar(
        select(func.count(ProgressReport.id)).where(
            ProgressReport.status == ReportStatus.draft,
            ProgressReport.lesson_id.is_not(None),
        )
    )) or 0

    # Recent open alerts (up to 5) for the alert panel
    alert_rows = await session.execute(
        select(SystemAlert)
        .where(SystemAlert.status == AlertStatus.open)
        .order_by(SystemAlert.created_at.desc())
        .limit(5)
    )
    recent_alerts = [
        {
            "id": a.id,
            "alert_type": a.alert_type,
            "severity": a.severity.value,
            "title": a.title,
            "message": a.message,
            "created_at": a.created_at.isoformat(),
        }
        for a in alert_rows.scalars().all()
    ]

    # Today's lessons (up to 8)
    lesson_rows = await session.execute(
        select(Lesson)
        .options(selectinload(Lesson.class_group))
        .where(
            Lesson.start_time >= today_start,
            Lesson.start_time <= today_end,
        )
        .order_by(Lesson.start_time)
        .limit(8)
    )
    today_lesson_list = [
        {
            "id": ls.id,
            "title": ls.title,
            "class_name": ls.class_group.name if ls.class_group else None,
            "start_time": ls.start_time.isoformat(),
            "end_time": ls.end_time.isoformat() if ls.end_time else None,
            "status": ls.status.value,
        }
        for ls in lesson_rows.scalars().all()
    ]

    return {
        "total_students":   total_students,
        "active_classes":   active_classes,
        "today_lessons":    today_lessons,
        "open_alerts":      open_alerts,
        "pending_payments": pending_payments,
        "missing_reports":  missing_reports,
        "recent_alerts":    recent_alerts,
        "today_lesson_list": today_lesson_list,
    }


# ===========================================================================
# 10. ADMIN CLASS MANAGEMENT
# ===========================================================================

@router.get("/classes")
async def admin_list_classes(
    active: Optional[bool] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List all class groups with member counts and assigned tutor."""
    q = select(ClassGroup).options(selectinload(ClassGroup.tutor))
    if active is not None:
        q = q.where(ClassGroup.active.is_(active))
    q = q.order_by(ClassGroup.name)

    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    rows = (await session.execute(q.offset(skip).limit(limit))).scalars().all()

    group_ids = [g.id for g in rows]
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
    for g in rows:
        tutor_data = None
        if g.tutor:
            tutor_data = {
                "id": g.tutor.id,
                "full_name": g.tutor.full_name,
                "discord_user_id": g.tutor.discord_user_id,
            }
        items.append({
            "id": g.id,
            "name": g.name,
            "year_level": g.year_level,
            "subject": g.subject,
            "active": g.active,
            "created_at": g.created_at.isoformat(),
            "tutor": tutor_data,
            "member_count": member_counts.get(g.id, 0),
        })

    return {"total": total, "items": items}


@router.get("/classes/{class_id}")
async def admin_get_class(
    class_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Get a class group with its full student roster."""
    cg = await session.get(
        ClassGroup, class_id,
        options=[
            selectinload(ClassGroup.tutor),
            selectinload(ClassGroup.members).selectinload(ClassGroupMember.user),
        ]
    )
    if not cg:
        raise HTTPException(status_code=404, detail="Class not found.")

    tutor_data = None
    if cg.tutor:
        tutor_data = {
            "id": cg.tutor.id,
            "full_name": cg.tutor.full_name,
            "discord_user_id": cg.tutor.discord_user_id,
        }

    roster = []
    for m in (cg.members or []):
        roster.append({
            "membership_id": m.id,
            "user_id": m.user_id,
            "student_name": m.user.full_name if m.user else None,
            "enrollment_status": m.enrollment_status.value if hasattr(m.enrollment_status, 'value') else str(m.enrollment_status),
            "start_date": m.start_date.isoformat() if m.start_date else None,
            "end_date": m.end_date.isoformat() if m.end_date else None,
        })

    return {
        "id": cg.id,
        "name": cg.name,
        "year_level": cg.year_level,
        "subject": cg.subject,
        "active": cg.active,
        "created_at": cg.created_at.isoformat(),
        "tutor": tutor_data,
        "member_count": sum(1 for m in (cg.members or []) if m.active),
        "roster": roster,
    }


# ===========================================================================
# 11. ADMIN LESSON MANAGEMENT
# ===========================================================================

@router.get("/lessons/{lesson_id}")
async def admin_get_lesson(
    lesson_id: int,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Fetch a single lesson with its class, tutor, and attendance summary."""
    lesson = await session.get(
        Lesson,
        lesson_id,
        options=[
            selectinload(Lesson.class_group).selectinload(ClassGroup.tutor),
            selectinload(Lesson.attendance_records),
        ],
    )
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found.")

    # Build attendance summary using tutor_marked_status where available,
    # falling back to the system-auto status field.
    summary: dict[str, int] = {
        "present": 0, "late": 0, "left_early": 0, "absent": 0, "unknown": 0,
    }
    for ar in lesson.attendance_records:
        effective = ar.tutor_marked_status or ar.status
        key = effective.value if hasattr(effective, "value") else str(effective)
        summary[key] = summary.get(key, 0) + 1

    cg = lesson.class_group
    tutor = cg.tutor if cg else None

    return {
        "id": lesson.id,
        "class_group_id": lesson.class_group_id,
        "class_group_name": cg.name if cg else None,
        "tutor_name": tutor.full_name if tutor else None,
        "tutor_user_id": tutor.id if tutor else None,
        "title": lesson.title,
        "description": lesson.description,
        "start_time": lesson.start_time.isoformat(),
        "end_time": lesson.end_time.isoformat() if lesson.end_time else None,
        "location": lesson.location,
        "meet_link": lesson.meet_link,
        "status": lesson.status.value,
        "created_at": lesson.created_at.isoformat(),
        "updated_at": lesson.updated_at.isoformat(),
        "attendance_summary": summary,
        "attendance_total": sum(summary.values()),
    }


@router.get("/lessons")
async def admin_list_lessons(
    class_group_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    upcoming_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List lessons with optional filters."""
    import datetime as _dt
    q = select(Lesson).options(selectinload(Lesson.class_group))

    if class_group_id is not None:
        q = q.where(Lesson.class_group_id == class_group_id)
    if status_filter:
        try:
            q = q.where(Lesson.status == LessonStatus(status_filter))
        except ValueError:
            pass
    if upcoming_only:
        now = _dt.datetime.now(_dt.timezone.utc)
        q = q.where(Lesson.start_time >= now)

    q = q.order_by(Lesson.start_time.desc())
    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    rows = (await session.execute(q.offset(skip).limit(limit))).scalars().all()

    items = [
        {
            "id": ls.id,
            "class_group_id": ls.class_group_id,
            "class_group_name": ls.class_group.name if ls.class_group else None,
            "title": ls.title,
            "description": ls.description,
            "start_time": ls.start_time.isoformat(),
            "end_time": ls.end_time.isoformat() if ls.end_time else None,
            "location": ls.location,
            "meet_link": ls.meet_link,
            "status": ls.status.value,
        }
        for ls in rows
    ]

    return {"total": total, "items": items}


# ===========================================================================
# 12. ADMIN HOMEWORK MANAGEMENT
# ===========================================================================

class HomeworkTaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    class_group_id: int
    lesson_id: Optional[int] = None
    due_at: str  # ISO 8601


class HomeworkTaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    due_at: Optional[str] = None
    lesson_id: Optional[int] = None


class SubmissionUpdate(BaseModel):
    status: Optional[str] = None
    tutor_feedback: Optional[str] = None


@router.get("/homework/{task_id}")
async def admin_get_homework_task(
    task_id: int,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Fetch a single homework task with all student submissions."""
    task = await session.get(
        HomeworkTask,
        task_id,
        options=[
            selectinload(HomeworkTask.class_group),
            selectinload(HomeworkTask.creator),
            selectinload(HomeworkTask.submissions).selectinload(HomeworkSubmission.student),
        ],
    )
    if not task:
        raise HTTPException(status_code=404, detail="Homework task not found.")

    submissions = [
        {
            "id": s.id,
            "student_user_id": s.student_user_id,
            "student_name": s.student.full_name if s.student else None,
            "status": s.status.value if hasattr(s.status, "value") else str(s.status),
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            "attachment_url": s.attachment_url,
            "tutor_feedback": s.tutor_feedback,
            "created_at": s.created_at.isoformat(),
        }
        for s in (task.submissions or [])
    ]

    summary: dict[str, int] = {"submitted": 0, "late": 0, "missing": 0, "reviewed": 0}
    for s in task.submissions or []:
        key = s.status.value if hasattr(s.status, "value") else str(s.status)
        summary[key] = summary.get(key, 0) + 1

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "class_group_id": task.class_group_id,
        "class_group_name": task.class_group.name if task.class_group else None,
        "lesson_id": task.lesson_id,
        "due_at": task.due_at.isoformat(),
        "created_by": task.created_by,
        "creator_name": task.creator.full_name if task.creator else None,
        "created_at": task.created_at.isoformat(),
        "submissions": submissions,
        "submission_summary": summary,
        "submission_total": len(task.submissions or []),
    }


@router.get("/homework")
async def admin_list_homework(
    class_group_id: Optional[int] = Query(None),
    overdue_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """List homework tasks with optional filters and per-task submission summary."""
    import datetime as _dt

    q = select(HomeworkTask).options(
        selectinload(HomeworkTask.class_group),
        selectinload(HomeworkTask.submissions),
    )

    if class_group_id is not None:
        q = q.where(HomeworkTask.class_group_id == class_group_id)
    if overdue_only:
        now = _dt.datetime.now(_dt.timezone.utc)
        q = q.where(HomeworkTask.due_at < now)

    q = q.order_by(HomeworkTask.due_at.desc())
    total = (await session.scalar(select(func.count()).select_from(q.subquery()))) or 0
    rows = (await session.execute(q.offset(skip).limit(limit))).scalars().all()

    items = []
    for task in rows:
        summary: dict[str, int] = {"submitted": 0, "late": 0, "missing": 0, "reviewed": 0}
        for s in task.submissions or []:
            key = s.status.value if hasattr(s.status, "value") else str(s.status)
            summary[key] = summary.get(key, 0) + 1

        items.append({
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "class_group_id": task.class_group_id,
            "class_group_name": task.class_group.name if task.class_group else None,
            "lesson_id": task.lesson_id,
            "due_at": task.due_at.isoformat(),
            "created_by": task.created_by,
            "created_at": task.created_at.isoformat(),
            "submission_summary": summary,
            "submission_total": len(task.submissions or []),
        })

    return {"total": total, "items": items}


@router.post("/homework")
async def admin_create_homework(
    body: HomeworkTaskCreate,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new homework task for a class."""
    import datetime as _dt

    cg_result = await session.execute(
        select(ClassGroup).where(ClassGroup.id == body.class_group_id)
    )
    cg = cg_result.scalar_one_or_none()
    if not cg:
        raise HTTPException(status_code=404, detail="Class not found.")

    try:
        due = _dt.datetime.fromisoformat(body.due_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid due_at format. Use ISO 8601.")

    task = HomeworkTask(
        title=body.title,
        description=body.description,
        class_group_id=body.class_group_id,
        lesson_id=body.lesson_id,
        due_at=due,
        created_by=current.user_id,
    )
    session.add(task)
    await session.flush()
    await session.refresh(task)

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "class_group_id": task.class_group_id,
        "class_group_name": cg.name,
        "lesson_id": task.lesson_id,
        "due_at": task.due_at.isoformat(),
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat(),
        "submission_summary": {"submitted": 0, "late": 0, "missing": 0, "reviewed": 0},
        "submission_total": 0,
    }


@router.patch("/homework/{task_id}")
async def admin_update_homework(
    task_id: int,
    body: HomeworkTaskUpdate,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update homework task title, description, due date, or linked lesson."""
    import datetime as _dt

    task = await session.get(HomeworkTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Homework task not found.")

    if body.title is not None:
        task.title = body.title
    if body.description is not None:
        task.description = body.description
    if body.due_at is not None:
        try:
            task.due_at = _dt.datetime.fromisoformat(body.due_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid due_at format.")
    if body.lesson_id is not None:
        task.lesson_id = body.lesson_id

    await session.flush()
    return {"id": task.id, "title": task.title, "due_at": task.due_at.isoformat()}


@router.delete("/homework/{task_id}", status_code=204)
async def admin_delete_homework(
    task_id: int,
    current: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """Delete a homework task and all its submissions (cascade)."""
    task = await session.get(HomeworkTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Homework task not found.")
    await session.delete(task)
    await session.flush()


@router.patch("/homework/{task_id}/submissions/{submission_id}")
async def admin_update_submission(
    task_id: int,
    submission_id: int,
    body: SubmissionUpdate,
    current: AuthUser = Depends(require_admin_or_tutor),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Update a student submission — set status or add tutor feedback."""
    sub = await session.get(HomeworkSubmission, submission_id)
    if not sub or sub.homework_task_id != task_id:
        raise HTTPException(status_code=404, detail="Submission not found.")

    if body.status is not None:
        try:
            sub.status = SubmissionStatus(body.status)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status. Choose from: {[s.value for s in SubmissionStatus]}",
            )
    if body.tutor_feedback is not None:
        sub.tutor_feedback = body.tutor_feedback

    await session.flush()
    return {
        "id": sub.id,
        "homework_task_id": sub.homework_task_id,
        "student_user_id": sub.student_user_id,
        "status": sub.status.value,
        "tutor_feedback": sub.tutor_feedback,
    }
