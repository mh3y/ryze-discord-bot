"""Google Calendar API wrapper — fetches and parses calendar events."""
import logging
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


def _build_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=config.GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


def _get_service():
    creds = _build_credentials()
    creds.refresh(Request())
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
        log.error("Could not list Google calendars: %s", exc)
        return []


def fetch_upcoming_events(calendar_id: str, days_ahead: int = 30) -> list[dict]:
    """Return parsed event dicts for the given calendar over the next *days_ahead* days."""
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)

    try:
        service = _get_service()
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
    except HttpError as exc:
        log.error("Google Calendar API error for %s: %s", calendar_id, exc)
        return []

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
