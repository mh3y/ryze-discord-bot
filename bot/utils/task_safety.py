"""Crash-safety for discord.ext.tasks loops. [H10/M20 · DEF-18]

By default an unhandled exception in a @tasks.loop body kills that loop
PERMANENTLY — the subsystem (job polling, calendar sync, reminders, …) silently
stops until the next process restart, while the bot still looks online. Every
loop in this bot registers this handler so a single bad tick logs loudly and
the loop restarts after a short back-off instead of dying.
"""
import asyncio
import logging

log = logging.getLogger(__name__)

# Back-off before reviving a crashed loop, so a persistent failure can't spin
# into a hot crash-loop. Module-level so tests can shrink it.
RESTART_DELAY_SECONDS: float = 15.0


def install_loop_restart(loop, name: str, bot=None) -> None:
    """Register an error handler on a tasks.Loop that logs the crash traceback
    and restarts the loop after RESTART_DELAY_SECONDS.

    Call once per loop (typically in the cog's __init__, before .start()).
    Replaces discord.py's default error handler, which only logs and lets the
    loop die.

    Pass ``bot`` so a crash-then-shutdown race cannot resurrect a zombie loop:
    the restart is scheduled 15s after a crash, and if the bot begins closing in
    that window the deferred restart is skipped. [review: loop-restart timer survives cog_unload]
    """

    def _safe_restart() -> None:
        try:
            if bot is not None and bot.is_closed():
                log.info("[loop:%s] bot is closing — not restarting.", name)
                return
            if not loop.is_running():
                loop.start()
                log.info("[loop:%s] restarted after crash.", name)
        except Exception:
            log.exception("[loop:%s] restart FAILED — subsystem is down until the next process restart.", name)

    async def _on_error(*args) -> None:
        # Bound cog loops receive (cog_self, error); free loops receive (error,).
        error = args[-1]
        log.error(
            "[loop:%s] iteration crashed — restarting in %.0fs.",
            name, RESTART_DELAY_SECONDS, exc_info=error,
        )
        # Schedule the revive OUTSIDE the dying task: at this point the loop's
        # own task is still unwinding, so calling start()/restart() here would
        # either no-op or cancel ourselves.
        asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, _safe_restart)

    loop.error(_on_error)
