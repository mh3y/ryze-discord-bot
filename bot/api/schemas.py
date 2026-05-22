"""Pydantic response schemas for the dashboard API."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Users / Students
# ---------------------------------------------------------------------------

class UserSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    discord_user_id: int
    full_name: str
    email: Optional[str]
    role: str
    active: bool
    created_at: datetime


class UserListResponse(BaseModel):
    total: int
    items: list[UserSchema]


# ---------------------------------------------------------------------------
# Class Groups
# ---------------------------------------------------------------------------

class TutorSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    full_name: str
    discord_user_id: int


class ClassGroupSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    year_level: Optional[str]
    subject: Optional[str]
    active: bool
    tutor: Optional[TutorSummary]
    member_count: int
    created_at: datetime


class ClassGroupListResponse(BaseModel):
    total: int
    items: list[ClassGroupSchema]


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------

class LessonSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    class_group_id: int
    class_group_name: str
    title: str
    description: Optional[str]
    start_time: datetime
    end_time: datetime
    location: Optional[str]
    meet_link: Optional[str]
    status: str
    discord_thread_id: Optional[int]
    google_event_id: Optional[str]


class LessonListResponse(BaseModel):
    total: int
    items: list[LessonSchema]


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

class AttendanceSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lesson_id: int
    lesson_title: str
    user_id: int
    student_name: str
    status: str
    joined_at: Optional[datetime]
    left_at: Optional[datetime]
    duration_minutes: Optional[int]


class AttendanceListResponse(BaseModel):
    total: int
    items: list[AttendanceSchema]


# ---------------------------------------------------------------------------
# Reminder Logs
# ---------------------------------------------------------------------------

class ReminderLogSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lesson_id: int
    lesson_title: Optional[str]
    user_id: Optional[int]
    reminder_type: str
    channel: str
    sent_at: datetime
    success: bool
    error_message: Optional[str]


class ReminderLogListResponse(BaseModel):
    total: int
    items: list[ReminderLogSchema]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    db_ok: bool
    student_count: int
    class_count: int
    upcoming_lessons: int
    timestamp: datetime
