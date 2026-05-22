import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.user import User
    from bot.models.lesson import Lesson
    from bot.models.academic import Program


class MemberRole(str, enum.Enum):
    student = "student"
    tutor = "tutor"


class EnrollmentStatus(str, enum.Enum):
    active = "active"
    trial = "trial"
    paused = "paused"
    withdrawn = "withdrawn"


class DeliveryMode(str, enum.Enum):
    online = "online"
    in_person = "in_person"
    hybrid = "hybrid"


class ClassGroup(Base):
    __tablename__ = "class_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    year_level: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # FK to programs table (optional — backfill from existing class groups later)
    program_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("programs.id"), nullable=True, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivery_mode: Mapped[DeliveryMode] = mapped_column(
        Enum(DeliveryMode), nullable=False, default=DeliveryMode.online
    )
    default_duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    discord_role_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    discord_text_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    discord_voice_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    tutor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    google_calendar_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    tutor: Mapped[Optional["User"]] = relationship("User", foreign_keys=[tutor_user_id])
    program: Mapped[Optional["Program"]] = relationship("Program", back_populates="class_groups")
    members: Mapped[List["ClassGroupMember"]] = relationship(
        "ClassGroupMember", back_populates="class_group", cascade="all, delete-orphan"
    )
    lessons: Mapped[List["Lesson"]] = relationship(
        "Lesson", back_populates="class_group", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ClassGroup id={self.id} name={self.name!r}>"


class ClassGroupMember(Base):
    __tablename__ = "class_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    class_group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("class_groups.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    role: Mapped[MemberRole] = mapped_column(Enum(MemberRole), nullable=False, default=MemberRole.student)
    # Enhanced enrollment tracking for portal
    enrollment_status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus), nullable=False, default=EnrollmentStatus.active
    )
    start_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    class_group: Mapped["ClassGroup"] = relationship("ClassGroup", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="class_memberships")

    def __repr__(self) -> str:
        return (
            f"<ClassGroupMember class={self.class_group_id} user={self.user_id} "
            f"role={self.role} status={self.enrollment_status}>"
        )
