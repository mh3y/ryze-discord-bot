"""Payment tracking models.

No real payment processing — this is manual tracking only.
Admin marks records paid/pending/overdue and can trigger reminder emails.
"""
import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.user import User
    from bot.models.lesson import Lesson
    from bot.models.portal_auth import ParentProfile


class PaymentStatus(str, enum.Enum):
    paid = "paid"
    pending = "pending"
    overdue = "overdue"
    waived = "waived"


class PaymentMethod(str, enum.Enum):
    bank_transfer = "bank_transfer"
    cash = "cash"
    other = "other"


class TutorPaymentStatus(str, enum.Enum):
    unpaid = "unpaid"
    pending = "pending"
    paid = "paid"


class StudentPayment(Base):
    """One payment record per student per term (or custom period).

    Admin creates these and updates status as payments are received.
    Parents can view their children's payment status in the portal.
    """
    __tablename__ = "student_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    parent_profile_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("parent_profiles.id"), nullable=True, index=True
    )
    # e.g. "Term 1 2026", "Week 3 Feb"
    term: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_due: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), nullable=False, default=PaymentStatus.pending
    )
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(
        Enum(PaymentMethod), nullable=True
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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

    student: Mapped["User"] = relationship("User", back_populates="payments")
    parent: Mapped[Optional["ParentProfile"]] = relationship(
        "ParentProfile", back_populates="payments"
    )
    logs: Mapped[List["StudentPaymentLog"]] = relationship(
        "StudentPaymentLog", back_populates="payment", cascade="all, delete-orphan"
    )

    @property
    def amount_remaining(self) -> float:
        return float(self.amount_due) - float(self.amount_paid)

    def __repr__(self) -> str:
        return (
            f"<StudentPayment id={self.id} student={self.student_user_id} "
            f"term={self.term!r} status={self.status}>"
        )


class StudentPaymentLog(Base):
    """Audit trail — every status change on a student payment."""
    __tablename__ = "student_payment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("student_payments.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    old_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    new_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    performed_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    payment: Mapped["StudentPayment"] = relationship("StudentPayment", back_populates="logs")
    performed_by_user: Mapped["User"] = relationship("User", foreign_keys=[performed_by])

    def __repr__(self) -> str:
        return f"<StudentPaymentLog payment={self.payment_id} action={self.action!r}>"


class TutorPayment(Base):
    """Tutor pay run for a given period.

    Admin creates pay periods and adds lesson items.
    Tutors can view their status (read-only).
    """
    __tablename__ = "tutor_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tutor_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    pay_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pay_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_due: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    amount_paid: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    status: Mapped[TutorPaymentStatus] = mapped_column(
        Enum(TutorPaymentStatus), nullable=False, default=TutorPaymentStatus.unpaid
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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

    tutor: Mapped["User"] = relationship("User", back_populates="tutor_payments")
    items: Mapped[List["TutorPaymentItem"]] = relationship(
        "TutorPaymentItem", back_populates="payment", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<TutorPayment id={self.id} tutor={self.tutor_user_id} "
            f"period={self.pay_period_start.date()}..{self.pay_period_end.date()} "
            f"status={self.status}>"
        )


class TutorPaymentItem(Base):
    """One lesson's contribution to a tutor pay run."""
    __tablename__ = "tutor_payment_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tutor_payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tutor_payments.id"), nullable=False, index=True
    )
    lesson_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lessons.id"), nullable=False
    )
    hours: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    payment: Mapped["TutorPayment"] = relationship("TutorPayment", back_populates="items")
    lesson: Mapped["Lesson"] = relationship("Lesson")

    def __repr__(self) -> str:
        return f"<TutorPaymentItem payment={self.tutor_payment_id} lesson={self.lesson_id} amount={self.amount}>"
