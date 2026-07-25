"""Tests for local ClassGroup hydration from the portal (DEF-2 / DEF-3).

class_discovery provisions classes into the PORTAL only, so the local ClassGroup
table is empty in production and the entire local reminder/homework/cancellation
graph is inert. hydrate_local_class_groups mirrors the portal's classes into
local ClassGroup rows so the existing local-first sync + Discord-role enrollment
can populate the rest. These tests cover the pure upsert logic (no network).
"""
from unittest.mock import patch

from bot.models.class_group import ClassGroup
from bot.services.lesson_service import (
    _as_int_or_none,
    get_active_class_groups,
    hydrate_local_class_groups,
    hydrate_local_class_groups_from_portal,
)
from bot import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _portal_class(
    id: int = 1,
    name: str = "Year 11 Advanced Maths",
    subject: str | None = "Maths",
    google_calendar_id: str | None = "c_abc123@group.calendar.google.com",
    discord_channel_id=None,
    discord_role_id=None,
) -> dict:
    """A class object exactly as GET /api/bot/classes returns it (Discord IDs
    are strings in the portal, or absent)."""
    return {
        "id": id,
        "name": name,
        "subject": subject,
        "google_calendar_id": google_calendar_id,
        "discord_channel_id": discord_channel_id,
        "discord_role_id": discord_role_id,
    }


async def _get_group_by_cal(session, cal_id: str) -> ClassGroup | None:
    from sqlalchemy import select
    result = await session.execute(
        select(ClassGroup).where(ClassGroup.google_calendar_id == cal_id)
    )
    return result.scalar_one_or_none()


class _FakePortalClient:
    """Stand-in for PortalAPIClient usable as `async with ... as client`."""

    def __init__(self, classes: list[dict], raise_exc: Exception | None = None) -> None:
        self._classes = classes
        self._raise = raise_exc

    async def __aenter__(self) -> "_FakePortalClient":
        return self

    async def __aexit__(self, *_) -> bool:
        return False

    async def get_classes(self) -> list[dict]:
        if self._raise is not None:
            raise self._raise
        return self._classes


# ---------------------------------------------------------------------------
# _as_int_or_none
# ---------------------------------------------------------------------------

class TestAsIntOrNone:
    def test_parses_string_snowflake(self):
        assert _as_int_or_none("1490369427961675966") == 1490369427961675966

    def test_passes_through_int(self):
        assert _as_int_or_none(42) == 42

    def test_none_and_empty_become_none(self):
        assert _as_int_or_none(None) is None
        assert _as_int_or_none("") is None

    def test_unparseable_becomes_none_not_raise(self):
        assert _as_int_or_none("not-a-number") is None
        assert _as_int_or_none([1, 2]) is None


# ---------------------------------------------------------------------------
# hydrate_local_class_groups (pure upsert)
# ---------------------------------------------------------------------------

