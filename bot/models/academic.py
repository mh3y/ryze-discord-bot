"""Academic structure: Subjects and Programs sit above ClassGroups."""
import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.class_group import ClassGroup


class ItemStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"


class Subject(Base):
    """e.g. Mathematics, English, Science."""
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus), nullable=False, default=ItemStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    programs: Mapped[List["Program"]] = relationship("Program", back_populates="subject")

    def __repr__(self) -> str:
        return f"<Subject id={self.id} name={self.name!r}>"


class Program(Base):
    """e.g. HSC Maths, Year 10 Accelerated, Selective OC Prep."""
    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("subjects.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    year_level: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus), nullable=False, default=ItemStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    subject: Mapped["Subject"] = relationship("Subject", back_populates="programs")
    class_groups: Mapped[List["ClassGroup"]] = relationship(
        "ClassGroup", back_populates="program"
    )

    def __repr__(self) -> str:
        return f"<Program id={self.id} name={self.name!r}>"
