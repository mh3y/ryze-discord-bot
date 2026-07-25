"""Google Calendar API wrapper — fetches and parses calendar events."""
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from bot import config
from bot.utils.time_utils import SYDNEY_TZ

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Cached OAuth credentials. A token refresh is a network round-trip; the old
# code refreshed on EVERY calendar fetch (once per class per sync cycle), so a
# 10-class sync did 10 token exchanges. Cache the Credentials and refresh only
# when the access token is missing/expired (~once per token lifetime). The lock
# guards the refresh because fetches are offloaded to executor threads. [M3/DEF-10]
_creds_lock = threading.Lock()
_cached_creds: Credentials | None = None


def _build_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=config.GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


def _get_credentials() -> Credentials:
    """Return valid credentials, refreshing the access token at most once per
    token lifetime rather than per calendar fetch. [M3]"""
    global _cached_creds
    with _creds_lock:
        if _cached_creds is None:
            _cached_creds = _build_credentials()
        if not _cached_creds.valid:
            try:
                _cached_creds.refresh(Request())
            except Exception:
                # A failed refresh (e.g. a rotated/revoked refresh token) must not
                # leave a poisoned cache — drop it so the next call rebuilds, and
                # surface the error rather than silently returning stale creds.
                _cached_creds = None
                raise
        return _cached_creds


def reset_credentials_cache() -> None:
    """Drop the cached credentials (test/ops helper)."""
    global _cached_creds
    with _creds_lock:
        _cached_creds = None


def _get_service():
    # A fresh service is built per call (cheap) from the cached, auto-validated
    # credentials, so no google-api http object is shared across executor threads.
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_event_time(time_obj: dict) -> datetime:
    """Parse a Google Calendar event time dict into a UTC-aware datetime."""
    if "dateTime" in time_obj:
        dt = datetime.fromisoformat(time_obj["dateTime"])
        if dt.tzinfo is None:
            dt = SYDNEY_TZ.localize(dt)
        return dt.astimezone(timezone.utc)
    # All-day event (date only) — treat as midnight Sydney time
    date_str = time_obj["date"]
    naive = datetime.strptime(date_str, "%Y-%m-%d")
    return SYDNEY_TZ.localize(naive).astimezone(timezone.utc)


def _extract_meet_link(event: dict) -> str | None:
    """Pull Google Meet link from conference data or description."""
    conf = event.get("conferenceData", {})
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            return ep.get("uri")
    return None


def list_calendars() -> list[dict]:
    """Return all calendars visible to the authenticated Google account.

    Each entry has:
        id          — the calendar ID (e.g. abc123@group.calendar.google.com)
        name        — the calendar summary / display name
        description — optional description text (useful for metadata)
        primary     — True if this is the account's primary calendar
    """
    try:
        service = _get_service()
        result = service.calendarList().list(showHidden=False).execute()
        calendars = []
        for item in result.get("items", []):
            calendars.append({
                "id":          item["id"],
                "name":        item.get("summary", ""),
                "description": item.get("description", ""),
                "primary":     item.get("primary", False),
            })
        log.info("Discovered %d calendar(s) from Google account.", len(calendars))
        return calendars
    except HttpError as exc:
        log.error("Could not list Google calendars (HttpError): %s", exc)
        return []
    except Exception as exc:
        # Catches google.auth.exceptions.RefreshError, TransportError, etc.
        log.error("Could not list Google calendars (%s): %s", type(exc).__name__, exc)
        raise  # Re-raise so the caller can log it as a sync failure


def fetch_upcoming_events(calendar_id: str, days_ahead: int = 30, days_back: int = 14) -> list[dict]:
    """Return parsed event dicts for the given calendar.

    Fetches from *days_back* days ago through *days_ahead* days into the future.
    The lookback window ensures that lessons missed during a token outage are
    backfilled automatically on the next successful sync.
    """
    now = datetime.now(timezone.utc)
    time_min = now - timedelta(days=days_back)
    time_max = now + timedelta(days=days_ahead)

    try:
        service = _get_service()
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
    except HttpError as exc:
        # RAISE, don't return [] — a swallowed API error made a failed fetch look
        # like a calendar with zero events (logged as a successful, empty sync),
        # so a Google outage silently read as "all lessons gone" for freshness and
        # the sync-log status. The caller (sync_all_calendars) counts this as that
        # group's error and moves on; the 14-day lookback backfills next cycle. [M11]
        log.error("Google Calendar API error for %s (HttpError): %s", calendar_id, exc)
        raise
    except Exception as exc:
        # Catches google.auth.exceptions.RefreshError, TransportError, etc.
        log.error("Google Calendar API error for %s (%s): %s", calendar_id, type(exc).__name__, exc)
        raise  # Re-raise so the caller records it as a sync failure

    events = []
    skipped = 0
    for item in result.get("items", []):
        if item.get("status") == "cancelled":
            events.append({"google_event_id": item["id"], "cancelled": True})
            continue

        try:
            events.append(
                {
                    "google_event_id": item["id"],
                    "cancelled": False,
                    "title": item.get("summary", "Lesson"),
                    "description": item.get("description"),
                    "start_time": _parse_event_time(item["start"]),
                    "end_time": _parse_event_time(item["end"]),
                    "location": item.get("location"),
                    "meet_link": _extract_meet_link(item),
                }
            )
        except Exception as exc:
            skipped += 1
            log.warning(
                "Could not parse calendar event %s (title=%r): %s",
                item.get("id"), item.get("summary"), exc,
            )

    if skipped:
        log.error(
            "Calendar %r: %d event(s) were skipped due to parse errors — check the warnings above.",
            calendar_id, skipped,
        )

    return events