class TestHydrateLocalClassGroups:
    async def test_creates_local_group_from_portal_class(self, db_session):
        classes = [_portal_class(discord_channel_id="555", discord_role_id="777")]
        counts = await hydrate_local_class_groups(db_session, classes)

        assert counts == {"created": 1, "updated": 0, "skipped": 0, "deactivated": 0}
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group is not None
        assert group.name == "Year 11 Advanced Maths"
        assert group.subject == "Maths"
        assert group.active is True

    async def test_string_discord_ids_converted_to_int(self, db_session):
        classes = [_portal_class(discord_channel_id="555000111", discord_role_id="777000222")]
        await hydrate_local_class_groups(db_session, classes)

        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group.discord_text_channel_id == 555000111
        assert group.discord_role_id == 777000222

    async def test_missing_discord_ids_created_as_none(self, db_session):
        classes = [_portal_class()]  # no discord ids
        await hydrate_local_class_groups(db_session, classes)

        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group is not None
        assert group.discord_text_channel_id is None
        assert group.discord_role_id is None

    async def test_skips_class_without_calendar_id(self, db_session):
        classes = [_portal_class(google_calendar_id=None)]
        counts = await hydrate_local_class_groups(db_session, classes)

        assert counts == {"created": 0, "updated": 0, "skipped": 1, "deactivated": 0}

    async def test_idempotent_second_call_no_duplicate(self, db_session):
        classes = [_portal_class(discord_channel_id="555", discord_role_id="777")]
        first = await hydrate_local_class_groups(db_session, classes)
        second = await hydrate_local_class_groups(db_session, classes)

        assert first["created"] == 1
        assert second["created"] == 0
        assert second["updated"] == 0  # nothing changed → no update
        # Exactly one row for that calendar.
        from sqlalchemy import select, func
        count = await db_session.scalar(
            select(func.count()).select_from(ClassGroup).where(
                ClassGroup.google_calendar_id == "c_abc123@group.calendar.google.com"
            )
        )
        assert count == 1

    async def test_matches_by_calendar_id_and_updates_name(self, db_session):
        # Pre-existing local group with the same calendar but a stale name.
        await hydrate_local_class_groups(db_session, [_portal_class(name="Old Name")])
        counts = await hydrate_local_class_groups(db_session, [_portal_class(name="New Name")])

        assert counts["created"] == 0
        assert counts["updated"] == 1
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group.name == "New Name"

    async def test_sets_discord_ids_on_update_when_portal_gains_them(self, db_session):
        await hydrate_local_class_groups(db_session, [_portal_class()])  # no IDs yet
        counts = await hydrate_local_class_groups(
            db_session, [_portal_class(discord_channel_id="900", discord_role_id="901")]
        )
        assert counts["updated"] == 1
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group.discord_text_channel_id == 900
        assert group.discord_role_id == 901

    async def test_does_not_null_out_existing_discord_ids(self, db_session):
        """A transiently-missing portal ID must NOT clobber a working local one —
        that would silently disable enrollment/notifications."""
        await hydrate_local_class_groups(
            db_session, [_portal_class(discord_channel_id="555", discord_role_id="777")]
        )
        # Portal now returns the class WITHOUT discord IDs (e.g. mid-provision).
        counts = await hydrate_local_class_groups(db_session, [_portal_class()])

        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group.discord_text_channel_id == 555
        assert group.discord_role_id == 777
        assert counts["updated"] == 0  # nothing was changed

    async def test_reactivates_inactive_group(self, db_session):
        await hydrate_local_class_groups(db_session, [_portal_class()])
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        group.active = False
        await db_session.flush()

        counts = await hydrate_local_class_groups(db_session, [_portal_class()])
        assert counts["updated"] == 1
        refreshed = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert refreshed.active is True

    async def test_hydrated_group_is_visible_to_local_sync(self, db_session):
        """The whole point: a hydrated group must be picked up by the local-first
        sync path (get_active_class_groups) so lessons/enrollment can populate."""
        await hydrate_local_class_groups(
            db_session, [_portal_class(discord_role_id="777")]
        )
        groups = await get_active_class_groups(db_session)
        assert len(groups) == 1
        assert groups[0].google_calendar_id == "c_abc123@group.calendar.google.com"
        assert groups[0].discord_role_id == 777  # so members-cog role enrollment can run

    async def test_multiple_classes_mixed(self, db_session):
        classes = [
            _portal_class(id=1, google_calendar_id="c_1@group.calendar.google.com"),
            _portal_class(id=2, google_calendar_id="c_2@group.calendar.google.com"),
            _portal_class(id=3, google_calendar_id=None),  # skipped
        ]
        counts = await hydrate_local_class_groups(db_session, classes)
        assert counts == {"created": 2, "updated": 0, "skipped": 1, "deactivated": 0}

    async def test_deactivates_group_dropped_from_portal(self, db_session):
        """A class archived in the portal (drops out of GET /api/bot/classes) is
        deactivated locally so it stops firing reminders."""
        c1 = _portal_class(id=1, google_calendar_id="c_1@group.calendar.google.com")
        c2 = _portal_class(id=2, google_calendar_id="c_2@group.calendar.google.com")
        await hydrate_local_class_groups(db_session, [c1, c2])

        # Next cycle the portal only returns c_1 (c_2 was archived).
        counts = await hydrate_local_class_groups(db_session, [c1])
        assert counts["deactivated"] == 1

        gone = await _get_group_by_cal(db_session, "c_2@group.calendar.google.com")
        assert gone.active is False
        kept = await _get_group_by_cal(db_session, "c_1@group.calendar.google.com")
        assert kept.active is True

    async def test_empty_portal_does_not_deactivate(self, db_session):
        """A transient empty/failed portal response must never mass-deactivate."""
        await hydrate_local_class_groups(db_session, [_portal_class()])
        counts = await hydrate_local_class_groups(db_session, [])

        assert counts["deactivated"] == 0
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group.active is True

    async def test_archived_class_reactivates_when_it_returns(self, db_session):
        c1 = _portal_class(id=1, google_calendar_id="c_1@group.calendar.google.com")
        c2 = _portal_class(id=2, google_calendar_id="c_2@group.calendar.google.com")
        await hydrate_local_class_groups(db_session, [c1, c2])
        await hydrate_local_class_groups(db_session, [c1])  # c_2 deactivated
        counts = await hydrate_local_class_groups(db_session, [c1, c2])  # c_2 back

        assert counts["updated"] >= 1
        revived = await _get_group_by_cal(db_session, "c_2@group.calendar.google.com")
        assert revived.active is True

    async def test_deactivated_group_lessons_excluded_from_reminders(self, db_session):
        """After a class is archived (group deactivated), its already-synced future
        lessons must NOT be returned to the reminder path — otherwise students keep
        getting reminders for a class that no longer exists."""
        from datetime import datetime, timezone, timedelta
        from bot.models.lesson import Lesson, LessonStatus
        from bot.services.lesson_service import get_upcoming_lessons

        await hydrate_local_class_groups(db_session, [_portal_class()])
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        db_session.add(Lesson(
            class_group_id=group.id, title="L", google_event_id="evt_reminder",
            start_time=soon, end_time=soon + timedelta(hours=1),
            status=LessonStatus.scheduled,
        ))
        await db_session.flush()

        # While active, the reminder path sees the lesson.
        while_active = await get_upcoming_lessons(db_session, hours_ahead=25)
        assert any(l.google_event_id == "evt_reminder" for l in while_active)

        # Archive the class (portal now returns a different class) -> deactivated.
        await hydrate_local_class_groups(
            db_session, [_portal_class(google_calendar_id="c_other@group.calendar.google.com")]
        )
        after_archive = await get_upcoming_lessons(db_session, hours_ahead=25)
        assert not any(l.google_event_id == "evt_reminder" for l in after_archive)

    async def test_tolerates_preexisting_duplicate_groups(self, db_session):
        """Two rows sharing a google_calendar_id (e.g. from a prior concurrent
        insert) must not wedge hydration with MultipleResultsFound."""
        cal = "c_dup@group.calendar.google.com"
        db_session.add(ClassGroup(name="Dup A", google_calendar_id=cal, active=True))
        db_session.add(ClassGroup(name="Dup B", google_calendar_id=cal, active=True))
        await db_session.flush()

        # Should not raise; updates the first row rather than blowing up.
        counts = await hydrate_local_class_groups(
            db_session, [_portal_class(name="Canonical", google_calendar_id=cal)]
        )
        assert counts["created"] == 0  # matched an existing row
        # Both duplicate rows still carry the shared calendar → neither deactivated.
        assert counts["deactivated"] == 0


