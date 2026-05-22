"""Tests for HomeworkCog slash commands and reminder loop."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from bot.cogs.homework import HomeworkCog
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.homework import HomeworkTask, SubmissionStatus
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User, UserRole

from tests.conftest import (
    make_bot, make_guild, make_member, make_text_channel, make_interaction,
    session_patch,
)


# ── Helpers ────────────────────────────────────────────────────────────────── #

def _get_embed(interaction):
    call = interaction.followup.send.call_args
    return call.kwargs.get("embed") or (call.kwargs.get("embeds") or [None])[0]


def _get_text(interaction):
    call = interaction.followup.send.call_args
    if call.args:
        return call.args[0]
    return call.kwargs.get("content", "")


async def _seed_basics(db_session, class_name="Homework Class"):
    tutor = User(discord_user_id=20001, full_name="HW Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(name=class_name, active=True)
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id
    now = datetime.now(timezone.utc)
    lesson = Lesson(
        class_group_id=group.id,
        title="Homework Lesson",
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(hours=1),
        status=LessonStatus.active,
    )
    db_session.add(lesson)
    await db_session.flush()
    return tutor, group, lesson


# ── /set_homework ────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_set_homework_bad_date(db_session, bot):
    """An invalid due_date format should return an error embed containing 'Invalid date'."""
    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction, 1, "My HW", "not-a-date", "")

    text = _get_text(interaction)
    assert "Invalid date format" in text or "❌" in text


@pytest.mark.asyncio
async def test_set_homework_lesson_not_found(db_session, bot):
    """A non-existent lesson_id should return an error embed."""
    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction, 99999, "HW Title", "2030-06-15 17:00", "")

    text = _get_text(interaction)
    assert "not found" in text.lower() or "❌" in text


@pytest.mark.asyncio
async def test_set_homework_creator_not_linked(db_session, bot):
    """If the command runner is not linked to the DB, it should return an error embed."""
    _, group, lesson = await _seed_basics(db_session)

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    user = make_member(discord_id=999888777)  # not in DB
    interaction = make_interaction(user=user)

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction, lesson.id, "HW", "2030-06-15 17:00", "")

    text = _get_text(interaction)
    assert "not linked" in text.lower() or "❌" in text


@pytest.mark.asyncio
async def test_set_homework_creates_task(db_session, bot):
    """Valid inputs should create a HomeworkTask in the DB and return a '✅ Homework Created' embed."""
    tutor, group, lesson = await _seed_basics(db_session)

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    user = make_member(discord_id=tutor.discord_user_id)
    guild = make_guild()
    interaction = make_interaction(guild=guild, user=user)

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction, lesson.id, "Read Ch 5", "2030-06-15 17:00", "pages 100-120")

    embed = _get_embed(interaction)
    assert embed is not None
    assert "Homework Created" in embed.title

    result = await db_session.execute(select(HomeworkTask).where(HomeworkTask.lesson_id == lesson.id))
    task = result.scalar_one_or_none()
    assert task is not None
    assert task.title == "Read Ch 5"


@pytest.mark.asyncio
async def test_set_homework_posts_to_thread(db_session, bot):
    """When the lesson has a discord_thread_id and the thread exists in guild, thread.send should be called."""
    tutor, group, lesson = await _seed_basics(db_session)
    lesson.discord_thread_id = 888
    await db_session.flush()

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()

    thread = MagicMock(spec=__import__("discord").Thread)
    thread.id = 888
    thread.send = AsyncMock()

    user = make_member(discord_id=tutor.discord_user_id)
    guild = make_guild()
    guild.get_channel = MagicMock(return_value=thread)
    interaction = make_interaction(guild=guild, user=user)

    # Make isinstance check pass
    with patch("bot.cogs.homework.discord.Thread", __import__("discord").Thread), \
         session_patch("bot.cogs.homework", db_session):
        # Patch isinstance to treat the mock thread as a discord.Thread
        with patch("discord.Thread", __import__("discord").Thread):
            # The code does isinstance(thread, discord.Thread) - mock it properly
            import discord as _discord
            original_isinstance = __builtins__["isinstance"] if isinstance(__builtins__, dict) else None
            # Simpler: use a real Thread-like object by making the mock pass the spec check
            # The cog checks isinstance(thread, discord.Thread) — mock the get_channel to return
            # something that will pass. We'll patch isinstance in the cog module instead.
            pass

    # Re-approach: patch discord.Thread in the cog namespace so isinstance works
    import discord as _discord
    thread.__class__ = _discord.Thread

    user2 = make_member(discord_id=tutor.discord_user_id)
    guild2 = make_guild()
    guild2.get_channel = MagicMock(return_value=thread)
    interaction2 = make_interaction(guild=guild2, user=user2)

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction2, lesson.id, "Thread HW", "2030-06-15 17:00", "")

    thread.send.assert_called_once()


@pytest.mark.asyncio
async def test_set_homework_posts_to_channel_fallback(db_session, bot):
    """When there's no thread but a text channel exists, channel.send should be called."""
    tutor, group, lesson = await _seed_basics(db_session)
    group.discord_text_channel_id = 999
    lesson.discord_thread_id = None
    await db_session.flush()

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()

    import discord as _discord
    channel = MagicMock(spec=_discord.TextChannel)
    channel.__class__ = _discord.TextChannel
    channel.id = 999
    channel.send = AsyncMock()

    user = make_member(discord_id=tutor.discord_user_id)
    guild = make_guild()
    guild.get_channel = MagicMock(return_value=channel)
    interaction = make_interaction(guild=guild, user=user)

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction, lesson.id, "Channel HW", "2030-06-15 17:00", "")

    channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_set_homework_no_channel_note(db_session, bot):
    """When no thread and no channel exist, the confirm embed should have a 'Note' warning field."""
    tutor, group, lesson = await _seed_basics(db_session)
    group.discord_text_channel_id = None
    lesson.discord_thread_id = None
    await db_session.flush()

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    user = make_member(discord_id=tutor.discord_user_id)
    guild = make_guild()
    interaction = make_interaction(guild=guild, user=user)

    with session_patch("bot.cogs.homework", db_session):
        await cog.set_homework.callback(cog, interaction, lesson.id, "Orphan HW", "2030-06-15 17:00", "")

    embed = _get_embed(interaction)
    assert embed is not None
    field_names = [f.name for f in embed.fields]
    assert any("Note" in name or "⚠️" in name for name in field_names)


