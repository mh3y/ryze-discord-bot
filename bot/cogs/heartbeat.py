"""Cog: Heartbeat — pings an external dead-man's-switch so an outage is visible.

The bot's last outage (the OCI host being reclaimed by Oracle) was completely
silent — nobody knew until someone noticed the bot offline in Discord. A Render
Background Worker has no inbound HTTP for an uptime check, so instead the bot
pings a configurable HEARTBEAT_URL (e.g. a free healthchecks.io or UptimeRobot
"heartbeat" monitor) on a schedule. If the pings stop, that monitor alerts the
owner. If HEARTBEAT_URL is unset the cog is inert — a safe no-op in dev and tests.
"""
import logging

import aiohttp
from discord.ext import commands, tasks

from bot import config, health
from bot.utils.task_safety import install_loop_restart

log = logging.getLogger(__name__)

# The ping is suppressed (so the external monitor FIRES) when the last
# successful calendar sync is older than this. 3 sync intervals of grace, with
# a floor so aggressive sync intervals can't make the heartbeat flappy. [M8]
STALENESS_LIMIT_MINUTES: int = max(config.CALENDAR_SYNC_INTERVAL * 3, 30)


class HeartbeatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        if config.HEARTBEAT_URL:
            self._heartbeat_loop.change_interval(minutes=config.HEARTBEAT_INTERVAL_MINUTES)
            install_loop_restart(self._heartbeat_loop, "heartbeat.ping", self.bot)
            self._heartbeat_loop.start()
            log.info(
                "[heartbeat] enabled — pinging the dead-man's-switch every %d min "
                "(suppressed if the gateway is down or the last sync is >%d min old).",
                config.HEARTBEAT_INTERVAL_MINUTES, STALENESS_LIMIT_MINUTES,
            )
        else:
            log.info("[heartbeat] HEARTBEAT_URL not set — dead-man's-switch disabled.")

    def cog_unload(self) -> None:
        self._heartbeat_loop.cancel()

    async def _send_heartbeat(self) -> bool:
        """Ping HEARTBEAT_URL once. Returns True on a <400 response, False otherwise.

        Never raises: a failed/missing ping is exactly what the external monitor
        exists to catch, so a transient error here must never disturb the bot.
        """
        url = config.HEARTBEAT_URL
        if not url:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        log.warning("[heartbeat] ping returned HTTP %s", resp.status)
                        return False
                    return True
        except Exception:
            log.warning("[heartbeat] ping failed", exc_info=True)
            return False

    def _should_ping(self) -> bool:
        """The heartbeat must prove the bot WORKS, not that the process exists.
        Suppressing the ping lets the external monitor fire and alert. [M8]"""
        if not self.bot.is_ready():
            log.warning("[heartbeat] suppressing ping — gateway not connected/ready.")
            return False
        if not health.is_fresh(STALENESS_LIMIT_MINUTES):
            log.warning(
                "[heartbeat] suppressing ping — no successful calendar sync in the last "
                "%d min (last activity: %s). The monitor will alert.",
                STALENESS_LIMIT_MINUTES, health.last_activity_at().isoformat(),
            )
            return False
        return True

    @tasks.loop(minutes=5)
    async def _heartbeat_loop(self) -> None:
        if self._should_ping():
            await self._send_heartbeat()

    @_heartbeat_loop.before_loop
    async def _before_heartbeat_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HeartbeatCog(bot))
