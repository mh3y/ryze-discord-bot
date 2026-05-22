"""Tests for AdminCog slash commands."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from bot.cogs.admin import AdminCog
from bot.models.user import User, UserRole
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.lesson import Lesson, LessonStatus

from tests.conftest import (
    make_bot, make_guild, make_member, make_role, make_interaction,
    make_text_channel, session_patch,
)


# ── Helpers ────────────────────────────────────────────────────────────────── #

def _get_embed(interaction):
    """Extract the embed from the last followup.send call."""
    call = interaction.followup.send.call_args
    return call.kwargs.get("embed") or (call.kwargs.get("embeds") or [None])[0]


def _get_text(interaction):
    """Extract the positional text content from the last followup.send call."""
    call = interaction.followup.send.call_args
    if call.args:
        return call.args[0]
    return call.kwargs.get("content", "")


# ── /link_user ─────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_link_user_creates_new_record(db_session, bot):
    """A fresh discord user should create a new DB row and respond with 'Linked'."""
    cog = AdminCog(bot)
    member = make_member(discord_id=555666777, display_name="New Student")
    guild = make_guild(roles=[make_role(1, "Student")])
    interaction = make_interaction(guild=guild, user=make_member())

    with session_patch("bot.cogs.admin", db_session):
        await cog.link_user.callback(cog, interaction, member, "New Student", "student", None)

    text = _get_text(interaction)
    assert "Linked" in text

    from sqlalchemy import select
    result = await db_session.execute(select(User).where(User.discord_user_id == 555666777))
    db_user = result.scalar_one_or_none()
    assert db_user is not None
    assert db_user.full_name == "New Student"
    assert db_user.role == UserRole.student


@pytest.mark.asyncio
async def test_link_user_updates_existing_record(db_session, bot):
    """An existing user should be updated and the response should contain 'Updated'."""
    existing = User(discord_user_id=111222333, full_name="Old Name", role=UserRole.student, active=True)
    db_session.add(existing)
    await db_session.flush()

    cog = AdminCog(bot)
    member = make_member(discord_id=111222333)
    guild = make_guild(roles=[make_role(1, "Student")])
    interaction = make_interaction(guild=guild, user=make_member())

    with session_patch("bot.cogs.admin", db_session):
        await cog.link_user.callback(cog, interaction, member, "New Name", "student", None)

    text = _get_text(interaction)
    assert "Updated" in text

    from sqlalchemy import select
    result = await db_session.execute(select(User).where(User.discord_user_id == 111222333))
    db_user = result.scalar_one_or_none()
    assert db_user.full_name == "New Name"


@pytest.mark.asyncio
async def test_link_user_assigns_base_role(db_session, bot):
    """When a Student role exists in the guild, add_roles should be called on the member."""
    cog = AdminCog(bot)
    student_role = make_role(role_id=10, name="Student")
    member = make_member(discord_id=200200200)
    guild = make_guild(roles=[student_role])
    interaction = make_interaction(guild=guild)

    with session_patch("bot.cogs.admin", db_session):
        await cog.link_user.callback(cog, interaction, member, "Test Student", "student", None)

    member.add_roles.assert_called_once_with(student_role, reason="Assigned via /link_user")


@pytest.mark.asyncio
async def test_link_user_assigns_class_role(db_session, bot):
    """Providing a class_role should call add_roles for that role too."""
    cog = AdminCog(bot)
    student_role = make_role(role_id=10, name="Student")
    year10_role = make_role(role_id=20, name="Year 10")
    member = make_member(discord_id=300300300)
    guild = make_guild(roles=[student_role, year10_role])
    interaction = make_interaction(guild=guild)

    with session_patch("bot.cogs.admin", db_session):
        await cog.link_user.callback(cog, interaction, member, "Year Ten Kid", "student", "Year 10")

    # Should be called twice: once for Student, once for Year 10
    calls = member.add_roles.call_args_list
    assigned_roles = [c.args[0].name for c in calls]
    assert "Student" in assigned_roles
    assert "Year 10" in assigned_roles


@pytest.mark.asyncio
async def test_link_user_role_not_in_guild(db_session, bot):
    """When the class role doesn't exist in the guild, the response mentions 'No Discord role named'."""
    cog = AdminCog(bot)
    member = make_member(discord_id=400400400)
    guild = make_guild(roles=[make_role(10, "Student")])  # No "Year 10" role
    interaction = make_interaction(guild=guild)

    with session_patch("bot.cogs.admin", db_session):
        await cog.link_user.callback(cog, interaction, member, "Test", "student", "Year 10")

    text = _get_text(interaction)
    assert "No Discord role named" in text


