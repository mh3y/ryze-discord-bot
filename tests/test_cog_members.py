"""Tests for MembersCog — automated sync, on_member_join, on_member_update, /enrol, etc."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

import discord

from bot.cogs.members import MembersCog
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.user import User, UserRole

from tests.conftest import make_bot, make_guild, make_member, make_interaction, session_patch


# ── Helpers ──────────────────────────────────────────────────────────────── #

def _make_discord_role(role_id: int, name: str = "Test Role") -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.name = name
    r.mention = f"<@&{role_id}>"
    return r


async def _seed_user(db_session, discord_id=111, name="Alice"):
    user = User(discord_user_id=discord_id, full_name=name, role=UserRole.student, active=True)
    db_session.add(user)
    await db_session.flush()
    return user


async def _seed_group(db_session, name="Maths Class", role_id: int | None = None):
    group = ClassGroup(
        name=name,
        discord_text_channel_id=999,
        discord_role_id=role_id,
        active=True,
    )
    db_session.add(group)
    await db_session.flush()
    return group


async def _seed_membership(db_session, user, group, active=True):
    m = ClassGroupMember(class_group_id=group.id, user_id=user.id, role=MemberRole.student, active=active)
    db_session.add(m)
    await db_session.flush()
    return m


# ── _sync_all_members (startup / periodic) ────────────────────────────────── #

@pytest.mark.asyncio
async def test_sync_creates_provisional_records(db_session, bot):
    """Sync should create User records for human members not yet in DB."""
    human = make_member(discord_id=1001, display_name="New Human")
    human.bot = False
    human.roles = []
    guild = make_guild(members=[human])

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        created, enrolled = await cog._sync_all_members(guild)

    assert created == 1
    result = await db_session.execute(select(User).where(User.discord_user_id == 1001))
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_sync_skips_bots(db_session, bot):
    """Sync should ignore bot accounts."""
    bot_member = make_member(discord_id=1002)
    bot_member.bot = True
    bot_member.roles = []
    guild = make_guild(members=[bot_member])

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        created, _ = await cog._sync_all_members(guild)

    assert created == 0


@pytest.mark.asyncio
async def test_sync_skips_existing_users(db_session, bot):
    """Sync should not create duplicate records for members already in DB."""
    await _seed_user(db_session, discord_id=1003)
    human = make_member(discord_id=1003)
    human.bot = False
    human.roles = []
    guild = make_guild(members=[human])

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        created, _ = await cog._sync_all_members(guild)

    assert created == 0
    result = await db_session.execute(select(User).where(User.discord_user_id == 1003))
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_sync_auto_enrols_by_discord_role(db_session, bot):
    """Sync should enrol a member who holds a Discord role mapped to a class group."""
    group = await _seed_group(db_session, name="Year 11", role_id=5555)

    discord_role = _make_discord_role(role_id=5555, name="Year 11 Advanced")
    human = make_member(discord_id=1004, display_name="RoleMember")
    human.bot = False
    human.roles = [discord_role]
    guild = make_guild(members=[human])

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        created, enrolled = await cog._sync_all_members(guild)

    assert created == 1
    assert enrolled == 1
    user_result = await db_session.execute(select(User).where(User.discord_user_id == 1004))
    db_user = user_result.scalar_one_or_none()
    mem_result = await db_session.execute(
        select(ClassGroupMember).where(
            ClassGroupMember.user_id == db_user.id,
            ClassGroupMember.class_group_id == group.id,
        )
    )
    assert mem_result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_sync_does_not_duplicate_enrolments(db_session, bot):
    """Sync should not add a second membership if one already exists."""
    user = await _seed_user(db_session, discord_id=1005)
    group = await _seed_group(db_session, role_id=6666)
    await _seed_membership(db_session, user, group, active=True)

    discord_role = _make_discord_role(role_id=6666)
    human = make_member(discord_id=1005)
    human.bot = False
    human.roles = [discord_role]
    guild = make_guild(members=[human])

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        _, enrolled = await cog._sync_all_members(guild)

    assert enrolled == 0
    result = await db_session.execute(
        select(ClassGroupMember).where(ClassGroupMember.user_id == user.id)
    )
    assert len(result.scalars().all()) == 1


# ── on_member_join ────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_on_member_join_creates_record(db_session, bot):
    """A new human joining should get a provisional DB record."""
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    member = make_member(discord_id=2001, display_name="Joiner")
    member.bot = False

    with session_patch("bot.cogs.members", db_session), \
         patch("bot.cogs.members.config.ADMIN_NOTIFICATION_CHANNEL_ID", None):
        await cog.on_member_join(member)

    result = await db_session.execute(select(User).where(User.discord_user_id == 2001))
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_on_member_join_skips_bots(db_session, bot):
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    member = make_member(discord_id=2002)
    member.bot = True

    with session_patch("bot.cogs.members", db_session):
        await cog.on_member_join(member)

    result = await db_session.execute(select(User).where(User.discord_user_id == 2002))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_on_member_join_no_duplicate_on_rejoin(db_session, bot):
    """A member who already has a record should not get a duplicate."""
    await _seed_user(db_session, discord_id=2003)
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    member = make_member(discord_id=2003)
    member.bot = False

    with session_patch("bot.cogs.members", db_session), \
         patch("bot.cogs.members.config.ADMIN_NOTIFICATION_CHANNEL_ID", None):
        await cog.on_member_join(member)

    result = await db_session.execute(select(User).where(User.discord_user_id == 2003))
    assert len(result.scalars().all()) == 1


# ── on_member_update (role-based auto-enrolment) ─────────────────────────── #

@pytest.mark.asyncio
async def test_on_member_update_auto_enrols_on_role_add(db_session, bot):
    """Adding a mapped Discord role should auto-enrol the member in the linked class group."""
    user = await _seed_user(db_session, discord_id=3001)
    group = await _seed_group(db_session, name="Year 12", role_id=7777)

    discord_role = _make_discord_role(role_id=7777, name="Year 12 Advanced")
    before = make_member(discord_id=3001)
    before.bot = False
    before.roles = []

    after = make_member(discord_id=3001)
    after.bot = False
    after.roles = [discord_role]
    after.guild = make_guild()

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session), \
         patch("bot.cogs.members.config.ADMIN_NOTIFICATION_CHANNEL_ID", None):
        await cog.on_member_update(before, after)

    mem_result = await db_session.execute(
        select(ClassGroupMember).where(
            ClassGroupMember.user_id == user.id,
            ClassGroupMember.class_group_id == group.id,
            ClassGroupMember.active.is_(True),
        )
    )
    assert mem_result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_on_member_update_no_action_when_role_not_mapped(db_session, bot):
    """Adding an unmapped Discord role should not create any enrolments."""
    await _seed_user(db_session, discord_id=3002)

    discord_role = _make_discord_role(role_id=8888, name="Unrelated Role")
    before = make_member(discord_id=3002)
    before.bot = False
    before.roles = []
    after = make_member(discord_id=3002)
    after.bot = False
    after.roles = [discord_role]

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        await cog.on_member_update(before, after)

    result = await db_session.execute(select(ClassGroupMember))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_on_member_update_no_action_when_no_roles_added(db_session, bot):
    """If only roles were removed (not added), no enrolment should occur."""
    discord_role = _make_discord_role(role_id=9999)
    before = make_member(discord_id=3003)
    before.bot = False
    before.roles = [discord_role]
    after = make_member(discord_id=3003)
    after.bot = False
    after.roles = []

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        await cog.on_member_update(before, after)

    result = await db_session.execute(select(ClassGroupMember))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_on_member_update_reactivates_inactive_membership(db_session, bot):
    """Re-adding a mapped role to a previously unenrolled member should reactivate membership."""
    user = await _seed_user(db_session, discord_id=3004)
    group = await _seed_group(db_session, role_id=4444)
    membership = await _seed_membership(db_session, user, group, active=False)

    discord_role = _make_discord_role(role_id=4444)
    before = make_member(discord_id=3004)
    before.bot = False
    before.roles = []
    after = make_member(discord_id=3004)
    after.bot = False
    after.roles = [discord_role]
    after.guild = make_guild()

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session), \
         patch("bot.cogs.members.config.ADMIN_NOTIFICATION_CHANNEL_ID", None):
        await cog.on_member_update(before, after)

    await db_session.refresh(membership)
    assert membership.active is True


# ── /enrol ───────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_enrol_creates_user_and_membership(db_session, bot):
    group = await _seed_group(db_session)
    discord_member = make_member(discord_id=4001, display_name="New Student")

    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    interaction = make_interaction(guild=make_guild())

    with session_patch("bot.cogs.members", db_session), \
         patch("bot.cogs.members._set_nickname", new=AsyncMock(return_value="✅")), \
         patch("bot.cogs.members._assign_discord_role", new=AsyncMock(return_value="✅")):
        await cog.enrol.callback(cog, interaction,
                                 discord_user=discord_member, class_group_id=group.id,
                                 full_name="New Student", role="student", class_role=None)

    user_result = await db_session.execute(select(User).where(User.discord_user_id == 4001))
    db_user = user_result.scalar_one_or_none()
    assert db_user is not None

    mem_result = await db_session.execute(
        select(ClassGroupMember).where(ClassGroupMember.user_id == db_user.id)
    )
    assert mem_result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_enrol_invalid_class_group_returns_error(db_session, bot):
    discord_member = make_member(discord_id=4002)
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.members", db_session):
        await cog.enrol.callback(cog, interaction,
                                 discord_user=discord_member, class_group_id=99999,
                                 full_name="Ghost", role="student", class_role=None)

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert "no class group" in text.lower() or "not found" in text.lower()


# ── /unenrol ─────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_unenrol_deactivates_membership(db_session, bot):
    user = await _seed_user(db_session, discord_id=5001)
    group = await _seed_group(db_session)
    membership = await _seed_membership(db_session, user, group)

    discord_member = make_member(discord_id=5001)
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.members", db_session):
        await cog.unenrol.callback(cog, interaction, discord_user=discord_member, class_group_id=group.id)

    await db_session.refresh(membership)
    assert membership.active is False


@pytest.mark.asyncio
async def test_unenrol_unknown_user_returns_error(db_session, bot):
    discord_member = make_member(discord_id=5002)
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.members", db_session):
        await cog.unenrol.callback(cog, interaction, discord_user=discord_member, class_group_id=1)

    sent = interaction.followup.send.call_args
    text = sent.args[0] if sent.args else sent.kwargs.get("content", "")
    assert "no linked record" in text.lower()


# ── /pending_members ──────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_pending_members_lists_unassigned(db_session, bot):
    await _seed_user(db_session, discord_id=6001, name="Unassigned")
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.members", db_session):
        await cog.pending_members.callback(cog, interaction)

    sent = interaction.followup.send.call_args
    embed = sent.kwargs.get("embed") or sent.args[0]
    assert "Unassigned" in embed.description


@pytest.mark.asyncio
async def test_pending_members_empty_when_all_enrolled(db_session, bot):
    user = await _seed_user(db_session, discord_id=6002)
    group = await _seed_group(db_session)
    await _seed_membership(db_session, user, group)

    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    interaction = make_interaction()

    with session_patch("bot.cogs.members", db_session):
        await cog.pending_members.callback(cog, interaction)

    sent = interaction.followup.send.call_args
    embed = sent.kwargs.get("embed") or sent.args[0]
    assert "No Pending" in embed.title


# ── /scan_members ─────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_scan_members_command_creates_records(db_session, bot):
    human = make_member(discord_id=7001, display_name="ScannedMember")
    human.bot = False
    human.roles = []
    guild = make_guild(members=[human])
    interaction = make_interaction(guild=guild)
    interaction.guild = guild

    cog = MembersCog(bot)
    cog._sync_loop.cancel()

    with session_patch("bot.cogs.members", db_session):
        await cog.scan_members.callback(cog, interaction)

    result = await db_session.execute(select(User).where(User.discord_user_id == 7001))
    assert result.scalar_one_or_none() is not None
