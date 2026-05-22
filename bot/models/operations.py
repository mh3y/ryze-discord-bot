"""Operational models: progress reports, announcements, resources, alerts, availability."""
import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.user import User
    from bot.models.lesson import Lesson
    from bot.models.class_group import ClassGroup
    from bot.models.academic import Program


# ---------------------------------------------------------------------------
# Progress Reports
# ---------------------------------------------------------------------------

class ReportStatus(str, enum.Enum):
    draft = "draft"
    submitted = "submitted"
    approved = "approved"
    sent_to_parent = "sent_to_parent"


class ProgressReport(Base):
    """Tutor's written feedback on a student after a lesson.

    Tutor submits via Tally form (report_url) or natively.
    Admin reviews and can mark as sent to parent.
    Visible flags control what students/parents can see.
    """
    __tablename__ = "progress_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    lesson_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=False, index=True
    )
    tutor_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    class_group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("class_groups.id"), nullable=False, index=True
    )
    # Link to Tally submission (future webhook integration)
    tally_submission_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    report_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    # Structured fields (used for native form or auto-populated from Tally webhook)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strengths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    areas_for_improvement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_steps: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    homework_set: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Visibility controls
    visible_to_parent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visible_to_student: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus), nullable=False, default=ReportStatus.draft
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    student: Mapped["User"] = relationship("User", foreign_keys=[student_user_id])
    tutor: Mapped["User"] = relationship("User", foreign_keys=[tutor_user_id])
    lesson: Mapped["Lesson"] = relationship("Lesson")
    class_group: Mapped["ClassGroup"] = relationship("ClassGroup")

    def __repr__(self) -> str:
        return (
            f"<ProgressReport id={self.id} student={self.student_user_id} "
            f"lesson={self.lesson_id} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Tutor Availability / Leave Requests
# ---------------------------------------------------------------------------

class AvailabilityStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class TutorAvailability(Base):
    """Tutor leave/unavailability request.

    Tutor submits via portal.  Admin approves or rejects.
    Used to flag scheduling conflicts in admin alerts.
    """
    __tablename__ = "tutor_availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tutor_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    unavailable_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unavailable_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[AvailabilityStatus] = mapped_column(
        Enum(AvailabilityStatus), nullable=False, default=AvailabilityStatus.pending
    )
    reviewed_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    tutor: Mapped["User"] = relationship("User", foreign_keys=[tutor_user_id])
    reviewer: Mapped[Optional["User"]] = relationship("User", foreign_keys=[reviewed_by])

    def __repr__(self) -> str:
        return (
            f"<TutorAvailability tutor={self.tutor_user_id} "
            f"{self.unavailable_start.date()}..{self.unavailable_end.date()} "
            f"status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Make-up Lesson Requests
# ---------------------------------------------------------------------------

class MakeupStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    completed = "completed"


class MakeupLessonRequest(Base):
    """Student requests a make-up for a missed lesson."""
    __tablename__ = "makeup_lesson_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    original_lesson_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=False, index=True
    )
    student_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rescheduled_lesson_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=True
    )
    status: Mapped[MakeupStatus] = mapped_column(
        Enum(MakeupStatus), nullable=False, default=MakeupStatus.pending
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    original_lesson: Mapped["Lesson"] = relationship("Lesson", foreign_keys=[original_lesson_id])
    rescheduled_lesson: Mapped[Optional["Lesson"]] = relationship(
        "Lesson", foreign_keys=[rescheduled_lesson_id]
    )
    student: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<MakeupLessonRequest student={self.student_user_id} "
            f"lesson={self.original_lesson_id} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------

class AnnouncementStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class Announcement(Base):
    """Targeted announcements from admin/tutors.

    Can be scoped to a role, class, or program.
    Shown in the relevant dashboard views.
    """
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    # Targeting — can combine (e.g. role=student + class_group_id=5)
    target_role: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_class_group_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("class_groups.id"), nullable=True
    )
    target_program_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("programs.id"), nullable=True
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[AnnouncementStatus] = mapped_column(
        Enum(AnnouncementStatus), nullable=False, default=AnnouncementStatus.draft
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    author: Mapped["User"] = relationship("User", foreign_keys=[created_by])

    def __repr__(self) -> str:
        return f"<Announcement id={self.id} title={self.title!r} status={self.status}>"


# ---------------------------------------------------------------------------
# Resources (Google Drive links)
# ---------------------------------------------------------------------------

class ResourceType(str, enum.Enum):
    worksheet = "worksheet"
    recording = "recording"
    link = "link"
    document = "document"
    other = "other"


class Resource(Base):
    """Google Drive links or external URLs assigned to classes/students.

    No native file storage — all resources are URLs.
    """
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resource_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    resource_type: Mapped[ResourceType] = mapped_column(
        Enum(ResourceType), nullable=False, default=ResourceType.link
    )
    # Scope — can be assigned to class, program, or individual student
    class_group_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("class_groups.id"), nullable=True
    )
    program_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("programs.id"), nullable=True
    )
    student_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    student: Mapped[Optional["User"]] = relationship("User", foreign_keys=[student_user_id])

    def __repr__(self) -> str:
        return f"<Resource id={self.id} title={self.title!r} type={self.resource_type}>"


# ---------------------------------------------------------------------------
# System Alerts
# ---------------------------------------------------------------------------

class AlertSeverity(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class AlertStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"
    dismissed = "dismissed"


class SystemAlert(Base):
    """Auto-generated operational alerts shown in the admin dashboard.

    Examples:
    - Missing progress report after lesson
    - Student payment overdue
    - Tutor unavailability conflicts with class
    - Attendance mismatch (tutor says present, Discord shows absent)
    """
    __tablename__ = "system_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), nullable=False, default=AlertSeverity.medium
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Related entity for deep-link action buttons
    related_entity_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    related_entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), nullable=False, default=AlertStatus.open
    )
    assigned_to: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    assignee: Mapped[Optional["User"]] = relationship("User", foreign_keys=[assigned_to])

    def __repr__(self) -> str:
        return (
            f"<SystemAlert id={self.id} type={self.alert_type!r} "
            f"severity={self.severity} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Email Templates (for one-click reminder actions)
# ---------------------------------------------------------------------------

class EmailTemplateType(str, enum.Enum):
    payment_reminder = "payment_reminder"
    progress_report_reminder = "progress_report_reminder"
    homework_reminder = "homework_reminder"
    parent_invite = "parent_invite"
    general = "general"


class EmailTemplate(Base):
    """Configurable email templates for admin one-click actions.

    Variables available: {parent_name}, {student_name}, {amount_due},
    {due_date}, {class_name}, {lesson_date}, {tutor_name}, {portal_link}
    """
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    template_type: Mapped[EmailTemplateType] = mapped_column(
        Enum(EmailTemplateType), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<EmailTemplate id={self.id} name={self.name!r} type={self.template_type}>"