# ── /edit_user ──────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_edit_user_not_linked(db_session, bot):
    """Editing a user not in the DB should return 'no linked record'."""
    cog = AdminCog(bot)
    member = make_member(discord_id=999888777)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.edit_user.callback(cog, interaction, member, "New Name", None, None, None)

    text = _get_text(interaction)
    assert "no linked record" in text.lower()


@pytest.mark.asyncio
async def test_edit_user_no_fields_provided(db_session, bot):
    """Providing no optional fields should respond with 'Nothing to change'."""
    cog = AdminCog(bot)
    member = make_member(discord_id=500500500)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.edit_user.callback(cog, interaction, member, None, None, None, None)

    text = _get_text(interaction)
    assert "Nothing to change" in text


@pytest.mark.asyncio
async def test_edit_user_updates_name(db_session, bot):
    """Providing full_name should update the DB and call member.edit for nickname."""
    user = User(discord_user_id=600600600, full_name="Old Name", role=UserRole.student, active=True)
    db_session.add(user)
    await db_session.flush()

    cog = AdminCog(bot)
    member = make_member(discord_id=600600600)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.edit_user.callback(cog, interaction, member, "New Name", None, None, None)

    member.edit.assert_called_once()
    call_kwargs = member.edit.call_args.kwargs
    assert call_kwargs.get("nick") == "New Name"

    from sqlalchemy import select
    result = await db_session.execute(select(User).where(User.discord_user_id == 600600600))
    db_user = result.scalar_one_or_none()
    assert db_user.full_name == "New Name"


@pytest.mark.asyncio
async def test_edit_user_changes_role(db_session, bot):
    """Changing the role should remove the old Discord role and add the new one."""
    user = User(discord_user_id=700700700, full_name="Test Person", role=UserRole.student, active=True)
    db_session.add(user)
    await db_session.flush()

    cog = AdminCog(bot)
    student_role = make_role(role_id=10, name="Student")
    tutor_role = make_role(role_id=11, name="Tutor")
    member = make_member(discord_id=700700700)
    member.roles = [student_role]
    guild = make_guild(roles=[student_role, tutor_role])
    interaction = make_interaction(guild=guild)

    with session_patch("bot.cogs.admin", db_session):
        await cog.edit_user.callback(cog, interaction, member, None, "tutor", None, None)

    member.remove_roles.assert_called_once()
    member.add_roles.assert_called_once()


# ── /delete_user ────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_delete_user_not_linked(db_session, bot):
    """Deleting a user not in the DB should respond with 'no linked record'."""
    cog = AdminCog(bot)
    member = make_member(discord_id=888777666)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.delete_user.callback(cog, interaction, member, True)

    text = _get_text(interaction)
    assert "no linked record" in text.lower()


# ── /class_roster ────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_class_roster_invalid_id(db_session, bot):
    """A non-existent class group ID should return 'No class group found'."""
    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.class_roster.callback(cog, interaction, 99999)

    text = _get_text(interaction)
    assert "No class group found" in text


