import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.lesson import Lesson
    from bot.models.user import User


class ReminderChannel(str, enum.Enum):
    dm = "dm"
    class_channel = "class_channel"


class ReminderLog(Base):
    __tablename__ = "reminder_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=False, index=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    reminder_type: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[ReminderChannel] = mapped_column(Enum(ReminderChannel), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    lesson: Mapped["Lesson"] = relationship("Lesson", back_populates="reminder_logs")
    user: Mapped[Optional["User"]] = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<ReminderLog lesson={self.lesson_id} user={self.user_id} "
            f"type={self.reminder_type!r} channel={self.channel}>"
        )
