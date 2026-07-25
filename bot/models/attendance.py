import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.lesson import Lesson
    from bot.models.user import User


class AttendanceStatus(str, enum.Enum):
    present = "present"
    late = "late"
    left_early = "left_early"
    absent = "absent"
    excused = "excused"
    unknown = "unknown"


class MarkedBy(str, enum.Enum):
    system = "system"
    tutor = "tutor"
    admin = "admin"


class DiscordVerificationStatus(str, enum.Enum):
    """Cross-reference between tutor-marked status and Discord voice activity."""
    verified = "verified"       # Tutor + Discord both agree
    mismatch = "mismatch"       # Tutor says X, Discord says Y
    no_data = "no_data"         # Discord has no voice activity (student may have no Discord)
    pending = "pending"         # Not yet checked


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    # One attendance row per (lesson, user). Without this, the SELECT-then-INSERT in
    # get_or_create_attendance_record can race under concurrent voice events, splitting
    # total_minutes across duplicate rows and risking MultipleResultsFound (DEF-22 / M1).
    __table_args__ = (
        UniqueConstraint("lesson_id", "user_id", name="uq_attendance_lesson_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    # System auto-status (from Discord voice duration)
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus), nullable=False, default=AttendanceStatus.unknown
    )
    # Tutor explicitly overrides the status (this becomes the official record)
    tutor_marked_status: Mapped[Optional[AttendanceStatus]] = mapped_column(
        Enum(AttendanceStatus), nullable=True
    )
    tutor_marked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Discord verification — cross-references with voice_sessions
    discord_verification_status: Mapped[DiscordVerificationStatus] = mapped_column(
        Enum(DiscordVerificationStatus), nullable=False, default=DiscordVerificationStatus.pending
    )
    discord_verified_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    marked_by: Mapped[MarkedBy] = mapped_column(
        Enum(MarkedBy), nullable=False, default=MarkedBy.system
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    lesson: Mapped["Lesson"] = relationship("Lesson", back_populates="attendance_records")
    user: Mapped["User"] = relationship("User", back_populates="attendance_records")

    @property
    def final_status(self) -> AttendanceStatus:
        """Tutor override takes precedence; fall back to system status."""
        return self.tutor_marked_status or self.status

    def __repr__(self) -> str:
        return (
            f"<AttendanceRecord lesson={self.lesson_id} user={self.user_id} "
            f"status={self.final_status} discord={self.discord_verification_status}>"
        )


class VoiceSession(Base):
    __tablename__ = "voice_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    discord_voice_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    lesson: Mapped[Optional["Lesson"]] = relationship("Lesson", back_populates="voice_sessions")
    user: Mapped["User"] = relationship("User", back_populates="voice_sessions")

    def __repr__(self) -> str:
        return f"<VoiceSession id={self.id} user={self.user_id} lesson={self.lesson_id}>"
