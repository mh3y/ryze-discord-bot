import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.class_group import ClassGroupMember
    from bot.models.attendance import AttendanceRecord, VoiceSession
    from bot.models.homework import HomeworkSubmission
    from bot.models.portal_auth import StudentParentLink, StudentProfile
    from bot.models.payment import StudentPayment, TutorPayment


class UserRole(str, enum.Enum):
    student = "student"
    tutor = "tutor"
    admin = "admin"
    parent = "parent"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.student)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    class_memberships: Mapped[List["ClassGroupMember"]] = relationship(
        "ClassGroupMember", back_populates="user", cascade="all, delete-orphan"
    )
    attendance_records: Mapped[List["AttendanceRecord"]] = relationship(
        "AttendanceRecord", back_populates="user"
    )
    voice_sessions: Mapped[List["VoiceSession"]] = relationship(
        "VoiceSession", back_populates="user"
    )
    homework_submissions: Mapped[List["HomeworkSubmission"]] = relationship(
        "HomeworkSubmission", back_populates="student"
    )
    # Portal-specific relationships
    parent_links: Mapped[List["StudentParentLink"]] = relationship(
        "StudentParentLink", foreign_keys="StudentParentLink.student_user_id", back_populates="student"
    )
    student_profile: Mapped[Optional["StudentProfile"]] = relationship(
        "StudentProfile", back_populates="user", uselist=False
    )
    payments: Mapped[List["StudentPayment"]] = relationship(
        "StudentPayment", foreign_keys="StudentPayment.student_user_id", back_populates="student"
    )
    tutor_payments: Mapped[List["TutorPayment"]] = relationship(
        "TutorPayment", foreign_keys="TutorPayment.tutor_user_id", back_populates="tutor"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} discord={self.discord_user_id} name={self.full_name!r}>"
