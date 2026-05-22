"""Tests for LessonThreadsCog slash commands and auto-thread creation."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from bot.cogs.lesson_threads import LessonThreadsCog
from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User, UserRole
from bot import config

from tests.conftest import (
    make_bot, make_guild, make_member, make_text_channel, make_interaction,
    session_patch,
)


# ── Helpers ────────────────────────────────────────────────────────────────── #

def _get_text(interaction):
    call = interaction.followup.send.call_args
    if call.args:
        return call.args[0]
    return call.kwargs.get("content", "")


async def _seed_group_and_lesson(
    db_session,
    class_name="Thread Class",
    text_channel_id=999,
    thread_id=None,
    lesson_offset_hours=1,
):
    tutor = User(discord_user_id=50001, full_name="Thread Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(name=class_name, discord_text_channel_id=text_channel_id, active=True)
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id

    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=lesson_offset_hours)
    lesson = Lesson(
        class_group_id=group.id,
        title=f"Lesson: {class_name}",
        start_time=start,
        end_time=start + timedelta(hours=1),
        status=LessonStatus.scheduled,
        discord_thread_id=thread_id,
    )
    db_session.add(lesson)
    await db_session.flush()
    return group, lesson


# ── /create_lesson_thread ────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_create_thread_cmd_lesson_not_found(db_session, bot):
    """A non-existent lesson_id should return 'not found'."""
    cog = LessonThreadsCog(bot)
    cog._thread_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.lesson_threads", db_session):
        await cog.create_lesson_thread_cmd.callback(cog, interaction, 99999)

    text = _get_text(interaction)
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_create_thread_cmd_already_has_thread(db_session, bot):
    """If the lesson already has a discord_thread_id, the response should say 'already exists'."""
    _, lesson = await _seed_group_and_lesson(db_session, thread_id=888)

    cog = LessonThreadsCog(bot)
    cog._thread_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.lesson_threads", db_session):
        await cog.create_lesson_thread_cmd.callback(cog, interaction, lesson.id)

    text = _get_text(interaction)
    assert "already exists" in text.lower() or "<#888>" in text


@pytest.mark.asyncio
async def test_create_thread_cmd_success(db_session, bot):
    """When no thread exists and a channel is configured, a thread should be created and the lesson updated."""
    group, lesson = await _seed_group_and_lesson(db_session, text_channel_id=999, thread_id=None)

    import discord as _discord
    mock_thread = MagicMock(spec=_discord.Thread)
    mock_thread.id = 777
    mock_thread.mention = "<#777>"
    mock_thread.send = AsyncMock()

    guild = make_guild()
    interaction = make_interaction(guild=guild)

    cog = LessonThreadsCog(bot)
    cog._thread_loop.cancel()

    # Patch create_lesson_thread at the cog module level to avoid lazy-load issues
    # when the service accesses class_group.tutor outside an async greenlet.
    with session_patch("bot.cogs.lesson_threads", db_session), \
         patch("bot.cogs.lesson_threads.create_lesson_thread", new=AsyncMock(return_value=mock_thread)):
        await cog.create_lesson_thread_cmd.callback(cog, interaction, lesson.id)

    text = _get_text(interaction)
    assert "Thread created" in text or "<#777>" in text

    # Verify the lesson was updated in DB
    result = await db_session.execute(select(Lesson).where(Lesson.id == lesson.id))
    updated_lesson = result.scalar_one_or_none()
    assert updated_lesson.discord_thread_id == 777


@pytest.mark.asyncio
async def test_create_thread_cmd_no_channel(db_session, bot):
    """When the class group has no text channel configured, the response should indicate failure."""
    # Set text_channel_id to None
    tutor = User(discord_user_id=60001, full_name="No Channel Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(name="No Channel Class", discord_text_channel_id=None, active=True)
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id

    now = datetime.now(timezone.utc)
    lesson = Lesson(
        class_group_id=group.id,
        title="No Channel Lesson",
        start_time=now + timedelta(hours=1),
        end_time=now + timedelta(hours=2),
        status=LessonStatus.scheduled,
        discord_thread_id=None,
    )
    db_session.add(lesson)
    await db_session.flush()

    cog = LessonThreadsCog(bot)
    cog._thread_loop.cancel()
    guild = make_guild()
    interaction = make_interaction(guild=guild)

    with session_patch("bot.cogs.lesson_threads", db_session):
        await cog.create_lesson_thread_cmd.callback(cog, interaction, lesson.id)

    text = _get_text(interaction)
    assert "Failed" in text or "failed" in text


# ── Auto-thread creation loop ─────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_auto_thread_skips_existing(db_session, bot):
    """A lesson that already has discord_thread_id should not trigger create_lesson_thread."""
    _, lesson = await _seed_group_and_lesson(db_session, thread_id=888)

    cog = LessonThreadsCog(bot)
    cog._thread_loop.cancel()
    guild = make_guild()

    with session_patch("bot.cogs.lesson_threads", db_session), \
         patch("bot.cogs.lesson_threads.create_lesson_thread", new=AsyncMock()) as mock_create:
        await cog._create_upcoming_threads(guild)

    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_auto_thread_creates_at_lead_time(db_session, bot):
    """A lesson starting in exactly THREAD_CREATION_LEAD_MINUTES with no thread should get one created."""
    lead = config.THREAD_CREATION_LEAD_MINUTES
    now = datetime.now(timezone.utc)
    lesson_start = now + timedelta(minutes=lead)

    tutor = User(discord_user_id=70001, full_name="Lead Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(name="Lead Class", discord_text_channel_id=555, active=True)
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id

    lesson = Lesson(
        class_group_id=group.id,
        title="Lead Time Lesson",
        start_time=lesson_start,
        end_time=lesson_start + timedelta(hours=1),
        status=LessonStatus.scheduled,
        discord_thread_id=None,
    )
    db_session.add(lesson)
    await db_session.flush()

    import discord as _discord
    mock_thread = MagicMock(spec=_discord.Thread)
    mock_thread.id = 456
    mock_thread.mention = "<#456>"
    mock_thread.send = AsyncMock()

    cog = LessonThreadsCog(bot)
    cog._thread_loop.cancel()
    guild = make_guild()

    with session_patch("bot.cogs.lesson_threads", db_session), \
         patch("bot.cogs.lesson_threads.now_utc", return_value=now), \
         patch("bot.cogs.lesson_threads.create_lesson_thread", new=AsyncMock(return_value=mock_thread)) as mock_create:
        await cog._create_upcoming_threads(guild)

    mock_create.assert_called_once()

    # Verify lesson was updated with the thread ID
    result = await db_session.execute(select(Lesson).where(Lesson.id == lesson.id))
    updated = result.scalar_one_or_none()
    assert updated.discord_thread_id == 456
