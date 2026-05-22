import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.lesson import Lesson
    from bot.models.class_group import ClassGroup
    from bot.models.user import User


class SubmissionStatus(str, enum.Enum):
    submitted = "submitted"
    late = "late"
    missing = "missing"
    reviewed = "reviewed"


class HomeworkTask(Base):
    __tablename__ = "homework_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=True, index=True
    )
    class_group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("class_groups.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    lesson: Mapped[Optional["Lesson"]] = relationship("Lesson", back_populates="homework_tasks")
    class_group: Mapped["ClassGroup"] = relationship("ClassGroup")
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    submissions: Mapped[List["HomeworkSubmission"]] = relationship(
        "HomeworkSubmission", back_populates="homework_task", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<HomeworkTask id={self.id} title={self.title!r} due={self.due_at}>"


class HomeworkSubmission(Base):
    __tablename__ = "homework_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    homework_task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("homework_tasks.id"), nullable=False, index=True
    )
    student_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attachment_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus), nullable=False, default=SubmissionStatus.missing
    )
    tutor_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    homework_task: Mapped["HomeworkTask"] = relationship("HomeworkTask", back_populates="submissions")
    student: Mapped["User"] = relationship("User", back_populates="homework_submissions")

    def __repr__(self) -> str:
        return f"<HomeworkSubmission task={self.homework_task_id} student={self.student_user_id} status={self.status}>"
