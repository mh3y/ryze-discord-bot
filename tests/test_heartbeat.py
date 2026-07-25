"""Tests for the heartbeat (dead-man's-switch) cog."""
from unittest.mock import AsyncMock, MagicMock, patch

from bot import config
from bot.cogs.heartbeat import HeartbeatCog
from tests.conftest import make_bot


def _mock_client_session(status: int = 200, exc: Exception | None = None) -> MagicMock:
    """Build a stand-in for aiohttp.ClientSession usable as `async with ... as s`
    followed by `async with s.get(url) as resp`."""
    resp = MagicMock()
    resp.status = status

    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=resp)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(side_effect=exc) if exc is not None else MagicMock(return_value=get_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=session_cm)


async def test_inert_when_url_unset(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_URL", None)
    cog = HeartbeatCog(make_bot())
    assert not cog._heartbeat_loop.is_running()


async def test_loop_starts_when_url_set(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/ping")
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_MINUTES", 3)
    cog = HeartbeatCog(make_bot())
    try:
        assert cog._heartbeat_loop.is_running()
        # interval override took effect
        assert cog._heartbeat_loop.minutes == 3
    finally:
        cog.cog_unload()  # cancel the background task


async def test_send_heartbeat_success(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/ping")
    cog = HeartbeatCog(make_bot())
    cog.cog_unload()  # don't let the real loop fire during the test
    with patch("bot.cogs.heartbeat.aiohttp.ClientSession", _mock_client_session(status=200)):
        assert await cog._send_heartbeat() is True


async def test_send_heartbeat_reports_failure_on_4xx(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/ping")
    cog = HeartbeatCog(make_bot())
    cog.cog_unload()
    with patch("bot.cogs.heartbeat.aiohttp.ClientSession", _mock_client_session(status=503)):
        assert await cog._send_heartbeat() is False


async def test_send_heartbeat_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/ping")
    cog = HeartbeatCog(make_bot())
    cog.cog_unload()
    boom = _mock_client_session(exc=RuntimeError("network down"))
    with patch("bot.cogs.heartbeat.aiohttp.ClientSession", boom):
        # Must never raise — a missing ping is what the monitor alerts on.
        assert await cog._send_heartbeat() is False


async def test_send_heartbeat_noop_when_url_unset(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_URL", None)
    cog = HeartbeatCog(make_bot())
    # No URL → returns False without attempting any HTTP.
    with patch("bot.cogs.heartbeat.aiohttp.ClientSession") as sess:
        assert await cog._send_heartbeat() is False
        sess.assert_not_called()
