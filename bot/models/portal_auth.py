"""Portal authentication models.

Students and tutors authenticate via Discord OAuth (their Discord ID is their identity).
Parents use email/password since they are not Discord users.

ParentProfile  — the parent's data record
PortalCredential — email + bcrypt password hash for parent login
StudentProfile — extended student data (school, preferred name, notes)
StudentParentLink — links a student (User) to a ParentProfile
"""
import enum
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base

if TYPE_CHECKING:
    from bot.models.user import User
    from bot.models.payment import StudentPayment


class ParentProfile(Base):
    """Portal-only account for parents/guardians.

    Parents do not need a Discord account.  They log into the portal
    using email + password and can only view their linked children's data.
    """
    __tablename__ = "parent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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

    credential: Mapped[Optional["PortalCredential"]] = relationship(
        "PortalCredential", back_populates="parent", uselist=False
    )
    student_links: Mapped[List["StudentParentLink"]] = relationship(
        "StudentParentLink", back_populates="parent"
    )
    payments: Mapped[List["StudentPayment"]] = relationship(
        "StudentPayment", back_populates="parent"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<ParentProfile id={self.id} name={self.full_name!r} email={self.email!r}>"


class PortalCredential(Base):
    """Email + bcrypt password for parent portal login.

    Students/tutors/admins use Discord OAuth — they do NOT need this table.
    """
    __tablename__ = "portal_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("parent_profiles.id"), nullable=False, unique=True
    )
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Invite token — set when admin creates account, cleared after parent sets password
    invite_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True, index=True)
    invite_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    invite_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    parent: Mapped["ParentProfile"] = relationship("ParentProfile", back_populates="credential")

    @property
    def has_set_password(self) -> bool:
        return self.password_hash is not None

    @property
    def invite_pending(self) -> bool:
        return self.invite_token is not None and not self.has_set_password

    def __repr__(self) -> str:
        return f"<PortalCredential parent_id={self.parent_profile_id} set={self.has_set_password}>"


class ParentRelationship(str, enum.Enum):
    mother = "mother"
    father = "father"
    guardian = "guardian"
    other = "other"


class StudentParentLink(Base):
    """Links a student (users table) to a parent (parent_profiles table)."""
    __tablename__ = "student_parent_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    parent_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("parent_profiles.id"), nullable=False, index=True
    )
    relationship_type: Mapped[ParentRelationship] = mapped_column(
        "relationship",  # keeps the DB column name unchanged
        Enum(ParentRelationship), nullable=False, default=ParentRelationship.guardian
    )
    is_primary_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    student: Mapped["User"] = relationship("User", back_populates="parent_links")
    parent: Mapped["ParentProfile"] = relationship("ParentProfile", back_populates="student_links")

    def __repr__(self) -> str:
        return (
            f"<StudentParentLink student={self.student_user_id} "
            f"parent={self.parent_profile_id} rel={self.relationship}>"
        )


class StudentProfile(Base):
    """Extended student details beyond what the core users table stores.

    One row per student user.  School, preferred name, and admin notes live here.
    """
    __tablename__ = "student_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    preferred_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    school: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    year_level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
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

    user: Mapped["User"] = relationship("User", back_populates="student_profile")

    def __repr__(self) -> str:
        return f"<StudentProfile user_id={self.user_id} school={self.school!r}>"
