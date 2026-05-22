from datetime import datetime, timezone, timedelta

import pytz

from bot import config

SYDNEY_TZ = pytz.timezone(config.DEFAULT_TIMEZONE)


def now_sydney() -> datetime:
    """Current time as a timezone-aware datetime in Sydney time."""
    return datetime.now(SYDNEY_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_sydney(dt: datetime) -> datetime:
    """Convert any aware datetime to Sydney timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SYDNEY_TZ)


def to_utc(dt: datetime) -> datetime:
    """Convert any aware datetime to UTC."""
    if dt.tzinfo is None:
        dt = SYDNEY_TZ.localize(dt)
    return dt.astimezone(timezone.utc)


def format_sydney(dt: datetime, fmt: str = "%A, %d %B %Y at %I:%M %p") -> str:
    """Format a datetime in Sydney time."""
    return to_sydney(dt).strftime(fmt)


def format_lesson_date(dt: datetime) -> str:
    """Short date for thread titles, e.g. '15 Jun'.

    Uses %d + lstrip to avoid the Linux-only '%-d' directive, which raises
    ValueError on Windows (and therefore fails in local tests / CI).
    """
    return to_sydney(dt).strftime("%d %b").lstrip("0")


def minutes_until(dt: datetime) -> int:
    """Minutes from now until the given datetime (negative if in the past)."""
    delta = dt - now_utc()
    return int(delta.total_seconds() / 60)


def lesson_duration_minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() / 60))
