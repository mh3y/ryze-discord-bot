"""Unit tests for class-discovery calendar filtering (DEF-9).

`_should_skip` decides which Google calendars are auto-provisioned as classes.
The bug: it skipped any calendar whose ID was email-shaped to avoid provisioning
the account's primary calendar — but secondary class calendars also have
email-shaped IDs (…@group.calendar.google.com), so EVERY real class was skipped
and nothing was ever provisioned. The fix uses Google's `primary` flag instead.
"""
from bot.cogs.class_discovery import _should_skip


def _cal(
    id: str = "c_abc123@group.calendar.google.com",
    name: str = "Year 11 Advanced Maths",
    primary: bool = False,
) -> dict:
    return {"id": id, "name": name, "primary": primary}


def test_secondary_class_calendar_not_skipped():
    """DEF-9 regression: a real class calendar (email-shaped @group.calendar
    .google.com ID) must NOT be skipped — the old email regex wrongly matched it."""
    assert _should_skip(_cal()) is False


def test_email_shaped_secondary_without_primary_flag_kept():
    """The exact shape the over-broad email regex matched — kept now."""
    cal = _cal(id="c_9f8e7d@group.calendar.google.com", name="Year 7 English")
    assert _should_skip(cal) is False


def test_primary_calendar_skipped():
    cal = _cal(id="ryzeeducationhq@gmail.com", name="Ryze Education", primary=True)
    assert _should_skip(cal) is True


def test_primary_flag_skips_even_with_class_like_name():
    """A primary calendar is skipped by the flag regardless of its name."""
    cal = _cal(id="ryzeeducationhq@gmail.com", name="Year 12 Physics", primary=True)
    assert _should_skip(cal) is True


def test_missing_primary_key_defaults_to_kept():
    """If the `primary` key is absent (defensive), a normal class is still kept."""
    cal = {"id": "c_abc@group.calendar.google.com", "name": "Year 9 Science"}
    assert _should_skip(cal) is False


def test_underscore_prefixed_calendar_skipped():
    assert _should_skip(_cal(name="_Admin")) is True


def test_ignored_name_skipped():
    assert _should_skip(_cal(name="Birthdays")) is True


def test_empty_name_skipped():
    assert _should_skip(_cal(name="")) is True
