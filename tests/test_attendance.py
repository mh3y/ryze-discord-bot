"""Tests for attendance calculation logic."""
import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from bot.models.attendance import AttendanceStatus
from bot.models.lesson import LessonStatus
from bot.services.attendance_service import calculate_attendance_status


def make_lesson(start_hour: int = 10, duration_minutes: int = 60) -> SimpleNamespace:
    start = datetime(2025, 6, 1, start_hour, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=duration_minutes)
    return SimpleNamespace(
        id=1,
        title="Test Lesson",
        start_time=start,
        end_time=end,
        status=LessonStatus.active,
    )


class TestCalculateAttendanceStatus:
    def test_present_full_attendance(self):
        lesson = make_lesson(duration_minutes=60)
        first_joined = lesson.start_time + timedelta(minutes=5)
        status = calculate_attendance_status(lesson, first_joined, total_minutes=55)
        assert status == AttendanceStatus.present

    def test_present_exactly_at_threshold(self):
        lesson = make_lesson(duration_minutes=60)
        first_joined = lesson.start_time
        # 70% of 60 = 42 minutes
        status = calculate_attendance_status(lesson, first_joined, total_minutes=42)
        assert status == AttendanceStatus.present

    def test_late_but_present_enough(self):
        lesson = make_lesson(duration_minutes=60)
        # Joined 20 minutes after start — late
        first_joined = lesson.start_time + timedelta(minutes=20)
        # Still attended 42 of 60 minutes
        status = calculate_attendance_status(lesson, first_joined, total_minutes=42)
        assert status == AttendanceStatus.late

    def test_left_early(self):
        lesson = make_lesson(duration_minutes=60)
        # Joined on time but only stayed for 30 of 60 minutes
        first_joined = lesson.start_time + timedelta(minutes=2)
        status = calculate_attendance_status(lesson, first_joined, total_minutes=30)
        assert status == AttendanceStatus.left_early

    def test_left_early_when_late_and_short(self):
        lesson = make_lesson(duration_minutes=60)
        first_joined = lesson.start_time + timedelta(minutes=20)
        status = calculate_attendance_status(lesson, first_joined, total_minutes=20)
        assert status == AttendanceStatus.left_early

    def test_present_boundary_on_time(self):
        lesson = make_lesson(duration_minutes=60)
        first_joined = lesson.start_time
        status = calculate_attendance_status(lesson, first_joined, total_minutes=60)
        assert status == AttendanceStatus.present

    def test_late_boundary(self):
        lesson = make_lesson(duration_minutes=60)
        # Joined exactly 15 minutes in — still on time (boundary is > 15)
        first_joined = lesson.start_time + timedelta(minutes=15)
        status = calculate_attendance_status(lesson, first_joined, total_minutes=45)
        assert status == AttendanceStatus.present

    def test_late_boundary_over(self):
        lesson = make_lesson(duration_minutes=60)
        # Joined 16 minutes in — late
        first_joined = lesson.start_time + timedelta(minutes=16)
        status = calculate_attendance_status(lesson, first_joined, total_minutes=44)
        assert status == AttendanceStatus.late

    def test_short_lesson(self):
        lesson = make_lesson(duration_minutes=30)
        # 70% of 30 = 21 minutes
        first_joined = lesson.start_time
        status = calculate_attendance_status(lesson, first_joined, total_minutes=21)
        assert status == AttendanceStatus.present

    def test_short_lesson_left_early(self):
        lesson = make_lesson(duration_minutes=30)
        first_joined = lesson.start_time
        status = calculate_attendance_status(lesson, first_joined, total_minutes=10)
        assert status == AttendanceStatus.left_early