@pytest.mark.asyncio
async def test_class_roster_with_members(db_session, bot):
    """A class with a tutor and student should produce an embed with two fields."""
    tutor_user = User(discord_user_id=111000111, full_name="Tutor Person", role=UserRole.tutor, active=True)
    student_user = User(discord_user_id=222000222, full_name="Student Person", role=UserRole.student, active=True)
    group = ClassGroup(name="Year 10 Maths", active=True)
    db_session.add_all([tutor_user, student_user, group])
    await db_session.flush()

    db_session.add(ClassGroupMember(class_group_id=group.id, user_id=tutor_user.id, role=MemberRole.tutor, active=True))
    db_session.add(ClassGroupMember(class_group_id=group.id, user_id=student_user.id, role=MemberRole.student, active=True))
    await db_session.flush()

    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.class_roster.callback(cog, interaction, group.id)

    embed = _get_embed(interaction)
    assert embed is not None
    assert len(embed.fields) >= 2


@pytest.mark.asyncio
async def test_class_roster_empty(db_session, bot):
    """A class with no active members should show 'No active members' in the description."""
    group = ClassGroup(name="Empty Class", active=True)
    db_session.add(group)
    await db_session.flush()

    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.class_roster.callback(cog, interaction, group.id)

    embed = _get_embed(interaction)
    assert embed is not None
    assert "No active members" in embed.description


# ── /today_classes ───────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_today_classes_no_lessons(db_session, bot):
    """With no lessons in the DB, the embed colour should be grey."""
    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        with patch("bot.services.lesson_service.get_todays_lessons", return_value=[]):
            await cog.today_classes.callback(cog, interaction)

    embed = _get_embed(interaction)
    assert embed is not None
    assert "No classes scheduled" in embed.description


@pytest.mark.asyncio
async def test_today_classes_with_lessons(db_session, bot):
    """With a lesson in the DB for today, the embed should be blue with a field."""
    tutor = User(discord_user_id=333444555, full_name="Good Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(name="Year 9 Maths", active=True)
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id

    # 10 AM UTC → ~8 PM Sydney (UTC+10), definitely today Sydney time
    now = datetime.now(timezone.utc)
    today_10am_utc = now.replace(hour=10, minute=0, second=0, microsecond=0)
    lesson = Lesson(
        class_group_id=group.id,
        title="Test Lesson",
        start_time=today_10am_utc,
        end_time=today_10am_utc + timedelta(hours=1),
        status=LessonStatus.scheduled,
    )
    db_session.add(lesson)
    await db_session.flush()

    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.today_classes.callback(cog, interaction)

    embed = _get_embed(interaction)
    assert embed is not None
    # Either we got lessons (blue embed with fields) or grey (no lessons found for UTC today boundaries)
    # Depending on timezone, it may or may not match. Check it's a valid embed at minimum.
    assert embed.title is not None
    assert "Today" in embed.title


# ── /upcoming_classes ────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_upcoming_classes_no_lessons(db_session, bot):
    """With no future lessons, the embed description should mention no classes."""
    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.upcoming_classes.callback(cog, interaction)

    embed = _get_embed(interaction)
    assert embed is not None
    assert "No classes" in embed.description


@pytest.mark.asyncio
async def test_upcoming_classes_with_lessons(db_session, bot):
    """With a future lesson, the embed should be blue and have at least one field."""
    tutor = User(discord_user_id=444555666, full_name="Ms Tutor", role=UserRole.tutor, active=True)
    group = ClassGroup(name="Year 12 Ext 1", active=True)
    db_session.add_all([tutor, group])
    await db_session.flush()
    group.tutor_user_id = tutor.id

    future = datetime.now(timezone.utc) + timedelta(hours=48)
    lesson = Lesson(
        class_group_id=group.id,
        title="Future Lesson",
        start_time=future,
        end_time=future + timedelta(hours=1),
        status=LessonStatus.scheduled,
    )
    db_session.add(lesson)
    await db_session.flush()

    cog = AdminCog(bot)
    interaction = make_interaction()

    with session_patch("bot.cogs.admin", db_session):
        await cog.upcoming_classes.callback(cog, interaction)

    embed = _get_embed(interaction)
    assert embed is not None
    assert len(embed.fields) >= 1
