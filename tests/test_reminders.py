"""Tests for reminder deduplication logic."""
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.models.lesson import LessonStatus
from bot.models.reminder_log import ReminderChannel
from bot.services.reminder_service import (
    has_reminder_been_sent,
    record_reminder,
    build_dm_message,
    build_channel_message,
    _reminder_type,
)


def make_lesson() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        title="Year 11 Advanced",
        start_time=datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc),
        end_time=datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc),
        meet_link=None,
        location=None,
        status=LessonStatus.scheduled,
    )


def make_class_group() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="Year 11 Advanced",
        discord_role_id=None,
        discord_text_channel_id=None,
        tutor=None,
    )


class TestReminderDeduplication:
    @pytest.mark.asyncio
    async def test_no_reminder_sent_initially(self, db_session):
        sent = await has_reminder_been_sent(
            db_session, lesson_id=1, reminder_type="15min",
            channel=ReminderChannel.dm, user_id=None
        )
        assert sent is False

    @pytest.mark.asyncio
    async def test_reminder_recorded_as_sent(self, db_session):
        await record_reminder(
            db_session, lesson_id=1, reminder_type="15min",
            channel=ReminderChannel.dm, success=True, user_id=None
        )
        sent = await has_reminder_been_sent(
            db_session, lesson_id=1, reminder_type="15min",
            channel=ReminderChannel.dm, user_id=None
        )
        assert sent is True

    @pytest.mark.asyncio
    async def test_failed_reminder_not_counted_as_sent(self, db_session):
        await record_reminder(
            db_session, lesson_id=2, reminder_type="1h",
            channel=ReminderChannel.dm, success=False, user_id=10,
            error_message="DM blocked"
        )
        sent = await has_reminder_been_sent(
            db_session, lesson_id=2, reminder_type="1h",
            channel=ReminderChannel.dm, user_id=10
        )
        assert sent is False

    @pytest.mark.asyncio
    async def test_different_reminder_types_are_independent(self, db_session):
        await record_reminder(
            db_session, lesson_id=3, reminder_type="24h",
            channel=ReminderChannel.dm, success=True, user_id=5
        )
        sent_24h = await has_reminder_been_sent(
            db_session, lesson_id=3, reminder_type="24h",
            channel=ReminderChannel.dm, user_id=5
        )
        sent_1h = await has_reminder_been_sent(
            db_session, lesson_id=3, reminder_type="1h",
            channel=ReminderChannel.dm, user_id=5
        )
        assert sent_24h is True
        assert sent_1h is False

    @pytest.mark.asyncio
    async def test_channel_and_dm_are_independent(self, db_session):
        await record_reminder(
            db_session, lesson_id=4, reminder_type="15min",
            channel=ReminderChannel.class_channel, success=True
        )
        sent_channel = await has_reminder_been_sent(
            db_session, lesson_id=4, reminder_type="15min",
            channel=ReminderChannel.class_channel
        )
        sent_dm = await has_reminder_been_sent(
            db_session, lesson_id=4, reminder_type="15min",
            channel=ReminderChannel.dm
        )
        assert sent_channel is True
        assert sent_dm is False

    @pytest.mark.asyncio
    async def test_user_id_scoping(self, db_session):
        await record_reminder(
            db_session, lesson_id=5, reminder_type="15min",
            channel=ReminderChannel.dm, success=True, user_id=100
        )
        sent_user_100 = await has_reminder_been_sent(
            db_session, lesson_id=5, reminder_type="15min",
            channel=ReminderChannel.dm, user_id=100
        )
        sent_user_200 = await has_reminder_been_sent(
            db_session, lesson_id=5, reminder_type="15min",
            channel=ReminderChannel.dm, user_id=200
        )
        assert sent_user_100 is True
        assert sent_user_200 is False


class TestReminderMessages:
    def test_dm_message_contains_class_name(self):
        lesson = make_lesson()
        cg = make_class_group()
        msg = build_dm_message(lesson, cg, 15)
        assert "Year 11 Advanced" in msg
        assert "15 minutes" in msg

    def test_dm_message_1h(self):
        lesson = make_lesson()
        cg = make_class_group()
        msg = build_dm_message(lesson, cg, 60)
        assert "1 hour" in msg

    def test_dm_message_24h(self):
        lesson = make_lesson()
        cg = make_class_group()
        msg = build_dm_message(lesson, cg, 24 * 60)
        assert "24 hours" in msg

    def test_dm_message_includes_meet_link(self):
        lesson = make_lesson()
        lesson.meet_link = "https://meet.google.com/abc-def-ghi"
        cg = make_class_group()
        msg = build_dm_message(lesson, cg, 15)
        assert "meet.google.com" in msg

    def test_channel_message_contains_class_name(self):
        lesson = make_lesson()
        cg = make_class_group()
        msg = build_channel_message(lesson, cg, 15)
        assert "Year 11 Advanced" in msg

    def test_channel_message_uses_user_mentions(self):
        lesson = make_lesson()
        cg = make_class_group()
        mentions = ["<@111>", "<@222>", "<@333>"]
        msg = build_channel_message(lesson, cg, 15, user_mentions=mentions)
        # All three user mentions should appear in the message
        assert "<@111>" in msg
        assert "<@222>" in msg
        assert "<@333>" in msg
        # Class name still appears in the body
        assert "Year 11 Advanced" in msg

    def test_channel_message_falls_back_to_class_name_when_no_mentions(self):
        lesson = make_lesson()
        cg = make_class_group()
        msg = build_channel_message(lesson, cg, 15, user_mentions=None)
        assert "Year 11 Advanced" in msg

    def test_reminder_type_label(self):
        assert _reminder_type(15) == "15min"
        assert _reminder_type(60) == "1h"
        assert _reminder_type(24 * 60) == "24h"
        assert _reminder_type(45) == "45min"
