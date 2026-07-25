"""Shared process-health state for the heartbeat dead-man's-switch. [M8]

The heartbeat must prove the bot WORKS, not merely that the process is alive —
the last outage was silent precisely because liveness looked fine. The calendar
sync loop records its successes here; the heartbeat cog refuses to ping when the
gateway is down or the last successful sync is stale, which lets the external
monitor fire and alert the owner.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

# Process start counts as the initial "activity" so a freshly-booted bot has one
# grace window before staleness can suppress the heartbeat.
_process_started_at: datetime = datetime.now(timezone.utc)
_last_sync_ok_at: Optional[datetime] = None


def record_sync_ok() -> None:
    """Called by the calendar-sync loop after a successful cycle."""
    global _last_sync_ok_at
    _last_sync_ok_at = datetime.now(timezone.utc)


def last_activity_at() -> datetime:
    return _last_sync_ok_at or _process_started_at


def is_fresh(max_age_minutes: int) -> bool:
    """True when the last successful sync (or process start) is recent enough."""
    age = datetime.now(timezone.utc) - last_activity_at()
    return age <= timedelta(minutes=max_age_minutes)


def reset_for_tests() -> None:
    """Test helper: restore boot state."""
    global _last_sync_ok_at, _process_started_at
    _last_sync_ok_at = None
    _process_started_at = datetime.now(timezone.utc)