# ── /missing_homework ────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_missing_homework_no_missing(db_session, bot):
    """With no overdue tasks, the embed should be green with 'No Missing Homework'."""
    _, group, _ = await _seed_basics(db_session)

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.homework", db_session):
        await cog.missing_homework.callback(cog, interaction, group.id)

    embed = _get_embed(interaction)
    assert embed is not None
    assert "No Missing Homework" in embed.title


@pytest.mark.asyncio
async def test_missing_homework_with_missing(db_session, bot):
    """An overdue task with no submission should appear as a field in a red embed."""
    tutor, group, lesson = await _seed_basics(db_session)

    student = User(discord_user_id=30001, full_name="Missing Student", role=UserRole.student, active=True)
    db_session.add(student)
    await db_session.flush()
    db_session.add(ClassGroupMember(class_group_id=group.id, user_id=student.id, role=MemberRole.student, active=True))

    overdue_task = HomeworkTask(
        class_group_id=group.id,
        lesson_id=lesson.id,
        title="Overdue Assignment",
        due_at=datetime.now(timezone.utc) - timedelta(days=1),
        created_by=tutor.id,
    )
    db_session.add(overdue_task)
    await db_session.flush()

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.homework", db_session):
        await cog.missing_homework.callback(cog, interaction, group.id)

    embed = _get_embed(interaction)
    assert embed is not None
    assert len(embed.fields) >= 1
    field_names = [f.name for f in embed.fields]
    assert any("Overdue Assignment" in name for name in field_names)


# ── Homework reminder loop ────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_hw_reminder_sends_dm(db_session, bot):
    """A task due within 25h with an enrolled student should call safe_send_dm."""
    tutor, group, lesson = await _seed_basics(db_session)
    student = User(discord_user_id=40001, full_name="DM Student", role=UserRole.student, active=True)
    db_session.add(student)
    await db_session.flush()
    db_session.add(ClassGroupMember(class_group_id=group.id, user_id=student.id, role=MemberRole.student, active=True))

    task = HomeworkTask(
        class_group_id=group.id,
        lesson_id=lesson.id,
        title="DM Reminder Task",
        due_at=datetime.now(timezone.utc) + timedelta(hours=20),
        created_by=tutor.id,
    )
    db_session.add(task)
    await db_session.flush()

    cog = HomeworkCog(bot)
    cog._hw_reminder_loop.cancel()

    discord_member = make_member(discord_id=40001)
    guild = make_guild()

    with session_patch("bot.cogs.homework", db_session), \
         patch("bot.cogs.homework.get_discord_member", new=AsyncMock(return_value=discord_member)) as mock_get, \
         patch("bot.cogs.homework.safe_send_dm", new=AsyncMock(return_value=True)) as mock_dm:
        await cog._send_homework_reminders(guild)

    mock_dm.assert_called_once()
    call_args = mock_dm.call_args
    assert call_args[0][0] == discord_member
    assert "DM Reminder Task" in call_args[0][1]
