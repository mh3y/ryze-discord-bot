"""Shared pytest fixtures — in-memory SQLite async database for tests."""
import os

# Point config at dummy values before importing anything from bot
os.environ.setdefault("DISCORD_TOKEN", "test_token")
os.environ.setdefault("DISCORD_GUILD_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test_client_id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test_secret")
os.environ.setdefault("GOOGLE_REFRESH_TOKEN", "test_refresh")
os.environ.setdefault("GOOGLE_PROJECT_ID", "test_project")

import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from bot.database import Base
import bot.models  # ensure all models are registered


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    # Disable FK enforcement in SQLite so tests can insert reminder_logs
    # without needing real lesson/user rows (unit tests, not integration tests)
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Discord object factories ────────────────────────────────────────── #
import discord
import pytest
from discord.ext import commands
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


def make_member(discord_id=111222333, display_name="Test User"):
    m = MagicMock(spec=discord.Member)
    m.id = discord_id
    m.display_name = display_name
    m.name = display_name.lower().replace(" ", "")
    m.mention = f"<@{discord_id}>"
    m.roles = []
    m.bot = False
    m.edit = AsyncMock()
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.send = AsyncMock()
    return m


def make_role(role_id=1, name="Student"):
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.name = name
    r.mention = f"<@&{role_id}>"
    return r


def make_text_channel(channel_id=999):
    c = MagicMock(spec=discord.TextChannel)
    c.id = channel_id
    c.send = AsyncMock()
    t = MagicMock(spec=discord.Thread)
    t.id = 888
    t.mention = "<#888>"
    t.send = AsyncMock()
    c.create_thread = AsyncMock(return_value=t)
    return c


def make_voice_channel(channel_id=777):
    c = MagicMock(spec=discord.VoiceChannel)
    c.id = channel_id
    return c


def make_guild(guild_id=123456789, roles=None, channels=None, members=None):
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    g.roles = roles or []
    g.members = members or []
    ch_map = {c.id: c for c in (channels or [])}
    g.get_channel = MagicMock(side_effect=lambda cid: ch_map.get(cid))
    mem_map = {m.id: m for m in (members or [])}
    g.get_member = MagicMock(side_effect=lambda mid: mem_map.get(mid))
    g.fetch_member = AsyncMock(return_value=None)
    g.get_role = MagicMock(side_effect=lambda rid: next((r for r in (roles or []) if r.id == rid), None))
    return g


def make_interaction(guild=None, user=None):
    i = MagicMock(spec=discord.Interaction)
    i.guild = guild or make_guild()
    i.user = user or make_member()
    i.response = AsyncMock()
    i.response.is_done = MagicMock(return_value=True)
    i.followup = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def make_bot():
    b = MagicMock(spec=commands.Bot)
    b.wait_until_ready = AsyncMock()
    b.get_guild = MagicMock(return_value=None)
    return b


def session_patch(module: str, session):
    """Patch get_session in a cog module to yield the given SQLite session."""
    @asynccontextmanager
    async def _cm():
        try:
            yield session
            await session.flush()
        except Exception:
            await session.rollback()
            raise
    return patch(f"{module}.get_session", new=_cm)


@pytest.fixture
def bot():
    return make_bot()