# ---------------------------------------------------------------------------
# hydrate_local_class_groups_from_portal (fetch wrapper)
# ---------------------------------------------------------------------------

class TestHydrateFromPortal:
    async def test_fetches_and_hydrates(self, db_session, monkeypatch):
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", "secret")
        monkeypatch.setattr(config, "PORTAL_API_URL", "https://portal.example")
        classes = [_portal_class(discord_role_id="777")]

        with patch(
            "bot.services.lesson_service._get_portal_client",
            return_value=(lambda: _FakePortalClient(classes), Exception),
        ):
            counts = await hydrate_local_class_groups_from_portal(db_session)

        assert counts["created"] == 1
        group = await _get_group_by_cal(db_session, "c_abc123@group.calendar.google.com")
        assert group is not None

    async def test_skips_when_config_unset(self, db_session, monkeypatch):
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", None)
        monkeypatch.setattr(config, "PORTAL_API_URL", None)
        counts = await hydrate_local_class_groups_from_portal(db_session)
        assert counts == {"created": 0, "updated": 0, "skipped": 0, "deactivated": 0}

    async def test_fails_soft_on_fetch_error(self, db_session, monkeypatch):
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", "secret")
        monkeypatch.setattr(config, "PORTAL_API_URL", "https://portal.example")

        with patch(
            "bot.services.lesson_service._get_portal_client",
            return_value=(lambda: _FakePortalClient([], raise_exc=RuntimeError("boom")), Exception),
        ):
            counts = await hydrate_local_class_groups_from_portal(db_session)

        # Fail-soft: zero counts, no exception propagated.
        assert counts == {"created": 0, "updated": 0, "skipped": 0, "deactivated": 0}
