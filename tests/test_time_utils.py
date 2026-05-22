"""Tests for timezone utilities and time formatting functions."""
import pytest
from datetime import datetime, timezone, timedelta

from bot.utils.time_utils import (
    SYDNEY_TZ,
    format_lesson_date,
    format_sydney,
    lesson_duration_minutes,
    minutes_until,
    now_sydney,
    now_utc,
    to_sydney,
    to_utc,
)


# ---------------------------------------------------------------------------
# Timezone conversion
# ---------------------------------------------------------------------------

class TestTimezoneConversions:
    def test_to_sydney_from_utc_winter(self):
        """June is AEST (UTC+10)."""
        utc_dt = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)  # 10am UTC
        sydney_dt = to_sydney(utc_dt)
        assert sydney_dt.hour == 20  # 10am UTC = 8pm AEST (+10)

    def test_to_sydney_from_utc_summer(self):
        """January is AEDT (UTC+11)."""
        utc_dt = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)  # 10am UTC
        sydney_dt = to_sydney(utc_dt)
        assert sydney_dt.hour == 21  # 10am UTC = 9pm AEDT (+11)

    def test_to_utc_from_sydney_winter(self):
        """8pm AEST → 10am UTC."""
        sydney_dt = SYDNEY_TZ.localize(datetime(2025, 6, 1, 20, 0))
        utc_dt = to_utc(sydney_dt)
        assert utc_dt.tzinfo == timezone.utc
        assert utc_dt.hour == 10

    def test_to_utc_from_sydney_summer(self):
        """9pm AEDT → 10am UTC."""
        sydney_dt = SYDNEY_TZ.localize(datetime(2025, 1, 15, 21, 0))
        utc_dt = to_utc(sydney_dt)
        assert utc_dt.tzinfo == timezone.utc
        assert utc_dt.hour == 10

    def test_to_sydney_naive_dt_treated_as_utc(self):
        """Naive datetime should be localised as UTC, then converted to Sydney."""
        naive = datetime(2025, 6, 1, 10, 0)  # Naive → treated as UTC
        result = to_sydney(naive)
        assert result.tzinfo is not None
        assert result.hour == 20  # 8pm AEST

    def test_to_utc_naive_dt_treated_as_sydney(self):
        """Naive datetime passed to to_utc() should be localised as Sydney first."""
        naive = datetime(2025, 6, 1, 20, 0)  # Naive → treated as Sydney
        result = to_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.hour == 10  # 8pm AEST = 10am UTC

    def test_roundtrip_utc_sydney_utc(self):
        utc_orig = datetime(2025, 6, 15, 14, 30, tzinfo=timezone.utc)
        assert to_utc(to_sydney(utc_orig)).replace(microsecond=0) == utc_orig.replace(microsecond=0)

    def test_now_utc_has_utc_tzinfo(self):
        result = now_utc()
        assert result.tzinfo == timezone.utc

    def test_now_sydney_has_sydney_tzinfo(self):
        result = now_sydney()
        assert str(result.tzinfo) == "Australia/Sydney"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_format_sydney_hour_minute(self):
        """10am UTC = 8pm AEST."""
        utc_dt = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        result = format_sydney(utc_dt, "%H:%M")
        assert result == "20:00"

    def test_format_sydney_12h_am_pm(self):
        """10am UTC = 8pm AEST."""
        utc_dt = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        result = format_sydney(utc_dt, "%I:%M %p")
        assert "08:00 PM" in result or "8:00 PM" in result

    def test_format_lesson_date_does_not_raise(self):
        """Must not raise ValueError on Windows (%-d is Linux-only)."""
        utc_dt = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        result = format_lesson_date(utc_dt)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_lesson_date_contains_month(self):
        utc_dt = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        result = format_lesson_date(utc_dt)
        assert "Jun" in result

    def test_format_lesson_date_single_digit_day_no_leading_zero(self):
        """1 Jun should appear as '1 Jun', not '01 Jun'."""
        # June 1, 10am UTC = June 1, 8pm AEST
        utc_dt = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        result = format_lesson_date(utc_dt)
        assert result.startswith("1 ") or result == "1 Jun"
        assert not result.startswith("0")

    def test_format_lesson_date_double_digit_day(self):
        """15 Jun should appear as '15 Jun'."""
        utc_dt = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        result = format_lesson_date(utc_dt)
        assert result.startswith("15 ")


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

class TestTimingHelpers:
    def test_minutes_until_future(self):
        future = now_utc() + timedelta(minutes=60)
        result = minutes_until(future)
        assert 59 <= result <= 61

    def test_minutes_until_past(self):
        past = now_utc() - timedelta(minutes=30)
        result = minutes_until(past)
        assert result <= -29

    def test_lesson_duration_minutes_full_hour(self):
        start = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc)
        assert lesson_duration_minutes(start, end) == 60

    def test_lesson_duration_minutes_90_min(self):
        start = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, 11, 30, tzinfo=timezone.utc)
        assert lesson_duration_minutes(start, end) == 90

    def test_lesson_duration_same_time_is_zero(self):
        dt = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        assert lesson_duration_minutes(dt, dt) == 0

    def test_lesson_duration_end_before_start_returns_zero(self):
        """Defensive: end before start should not return negative."""
        start = datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        assert lesson_duration_minutes(start, end) == 0
