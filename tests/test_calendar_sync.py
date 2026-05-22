"""Tests for Google Calendar sync upsert logic (lesson_service.sync_calendar_for_group).

The Google API call is mocked — we only test the DB upsert behaviour.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from sqlalchemy import select

from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson, LessonStatus
from bot.services.lesson_service import sync_calendar_for_group


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    event_id: str,
    title: str = "Test Lesson",
    hours_from_now: int = 24,
    description: str | None = None,
    meet_link: str | None = None,
) -> dict:
    """Build a parsed event dict exactly as returned by fetch_upcoming_events."""
    start = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return {
        "google_event_id": event_id,
        "cancelled": False,
        "title": title,
        "description": description,
        "start_time": start,
        "end_time": start + timedelta(hours=1),
        "location": None,
        "meet_link": meet_link,
    }


def _make_cancelled(event_id: str) -> dict:
    return {"google_event_id": event_id, "cancelled": True}


async def _make_group(session, calendar_id: str | None = "cal@group.calendar.google.com") -> ClassGroup:
    group = ClassGroup(name="Sync Test Group", google_calendar_id=calendar_id, active=True)
    session.add(group)
    await session.flush()
    return group


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCalendarSyncUpsert:

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_new_event_creates_lesson(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_create_01")]

        counts = await sync_calendar_for_group(db_session, group)

        assert counts["created"] == 1
        assert counts["updated"] == 0
        assert counts["cancelled"] == 0

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_new_lesson_is_persisted_in_db(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        event = _make_event("evt_persist_01", title="Year 10 Maths")
        mock_fetch.return_value = [event]
        await sync_calendar_for_group(db_session, group)

        result = await db_session.execute(
            select(Lesson).where(Lesson.google_event_id == "evt_persist_01")
        )
        lesson = result.scalar_one_or_none()
        assert lesson is not None
        assert lesson.title == "Year 10 Maths"
        assert lesson.status == LessonStatus.scheduled

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_duplicate_event_id_not_created_twice(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_dup_01")]

        # First sync → creates
        counts1 = await sync_calendar_for_group(db_session, group)
        assert counts1["created"] == 1

        # Second sync with same event → skips (no change)
        counts2 = await sync_calendar_for_group(db_session, group)
        assert counts2["created"] == 0
        assert counts2["skipped"] == 1

        # Only one lesson in DB
        result = await db_session.execute(
            select(Lesson).where(Lesson.google_event_id == "evt_dup_01")
        )
        assert len(result.scalars().all()) == 1

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_updated_title_is_detected_and_saved(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        original = _make_event("evt_upd_01", title="Original Title")
        mock_fetch.return_value = [original]
        await sync_calendar_for_group(db_session, group)

        # Simulate a title change in Google Calendar
        updated = dict(original)
        updated["title"] = "Updated Title"
        mock_fetch.return_value = [updated]
        counts = await sync_calendar_for_group(db_session, group)

        assert counts["updated"] == 1
        result = await db_session.execute(
            select(Lesson).where(Lesson.google_event_id == "evt_upd_01")
        )
        lesson = result.scalar_one()
        assert lesson.title == "Updated Title"

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_unchanged_event_is_skipped(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        event = _make_event("evt_skip_01", title="Stable Title")
        mock_fetch.return_value = [event]
        await sync_calendar_for_group(db_session, group)

        # Sync again — nothing changed
        counts = await sync_calendar_for_group(db_session, group)
        assert counts["skipped"] == 1
        assert counts["updated"] == 0

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_cancelled_event_marks_lesson_cancelled(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_cancel_01")]
        await sync_calendar_for_group(db_session, group)

        # Google returns it as cancelled
        mock_fetch.return_value = [_make_cancelled("evt_cancel_01")]
        counts = await sync_calendar_for_group(db_session, group)

        assert counts["cancelled"] == 1
        result = await db_session.execute(
            select(Lesson).where(Lesson.google_event_id == "evt_cancel_01")
        )
        lesson = result.scalar_one()
        assert lesson.status == LessonStatus.cancelled

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_already_cancelled_lesson_is_skipped(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_cancel_02")]
        await sync_calendar_for_group(db_session, group)

        # Cancel it once
        mock_fetch.return_value = [_make_cancelled("evt_cancel_02")]
        await sync_calendar_for_group(db_session, group)

        # Cancel again — should skip, not double-count
        counts = await sync_calendar_for_group(db_session, group)
        assert counts["cancelled"] == 0
        assert counts["skipped"] == 1

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_meet_link_is_stored(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        event = _make_event("evt_meet_01", meet_link="https://meet.google.com/abc-xyz")
        mock_fetch.return_value = [event]
        await sync_calendar_for_group(db_session, group)

        result = await db_session.execute(
            select(Lesson).where(Lesson.google_event_id == "evt_meet_01")
        )
        lesson = result.scalar_one()
        assert lesson.meet_link == "https://meet.google.com/abc-xyz"

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_no_calendar_id_returns_zeros_without_api_call(self, mock_fetch, db_session):
        group = await _make_group(db_session, calendar_id=None)
        counts = await sync_calendar_for_group(db_session, group)

        mock_fetch.assert_not_called()
        assert counts["created"] == 0
        assert counts["updated"] == 0
        assert counts["cancelled"] == 0
        assert counts["skipped"] == 0
        assert counts["newly_cancelled"] == []

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_multiple_events_in_one_sync(self, mock_fetch, db_session):
        group = await _make_group(db_session)
        mock_fetch.return_value = [
            _make_event("evt_multi_01", title="Lesson A"),
            _make_event("evt_multi_02", title="Lesson B"),
            _make_event("evt_multi_03", title="Lesson C"),
        ]
        counts = await sync_calendar_for_group(db_session, group)
        assert counts["created"] == 3

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_newly_cancelled_populated_on_first_cancellation(self, mock_fetch, db_session):
        """newly_cancelled should contain the lesson when it first transitions to cancelled."""
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_nc_01")]
        await sync_calendar_for_group(db_session, group)

        mock_fetch.return_value = [_make_cancelled("evt_nc_01")]
        counts = await sync_calendar_for_group(db_session, group)

        assert counts["cancelled"] == 1
        assert len(counts["newly_cancelled"]) == 1
        assert counts["newly_cancelled"][0].google_event_id == "evt_nc_01"

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_newly_cancelled_empty_if_already_cancelled(self, mock_fetch, db_session):
        """Re-syncing an already-cancelled lesson should not re-populate newly_cancelled."""
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_nc_02")]
        await sync_calendar_for_group(db_session, group)

        # Cancel it
        mock_fetch.return_value = [_make_cancelled("evt_nc_02")]
        await sync_calendar_for_group(db_session, group)

        # Sync again — already cancelled, should not re-appear
        counts = await sync_calendar_for_group(db_session, group)
        assert counts["newly_cancelled"] == []

    @patch("bot.services.lesson_service.fetch_upcoming_events")
    async def test_newly_cancelled_empty_for_new_lessons(self, mock_fetch, db_session):
        """Creating new lessons should not populate newly_cancelled."""
        group = await _make_group(db_session)
        mock_fetch.return_value = [_make_event("evt_nc_03")]
        counts = await sync_calendar_for_group(db_session, group)
        assert counts["newly_cancelled"] == []


class TestCancellationMessages:
    """Tests for the cancellation message builders."""

    def _make_lesson_ns(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            id=10,
            title="Year 11 Advanced",
            start_time=datetime(2025, 7, 15, 9, 0, tzinfo=timezone.utc),
            end_time=datetime(2025, 7, 15, 10, 0, tzinfo=timezone.utc),
            meet_link=None,
            location=None,
        )

    def _make_group_ns(self):
        from types import SimpleNamespace
        return SimpleNamespace(id=1, name="Year 11 Advanced Maths", tutor=None)

    def test_cancellation_dm_contains_class_name(self):
        from bot.services.reminder_service import build_cancellation_dm
        lesson = self._make_lesson_ns()
        group = self._make_group_ns()
        msg = build_cancellation_dm(lesson, group)
        assert "Year 11 Advanced Maths" in msg
        assert "cancelled" in msg.lower()

    def test_cancellation_dm_contains_date(self):
        from bot.services.reminder_service import build_cancellation_dm
        lesson = self._make_lesson_ns()
        group = self._make_group_ns()
        msg = build_cancellation_dm(lesson, group)
        assert "2025" in msg or "July" in msg or "15" in msg

    def test_cancellation_channel_message_contains_class_name(self):
        from bot.services.reminder_service import build_cancellation_channel_message
        lesson = self._make_lesson_ns()
        group = self._make_group_ns()
        msg = build_cancellation_channel_message(lesson, group)
        assert "Year 11 Advanced Maths" in msg
        assert "cancelled" in msg.lower()

    def test_cancellation_channel_message_with_mentions(self):
        from bot.services.reminder_service import build_cancellation_channel_message
        lesson = self._make_lesson_ns()
        group = self._make_group_ns()
        mentions = ["<@111>", "<@222>"]
        msg = build_cancellation_channel_message(lesson, group, user_mentions=mentions)
        assert "<@111>" in msg
        assert "<@222>" in msg
        assert "Year 11 Advanced Maths" in msg

    def test_cancellation_channel_message_fallback_when_no_mentions(self):
        from bot.services.reminder_service import build_cancellation_channel_message
        lesson = self._make_lesson_ns()
        group = self._make_group_ns()
        msg = build_cancellation_channel_message(lesson, group, user_mentions=None)
        assert "Year 11 Advanced Maths" in msg
