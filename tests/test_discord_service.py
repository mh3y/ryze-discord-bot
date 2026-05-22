"""Tests for discord_service helpers — pure unit tests, no Discord connection required."""
import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from bot.services.discord_service import lesson_thread_body, lesson_thread_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lesson(
    title: str = "Year 11 Advanced",
    hour_utc: int = 10,  # 8pm AEST (UTC+10, June)
    meet_link: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> SimpleNamespace:
    start = datetime(2025, 6, 15, hour_utc, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=1,
        title=title,
        start_time=start,
        end_time=start + timedelta(hours=1),
        meet_link=meet_link,
        location=location,
        description=description,
    )


def _make_group(name: str = "Year 11 Advanced", tutor_name: str | None = "Jake Tongko") -> SimpleNamespace:
    tutor = SimpleNamespace(full_name=tutor_name) if tutor_name else None
    return SimpleNamespace(id=1, name=name, tutor=tutor, discord_text_channel_id=None)


# ---------------------------------------------------------------------------
# Thread name
# ---------------------------------------------------------------------------

class TestLessonThreadName:
    def test_contains_class_name(self):
        name = lesson_thread_name(_make_lesson(), _make_group("Year 11 Advanced"))
        assert "Year 11 Advanced" in name

    def test_contains_month_abbreviation(self):
        name = lesson_thread_name(_make_lesson(), _make_group())
        assert "Jun" in name

    def test_starts_with_square_bracket(self):
        name = lesson_thread_name(_make_lesson(), _make_group())
        assert name.startswith("[")

    def test_format_is_date_then_class(self):
        """Expected format: '[15 Jun] Year 11 Advanced'."""
        name = lesson_thread_name(_make_lesson(), _make_group("Year 11 Advanced"))
        assert "]" in name
        assert name.index("]") < name.index("Year 11 Advanced")

    def test_different_classes_produce_different_names(self):
        lesson = _make_lesson()
        n1 = lesson_thread_name(lesson, _make_group("Year 10 Standard"))
        n2 = lesson_thread_name(lesson, _make_group("Year 12 Extension"))
        assert n1 != n2

    def test_no_leading_zero_in_date(self):
        """Day 1 should appear as '1 Jun', not '01 Jun'."""
        lesson = _make_lesson()
        lesson.start_time = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        lesson.end_time = lesson.start_time + timedelta(hours=1)
        name = lesson_thread_name(lesson, _make_group())
        # Should not contain "[01 "
        assert "[01 " not in name


# ---------------------------------------------------------------------------
# Thread body
# ---------------------------------------------------------------------------

class TestLessonThreadBody:
    def test_contains_lesson_title(self):
        body = lesson_thread_body(_make_lesson(title="Year 11 Advanced Maths"), _make_group())
        assert "Year 11 Advanced Maths" in body

    def test_contains_tutor_name(self):
        body = lesson_thread_body(_make_lesson(), _make_group(tutor_name="Jake Tongko"))
        assert "Jake Tongko" in body

    def test_contains_class_name(self):
        body = lesson_thread_body(_make_lesson(), _make_group("Year 11 Advanced"))
        assert "Year 11 Advanced" in body

    def test_fallback_to_your_tutor_when_no_tutor(self):
        body = lesson_thread_body(_make_lesson(), _make_group(tutor_name=None))
        assert "Your tutor" in body

    def test_contains_meet_link_when_present(self):
        lesson = _make_lesson(meet_link="https://meet.google.com/abc-def-ghi")
        body = lesson_thread_body(lesson, _make_group())
        assert "meet.google.com/abc-def-ghi" in body

    def test_meet_link_takes_priority_over_location(self):
        """If both are set, meet link should appear (not location)."""
        lesson = _make_lesson(meet_link="https://meet.google.com/xyz", location="Room 2B")
        body = lesson_thread_body(lesson, _make_group())
        assert "meet.google.com" in body
        # Location should NOT appear when meet link is present
        assert "Room 2B" not in body

    def test_contains_location_when_no_meet_link(self):
        lesson = _make_lesson(location="Room 2B, Collins Street Campus")
        body = lesson_thread_body(lesson, _make_group())
        assert "Room 2B, Collins Street Campus" in body

    def test_contains_workbook_reminder(self):
        body = lesson_thread_body(_make_lesson(), _make_group())
        assert "workbook" in body.lower()

    def test_contains_date_in_body(self):
        body = lesson_thread_body(_make_lesson(), _make_group())
        assert "Jun" in body

    def test_contains_time_range(self):
        body = lesson_thread_body(_make_lesson(hour_utc=10), _make_group())
        # 10am UTC = 8pm AEST — time should appear somewhere
        assert "PM" in body or "pm" in body.lower()

    def test_contains_description_when_present(self):
        lesson = _make_lesson(description="Focus: Quadratic equations and factoring.")
        body = lesson_thread_body(lesson, _make_group())
        assert "Quadratic equations" in body

    def test_no_description_no_extra_blank_content(self):
        lesson = _make_lesson(description=None)
        body = lesson_thread_body(lesson, _make_group())
        assert isinstance(body, str)
        assert len(body) > 50  # Must still have substantial content


# ---------------------------------------------------------------------------
# Reminder window detection (pure logic — mirrors reminders.py)
# ---------------------------------------------------------------------------

class TestReminderWindowDetection:
    """Verify the 1-minute fire-window logic used in the reminders cog."""

    def _should_fire(self, now: datetime, fire_at: datetime) -> bool:
        return fire_at <= now < fire_at + timedelta(minutes=1)

    def test_fires_at_exact_fire_time(self):
        fire_at = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        assert self._should_fire(fire_at, fire_at) is True

    def test_fires_59_seconds_after_fire_time(self):
        fire_at = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        now = fire_at + timedelta(seconds=59)
        assert self._should_fire(now, fire_at) is True

    def test_does_not_fire_exactly_1_minute_later(self):
        fire_at = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        now = fire_at + timedelta(minutes=1)
        assert self._should_fire(now, fire_at) is False

    def test_does_not_fire_before_fire_time(self):
        fire_at = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        now = fire_at - timedelta(seconds=1)
        assert self._should_fire(now, fire_at) is False

    def test_24h_fire_time_calculation(self):
        lesson_start = datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc)
        fire_at = lesson_start - timedelta(minutes=24 * 60)
        expected = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        assert fire_at == expected

    def test_1h_fire_time_calculation(self):
        lesson_start = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        fire_at = lesson_start - timedelta(minutes=60)
        expected = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
        assert fire_at == expected

    def test_15min_fire_time_calculation(self):
        lesson_start = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        fire_at = lesson_start - timedelta(minutes=15)
        expected = datetime(2025, 6, 1, 9, 45, tzinfo=timezone.utc)
        assert fire_at == expected
