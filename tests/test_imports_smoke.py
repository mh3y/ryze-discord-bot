"""Import-smoke: every cog the bot loads must import cleanly.

setup_hook (bot/main.py) wraps load_extension in try/except and only logs a
failure, so a cog that fails to import — a SyntaxError, a bad `from … import`,
a NameError at module scope — would ship "green" and silently disable its whole
subsystem at runtime. The CI `compileall` step catches syntax errors; this test
catches the import-time errors compileall cannot, by importing exactly the
modules setup_hook loads. [review: SyntaxError in calendar_sync shipped green]
"""
import importlib

import pytest

from bot.main import COGS


@pytest.mark.parametrize("module_name", COGS)
def test_cog_module_imports(module_name: str) -> None:
    """Each loaded cog module imports without error."""
    importlib.import_module(module_name)


def test_entrypoint_imports() -> None:
    """The bot entry point itself imports without error."""
    importlib.import_module("bot.main")
