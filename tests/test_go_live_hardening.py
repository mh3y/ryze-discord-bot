"""Go-live hardening tests (Increment 1). [H13]

Covers the deploy-blocker safeguarding + reliability fixes:
- DEF-15: permission-sync grant validation (channel + grantee)
- DEF-16: role classification by configured IDs, fail-closed, never-demote
- H10/M20: a crashed tasks.loop restarts instead of dying permanently
- M8: heartbeat suppressed when the gateway is down or sync is stale
- H2/H5: /delete_user soft-deactivates and can never lie about success
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import tasks

from bot import config, health
from bot.models.attendance import AttendanceRecord, AttendanceStatus
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.user import User, UserRole
from bot.utils import task_safety
from tests.conftest import (
    make_bot,
    make_guild,
    make_member,
    make_role,
    make_text_channel,
    session_patch,
)

_CHANNEL_ID = 999_000_555


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

async def _make_class_with_channel(session, channel_id: int = _CHANNEL_ID) -> ClassGroup:
    group = ClassGroup(name="Year 11 Advanced", discord_text_channel_id=channel_id, active=True)
    session.add(group)
    await session.flush()
    return group


async def _make_user(session, discord_id: int, role: UserRole = UserRole.student, active: bool = True) -> User:
    u = User(discord_user_id=discord_id, full_name=f"U{discord_id}", role=role, active=active)
    session.add(u)
    await session.flush()
    return u


async def _enrol(session, group: ClassGroup, user: User, active: bool = True) -> ClassGroupMember:
    m = ClassGroupMember(class_group_id=group.id, user_id=user.id, role=MemberRole.student, active=active)
    session.add(m)
    await session.flush()
    return m


# ---------------------------------------------------------------------------
# DEF-15 — permission-sync grant validation
# ---------------------------------------------------------------------------

def _make_jobs_cog(guild):
    from bot.cogs.bot_jobs import BotJobsCog
    bot = make_bot()
    bot.get_guild = MagicMock(return_value=guild)
    cog = BotJobsCog(bot)
    cog._poll_loop.cancel()  # tests drive the method directly
    return cog


def _perm_channel(channel_id: int = _CHANNEL_ID):
    ch = make_text_channel(channel_id)
    ch.set_permissions = AsyncMock()
    return ch


class TestPermissionSyncValidation:
    async def test_unknown_channel_rejected(self, db_session):
        """A channel with no matching class group must never have permissions
        applied — the job fails loudly instead. [DEF-15]"""
        channel = _perm_channel()
        member = make_member(1001)
        guild = make_guild(channels=[channel], members=[member])
        cog = _make_jobs_cog(guild)

        with session_patch("bot.cogs.bot_jobs", db_session):
            with pytest.raises(RuntimeError, match="not a known active"):
                await cog._trigger_discord_permission_sync(
                    {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["1001"]}
                )
        channel.set_permissions.assert_not_called()

    async def test_enrolled_member_grant_applies(self, db_session):
        channel = _perm_channel()
        member = make_member(1002)
        guild = make_guild(channels=[channel], members=[member])
        cog = _make_jobs_cog(guild)

        group = await _make_class_with_channel(db_session)
        user = await _make_user(db_session, 1002)
        await _enrol(db_session, group, user)

        with session_patch("bot.cogs.bot_jobs", db_session):
            await cog._trigger_discord_permission_sync(
                {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["1002"]}
            )
        channel.set_permissions.assert_awaited_once()

    async def test_unenrolled_grant_refused(self, db_session):
        """The core DEF-15 case: a snowflake not enrolled in THIS class is never
        granted access. After a member-sync retry it is still unenrolled, so the
        job FAILS (raises) rather than reporting false success — the CRM re-queues
        it instead of showing the student as done. [review: false-success on refusal]"""
        channel = _perm_channel()
        outsider = make_member(2002)
        guild = make_guild(channels=[channel], members=[outsider])
        cog = _make_jobs_cog(guild)
        cog._trigger_member_sync = AsyncMock()  # retry is a clean no-op here

        group = await _make_class_with_channel(db_session)
        enrolled = await _make_user(db_session, 1003)
        await _enrol(db_session, group, enrolled)
        # outsider exists as a user but is NOT enrolled in this class
        await _make_user(db_session, 2002)

        with session_patch("bot.cogs.bot_jobs", db_session):
            with pytest.raises(RuntimeError, match="could not be authorised"):
                await cog._trigger_discord_permission_sync(
                    {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["2002"]}
                )
        channel.set_permissions.assert_not_called()
        cog._trigger_member_sync.assert_awaited_once()  # it tried a sync before failing

    async def test_inactive_membership_refused(self, db_session):
        channel = _perm_channel()
        member = make_member(1004)
        guild = make_guild(channels=[channel], members=[member])
        cog = _make_jobs_cog(guild)
        cog._trigger_member_sync = AsyncMock()

        group = await _make_class_with_channel(db_session)
        user = await _make_user(db_session, 1004)
        await _enrol(db_session, group, user, active=False)  # unenrolled

        with session_patch("bot.cogs.bot_jobs", db_session):
            with pytest.raises(RuntimeError, match="could not be authorised"):
                await cog._trigger_discord_permission_sync(
                    {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["1004"]}
                )
        channel.set_permissions.assert_not_called()

    async def test_assigned_tutor_grant_applies_without_membership(self, db_session):
        channel = _perm_channel()
        tutor_member = make_member(3001)
        guild = make_guild(channels=[channel], members=[tutor_member])
        cog = _make_jobs_cog(guild)

        tutor = await _make_user(db_session, 3001, role=UserRole.tutor)
        group = await _make_class_with_channel(db_session)
        group.tutor_user_id = tutor.id
        await db_session.flush()

        with session_patch("bot.cogs.bot_jobs", db_session):
            await cog._trigger_discord_permission_sync(
                {"channel_id": str(_CHANNEL_ID), "tutor_discord_id": "3001"}
            )
        channel.set_permissions.assert_awaited_once()

    async def test_revoke_allowed_for_unenrolled_and_admin_skip_preserved(self, db_session):
        """Revokes (deny access) legitimately target just-unenrolled people, so
        they are channel-validated but not membership-validated — and the
        admin/owner protection still holds."""
        channel = _perm_channel()
        normal = make_member(4001)
        normal.guild_permissions = MagicMock(administrator=False)
        admin = make_member(4002)
        admin.guild_permissions = MagicMock(administrator=True)
        guild = make_guild(channels=[channel], members=[normal, admin])
        guild.owner = None
        cog = _make_jobs_cog(guild)

        await _make_class_with_channel(db_session)  # channel IS a class channel

        with session_patch("bot.cogs.bot_jobs", db_session):
            await cog._trigger_discord_permission_sync(
                {"channel_id": str(_CHANNEL_ID), "revoke_discord_ids": ["4001", "4002"]}
            )
        # normal member revoked, admin skipped
        channel.set_permissions.assert_awaited_once()

    async def test_malformed_job_does_not_kill_processing(self, db_session):
        """A job with no 'id' fails in isolation; later jobs still run. [DEF-18]"""
        from bot.cogs import bot_jobs as bj

        cog = _make_jobs_cog(make_guild())
        executed: list[str] = []

        async def fake_execute(job_type, job):
            executed.append(job_type)

        cog._execute = fake_execute  # type: ignore[method-assign]

        called = {"complete": 0}

        async def fake_complete(job_id):
            called["complete"] += 1

        bad_and_good = [{"job_type": "sync_members"}, {"id": 7, "job_type": "sync_classes"}]
        orig_claim, orig_complete = bj._claim_pending_jobs, bj._complete_job
        bj._claim_pending_jobs = AsyncMock(return_value=bad_and_good)
        bj._complete_job = fake_complete
        try:
            await cog._poll_loop.coro(cog)
        finally:
            bj._claim_pending_jobs = orig_claim
            bj._complete_job = orig_complete

        assert executed == ["sync_classes"]  # bad job skipped, good job ran
        assert called["complete"] == 1


# ---------------------------------------------------------------------------
# DEF-16 — role classification by configured IDs, fail-closed
# ---------------------------------------------------------------------------

def _member_with_role_ids(*role_ids: int, discord_id: int = 5001, name: str = "M"):
    m = make_member(discord_id, name)
    m.roles = [make_role(rid, f"r{rid}") for rid in role_ids]
    m.display_avatar = None
    return m


class TestRoleClassification:
    def test_unconfigured_is_fail_closed_even_for_admin_named_role(self, monkeypatch):
        """A role literally NAMED 'Admin' grants nothing — names are forgeable."""
        from bot.cogs.members import MembersCog
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset())
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset())
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())

        m = make_member(5000)
        m.roles = [make_role(42, "Admin"), make_role(43, "Tutor")]
        assert MembersCog._detect_portal_role(m) == UserRole.student
        assert config.role_classification_configured() is False

    def test_configured_ids_classify(self, monkeypatch):
        from bot.cogs.members import MembersCog
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset({200}))
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset({300}))

        assert MembersCog._detect_portal_role(_member_with_role_ids(100)) == UserRole.admin
        assert MembersCog._detect_portal_role(_member_with_role_ids(200)) == UserRole.tutor
        assert MembersCog._detect_portal_role(_member_with_role_ids(300)) == UserRole.parent
        assert MembersCog._detect_portal_role(_member_with_role_ids(999)) == UserRole.student

    def test_admin_NAME_ignored_when_ids_configured(self, monkeypatch):
        from bot.cogs.members import MembersCog
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset())
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())

        m = make_member(5002)
        m.roles = [make_role(42, "Admin")]  # right name, wrong (unconfigured) ID
        assert MembersCog._detect_portal_role(m) == UserRole.student

    def test_config_parser_rejects_garbage(self, monkeypatch):
        monkeypatch.setenv("STAFF_ROLE_IDS", "123, not-a-number")
        with pytest.raises(RuntimeError, match="non-numeric"):
            config._parse_role_id_set("STAFF_ROLE_IDS")

    def test_config_parser_parses_lists(self, monkeypatch):
        monkeypatch.setenv("STAFF_ROLE_IDS", " 111 , 222 ;333, ")
        assert config._parse_role_id_set("STAFF_ROLE_IDS") == frozenset({111, 222, 333})


class _FakePortalClient:
    """Captures sync_members payloads."""
    pushed: list[list[dict]] = []

    def __init__(self) -> None: ...
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False

    async def sync_members(self, members):
        _FakePortalClient.pushed.append(members)
        return {"synced": len(members), "created": 0, "updated": 0}

    async def push_sync_log(self, **kwargs): return {}


def _make_members_cog(guild):
    from bot.cogs.members import MembersCog
    bot = make_bot()
    bot.get_guild = MagicMock(return_value=guild)
    cog = MembersCog(bot)
    cog._sync_loop.cancel()
    return cog


class TestSyncAllMembersFailClosed:
    @pytest.fixture(autouse=True)
    def _portal_env(self, monkeypatch):
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", "k")
        monkeypatch.setattr(config, "PORTAL_API_URL", "https://portal.example")
        monkeypatch.setattr("bot.cogs.members.PortalAPIClient", _FakePortalClient)
        _FakePortalClient.pushed = []

    async def test_unconfigured_pushes_nothing_but_creates_local(self, db_session, monkeypatch):
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset())
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset())
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())

        guild = make_guild(members=[_member_with_role_ids(42, discord_id=6001)])
        cog = _make_members_cog(guild)
        with session_patch("bot.cogs.members", db_session):
            created, _ = await cog._sync_all_members(guild)

        assert created == 1                       # local record still made
        assert _FakePortalClient.pushed == []     # but NOTHING went to the CRM

    async def test_configured_pushes_classified_roles_and_excludes_parents(self, db_session, monkeypatch):
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset({200}))
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset({300}))

        staff = _member_with_role_ids(100, discord_id=6002, name="Staff")
        parent = _member_with_role_ids(300, discord_id=6003, name="Parent")
        student = _member_with_role_ids(discord_id=6004, name="Student")
        guild = make_guild(members=[staff, parent, student])
        cog = _make_members_cog(guild)
        with session_patch("bot.cogs.members", db_session):
            await cog._sync_all_members(guild)

        assert len(_FakePortalClient.pushed) == 1
        payload = _FakePortalClient.pushed[0]
        by_id = {p["discord_user_id"]: p["role"] for p in payload}
        assert by_id == {"6002": "admin", "6004": "student"}  # parent absent

    async def test_never_demotes_local_admin(self, db_session, monkeypatch):
        """An admin whose staff role vanished is HELD, not demoted via push."""
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset())
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())

        await _make_user(db_session, 6005, role=UserRole.admin)
        demoted_in_discord = _member_with_role_ids(discord_id=6005, name="ExStaff")  # no staff role
        guild = make_guild(members=[demoted_in_discord])
        cog = _make_members_cog(guild)
        with session_patch("bot.cogs.members", db_session):
            await cog._sync_all_members(guild)

        assert _FakePortalClient.pushed and all(
            p["discord_user_id"] != "6005" for p in _FakePortalClient.pushed[0]
        ) or _FakePortalClient.pushed == []  # omitted entirely
        from sqlalchemy import select
        u = (await db_session.execute(select(User).where(User.discord_user_id == 6005))).scalar_one()
        assert u.role == UserRole.admin  # local role held

    async def test_local_role_updates_when_configured(self, db_session, monkeypatch):
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset({200}))
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())

        await _make_user(db_session, 6006, role=UserRole.student)
        promoted = _member_with_role_ids(200, discord_id=6006, name="NowTutor")
        guild = make_guild(members=[promoted])
        cog = _make_members_cog(guild)
        with session_patch("bot.cogs.members", db_session):
            await cog._sync_all_members(guild)

        from sqlalchemy import select
        u = (await db_session.execute(select(User).where(User.discord_user_id == 6006))).scalar_one()
        assert u.role == UserRole.tutor  # dead no-op replaced with a real update
        assert _FakePortalClient.pushed[0][0]["role"] == "tutor"


# ---------------------------------------------------------------------------
# H10/M20 — crashed loops restart
# ---------------------------------------------------------------------------

class TestTaskSafety:
    async def test_crashed_loop_restarts(self, monkeypatch):
        monkeypatch.setattr(task_safety, "RESTART_DELAY_SECONDS", 0.05)
        runs: list[int] = []

        @tasks.loop(seconds=0.05)
        async def flaky():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("boom")

        task_safety.install_loop_restart(flaky, "test.flaky")
        flaky.start()
        try:
            await asyncio.sleep(0.5)
        finally:
            flaky.cancel()

        # Crashed on run 1, restarted by the handler, ran again.
        assert len(runs) >= 2


# ---------------------------------------------------------------------------
# M8 — heartbeat suppressed when not ready / stale
# ---------------------------------------------------------------------------

def _make_heartbeat_cog(monkeypatch, ready: bool = True):
    from bot.cogs.heartbeat import HeartbeatCog
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/ping")
    bot = make_bot()
    bot.is_ready = MagicMock(return_value=ready)
    cog = HeartbeatCog(bot)
    cog._heartbeat_loop.cancel()
    return cog


class TestHeartbeatGating:
    @pytest.fixture(autouse=True)
    def _fresh_health(self):
        health.reset_for_tests()
        yield
        health.reset_for_tests()

    async def test_suppressed_when_gateway_not_ready(self, monkeypatch):
        cog = _make_heartbeat_cog(monkeypatch, ready=False)
        assert cog._should_ping() is False

    async def test_pings_when_ready_and_fresh(self, monkeypatch):
        cog = _make_heartbeat_cog(monkeypatch, ready=True)
        # process just started → within the grace window
        assert cog._should_ping() is True

    async def test_suppressed_when_sync_stale(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        cog = _make_heartbeat_cog(monkeypatch, ready=True)
        stale = datetime.now(timezone.utc) - timedelta(hours=10)
        monkeypatch.setattr(health, "_process_started_at", stale)
        monkeypatch.setattr(health, "_last_sync_ok_at", None)
        assert cog._should_ping() is False

    async def test_record_sync_ok_restores_freshness(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        cog = _make_heartbeat_cog(monkeypatch, ready=True)
        monkeypatch.setattr(health, "_process_started_at", datetime.now(timezone.utc) - timedelta(hours=10))
        assert cog._should_ping() is False
        health.record_sync_ok()
        assert cog._should_ping() is True

    async def test_loop_tick_respects_gate(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        cog = _make_heartbeat_cog(monkeypatch, ready=True)
        cog._send_heartbeat = AsyncMock()
        monkeypatch.setattr(health, "_process_started_at", datetime.now(timezone.utc) - timedelta(hours=10))
        await cog._heartbeat_loop.coro(cog)
        cog._send_heartbeat.assert_not_awaited()
        health.record_sync_ok()
        await cog._heartbeat_loop.coro(cog)
        cog._send_heartbeat.assert_awaited_once()


# ---------------------------------------------------------------------------
# H2/H5 — /delete_user soft-deactivates truthfully
# ---------------------------------------------------------------------------

class TestSoftDeactivateUser:
    async def test_soft_deactivates_user_with_history(self, db_session):
        """A student WITH attendance history — the exact case the old hard
        delete blew up on (FK, after showing a success embed)."""
        from bot.cogs.admin import soft_deactivate_user

        group = await _make_class_with_channel(db_session)
        user = await _make_user(db_session, 7001)
        await _enrol(db_session, group, user)
        db_session.add(AttendanceRecord(
            lesson_id=12345, user_id=user.id,
            status=AttendanceStatus.present, total_minutes=60,
        ))
        await db_session.flush()

        with session_patch("bot.cogs.admin", db_session):
            deactivated = await soft_deactivate_user(7001)

        assert deactivated == 1
        from sqlalchemy import select
        u = (await db_session.execute(select(User).where(User.discord_user_id == 7001))).scalar_one()
        assert u.active is False
        m = (await db_session.execute(select(ClassGroupMember).where(ClassGroupMember.user_id == u.id))).scalar_one()
        assert m.active is False
        # history retained
        a = (await db_session.execute(select(AttendanceRecord).where(AttendanceRecord.user_id == u.id))).scalar_one()
        assert a.total_minutes == 60

    async def test_unknown_user_returns_none(self, db_session):
        from bot.cogs.admin import soft_deactivate_user
        with session_patch("bot.cogs.admin", db_session):
            assert await soft_deactivate_user(999_999) is None


# ===========================================================================
# Adversarial-review fixes (2026-07-25) — regressions for the confirmed findings
# ===========================================================================

class TestReviewFixesPermissionSync:
    """DEF-15 hardening surfaced by the Increment-1 adversarial review."""

    async def test_grant_applies_after_member_sync_retry(self, db_session):
        """A grant refused on the first pass but enrolled by the member-sync
        retry is granted — not silently lost, not failed. [review: false-success]"""
        channel = _perm_channel()
        member = make_member(2100)
        guild = make_guild(channels=[channel], members=[member])
        cog = _make_jobs_cog(guild)

        group = await _make_class_with_channel(db_session)
        user = await _make_user(db_session, 2100)  # exists but NOT enrolled yet

        calls = {"n": 0}

        async def _retry():
            calls["n"] += 1
            await _enrol(db_session, group, user)  # the "sync" enrols them

        cog._trigger_member_sync = _retry

        with session_patch("bot.cogs.bot_jobs", db_session):
            await cog._trigger_discord_permission_sync(
                {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["2100"]}
            )
        assert calls["n"] == 1
        channel.set_permissions.assert_awaited_once()

    async def test_deactivated_tutor_not_granted(self, db_session):
        """A soft-deactivated tutor must not stay grantable. [review: tutor path skips User.active]"""
        channel = _perm_channel()
        tutor_member = make_member(3100)
        guild = make_guild(channels=[channel], members=[tutor_member])
        cog = _make_jobs_cog(guild)
        cog._trigger_member_sync = AsyncMock()

        tutor = await _make_user(db_session, 3100, role=UserRole.tutor, active=False)
        group = await _make_class_with_channel(db_session)
        group.tutor_user_id = tutor.id
        await db_session.flush()

        with session_patch("bot.cogs.bot_jobs", db_session):
            with pytest.raises(RuntimeError, match="could not be authorised"):
                await cog._trigger_discord_permission_sync(
                    {"channel_id": str(_CHANNEL_ID), "tutor_discord_id": "3100"}
                )
        channel.set_permissions.assert_not_called()

    async def test_category_channel_refused(self, db_session):
        """A class record pointing at a CATEGORY channel is refused before any
        overwrite is applied — no cascade to child channels. [review: channel type]"""
        import discord
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = _CHANNEL_ID
        category.set_permissions = AsyncMock()
        guild = make_guild(channels=[category], members=[make_member(2300)])
        cog = _make_jobs_cog(guild)

        await _make_class_with_channel(db_session)  # channel id matches the category

        with session_patch("bot.cogs.bot_jobs", db_session):
            with pytest.raises(RuntimeError, match="not a text channel"):
                await cog._trigger_discord_permission_sync(
                    {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["2300"]}
                )
        category.set_permissions.assert_not_called()

    async def test_duplicate_groups_share_channel_union(self, db_session):
        """Two active classes on one channel: a student of EITHER is authorised
        (union, not arbitrary .first()). [review: duplicate class groups .first()]"""
        channel = _perm_channel()
        member = make_member(2200)
        guild = make_guild(channels=[channel], members=[member])
        cog = _make_jobs_cog(guild)

        await _make_class_with_channel(db_session)  # group 1 on the channel, no members
        g2 = ClassGroup(name="Other Y11", discord_text_channel_id=_CHANNEL_ID, active=True)
        db_session.add(g2)
        await db_session.flush()
        user = await _make_user(db_session, 2200)
        await _enrol(db_session, g2, user)  # enrolled only in group 2

        with session_patch("bot.cogs.bot_jobs", db_session):
            await cog._trigger_discord_permission_sync(
                {"channel_id": str(_CHANNEL_ID), "grant_discord_ids": ["2200"]}
            )
        channel.set_permissions.assert_awaited_once()

    async def test_snowflake_type_normalisation_protects_grantee(self, db_session):
        """A grantee listed as int and revoked as str (same id) is still
        protected — snowflakes normalise to int. [review: revoke str/int mismatch]"""
        channel = _perm_channel()
        member = make_member(5300)
        guild = make_guild(channels=[channel], members=[member])
        cog = _make_jobs_cog(guild)

        group = await _make_class_with_channel(db_session)
        user = await _make_user(db_session, 5300)
        await _enrol(db_session, group, user)

        with session_patch("bot.cogs.bot_jobs", db_session):
            await cog._trigger_discord_permission_sync({
                "channel_id": str(_CHANNEL_ID),
                "grant_discord_ids": [5300],     # int
                "revoke_discord_ids": ["5300"],  # str, same id — must NOT revoke
            })
        # Granted once; the revoke is recognised as the same id and skipped.
        channel.set_permissions.assert_awaited_once()


class TestReviewFixesMemberSync:
    """DEF-16 / soft-delete interactions surfaced by the review."""

    @pytest.fixture(autouse=True)
    def _portal_env(self, monkeypatch):
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", "k")
        monkeypatch.setattr(config, "PORTAL_API_URL", "https://portal.example")
        monkeypatch.setattr("bot.cogs.members.PortalAPIClient", _FakePortalClient)
        _FakePortalClient.pushed = []

    async def test_deactivated_user_not_reenrolled(self, db_session, monkeypatch):
        """A soft-deactivated user still holding a mapped class role is NOT
        re-enrolled by the sync. [review: soft-deactivated user auto re-enrolled]"""
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset({200}))
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset({300}))

        group = ClassGroup(name="Y11", discord_text_channel_id=555, discord_role_id=900, active=True)
        db_session.add(group)
        await db_session.flush()
        user = await _make_user(db_session, 6100, active=False)  # deactivated
        member = _member_with_role_ids(900, discord_id=6100)     # still holds the class role
        guild = make_guild(members=[member])
        cog = _make_members_cog(guild)

        with session_patch("bot.cogs.members", db_session):
            _, enrolled = await cog._sync_all_members(guild)

        assert enrolled == 0
        from sqlalchemy import select
        rows = (await db_session.execute(
            select(ClassGroupMember).where(ClassGroupMember.user_id == user.id)
        )).scalars().all()
        assert rows == []  # no membership created

    async def test_deactivated_user_not_pushed_to_crm(self, db_session, monkeypatch):
        """A soft-deactivated user is not re-pushed to the CRM. [review: deactivated still pushed]"""
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset({200}))
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset({300}))

        await _make_user(db_session, 6110, active=False)
        guild = make_guild(members=[_member_with_role_ids(discord_id=6110)])
        cog = _make_members_cog(guild)

        with session_patch("bot.cogs.members", db_session):
            await cog._sync_all_members(guild)

        pushed_ids = [p["discord_user_id"] for batch in _FakePortalClient.pushed for p in batch]
        assert "6110" not in pushed_ids

    async def test_partial_config_holds_local_tutor(self, db_session, monkeypatch):
        """STAFF set, TUTOR unset: a known local tutor is HELD, not demoted to
        student or pushed as one. [review: partial config demotes tutors]"""
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset())   # unconfigured tier
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())

        await _make_user(db_session, 6120, role=UserRole.tutor)
        guild = make_guild(members=[_member_with_role_ids(discord_id=6120)])  # no configured role
        cog = _make_members_cog(guild)

        with session_patch("bot.cogs.members", db_session):
            await cog._sync_all_members(guild)

        from sqlalchemy import select
        u = (await db_session.execute(select(User).where(User.discord_user_id == 6120))).scalar_one()
        assert u.role == UserRole.tutor  # held, not demoted
        pushed_ids = [p["discord_user_id"] for batch in _FakePortalClient.pushed for p in batch]
        assert "6120" not in pushed_ids  # not pushed as student

    async def test_partial_config_holds_local_parent(self, db_session, monkeypatch):
        """TUTOR set, PARENT unset: a known local parent is HELD, never re-filed
        or pushed as a student. [review: partial config mis-files parents]"""
        monkeypatch.setattr(config, "STAFF_ROLE_IDS", frozenset({100}))
        monkeypatch.setattr(config, "TUTOR_ROLE_IDS", frozenset({200}))
        monkeypatch.setattr(config, "PARENT_ROLE_IDS", frozenset())   # unconfigured tier

        await _make_user(db_session, 6130, role=UserRole.parent)
        guild = make_guild(members=[_member_with_role_ids(discord_id=6130)])
        cog = _make_members_cog(guild)

        with session_patch("bot.cogs.members", db_session):
            await cog._sync_all_members(guild)

        from sqlalchemy import select
        u = (await db_session.execute(select(User).where(User.discord_user_id == 6130))).scalar_one()
        assert u.role == UserRole.parent  # held
        pushed_ids = [p["discord_user_id"] for batch in _FakePortalClient.pushed for p in batch]
        assert "6130" not in pushed_ids


class TestReviewFixesHeartbeatFreshness:
    """M8 heartbeat must not be suppressed forever by ONE stale calendar."""

    @pytest.fixture(autouse=True)
    def _fresh_health(self):
        health.reset_for_tests()
        yield
        health.reset_for_tests()

    async def test_partial_calendar_errors_still_records_fresh(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        from bot.cogs import calendar_sync as cs

        # Make the process look old so freshness can only come from a sync cycle.
        monkeypatch.setattr(health, "_process_started_at", datetime.now(timezone.utc) - timedelta(hours=10))
        monkeypatch.setattr(health, "_last_sync_ok_at", None)

        async def fake_sync():
            # 1 of 3 calendars failed — the bot IS working; freshness must record.
            return {"created": 1, "updated": 0, "cancelled": 0, "skipped": 0,
                    "errors": 1, "groups_total": 3, "newly_cancelled": []}

        monkeypatch.setattr(cs, "sync_all_calendars", fake_sync)
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", "")  # skip portal push
        monkeypatch.setattr(config, "PORTAL_API_URL", "")

        cog = cs.CalendarSyncCog(make_bot())
        cog._sync_loop.cancel()
        cog._handle_cancellations = AsyncMock()

        await cog._sync_loop.coro(cog)

        # A clean sync timestamp was recorded, so the bot reads as fresh even
        # though the process "started" 10h ago — one stale calendar didn't
        # suppress the heartbeat.
        assert health._last_sync_ok_at is not None
        assert health.is_fresh(30) is True

    async def test_total_calendar_failure_does_not_record_fresh(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        from bot.cogs import calendar_sync as cs

        monkeypatch.setattr(health, "_process_started_at", datetime.now(timezone.utc) - timedelta(hours=10))
        monkeypatch.setattr(health, "_last_sync_ok_at", None)

        async def fake_sync():
            # EVERY calendar failed — a real systemic outage; heartbeat must fire.
            return {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0,
                    "errors": 3, "groups_total": 3, "newly_cancelled": []}

        monkeypatch.setattr(cs, "sync_all_calendars", fake_sync)
        monkeypatch.setattr(config, "DASHBOARD_API_KEY", "")
        monkeypatch.setattr(config, "PORTAL_API_URL", "")

        cog = cs.CalendarSyncCog(make_bot())
        cog._sync_loop.cancel()
        cog._handle_cancellations = AsyncMock()

        await cog._sync_loop.coro(cog)

        # No clean-sync timestamp recorded, so with a 10h-old process start the
        # bot reads as stale and the dead-man's-switch is allowed to fire.
        assert health._last_sync_ok_at is None
        assert health.is_fresh(30) is False
