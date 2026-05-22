import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.class_group import ClassGroup
    from bot.models.attendance import AttendanceRecord, VoiceSession
    from bot.models.homework import HomeworkTask
    from bot.models.reminder_log import ReminderLog


class LessonStatus(str, enum.Enum):
    scheduled = "scheduled"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    class_group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("class_groups.id"), nullable=False, index=True
    )
    google_event_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    meet_link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    discord_thread_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[LessonStatus] = mapped_column(
        Enum(LessonStatus), nullable=False, default=LessonStatus.scheduled
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

    class_group: Mapped["ClassGroup"] = relationship("ClassGroup", back_populates="lessons")
    attendance_records: Mapped[List["AttendanceRecord"]] = relationship(
        "AttendanceRecord", back_populates="lesson", cascade="all, delete-orphan"
    )
    voice_sessions: Mapped[List["VoiceSession"]] = relationship(
        "VoiceSession", back_populates="lesson", cascade="all, delete-orphan"
    )
    homework_tasks: Mapped[List["HomeworkTask"]] = relationship(
        "HomeworkTask", back_populates="lesson"
    )
    reminder_logs: Mapped[List["ReminderLog"]] = relationship(
        "ReminderLog", back_populates="lesson", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Lesson id={self.id} title={self.title!r} start={self.start_time}>"
