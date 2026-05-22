import os
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from dotenv import load_dotenv

load_dotenv()

# Stub out bot-only env vars so config.py doesn't raise at import time.
# Alembic only needs DATABASE_URL — the Discord/Google values are irrelevant here.
import os as _os
_os.environ["DISCORD_TOKEN"] = "migration_stub"
_os.environ["DISCORD_GUILD_ID"] = "0"
_os.environ["GOOGLE_CLIENT_ID"] = "migration_stub"
_os.environ["GOOGLE_CLIENT_SECRET"] = "migration_stub"
_os.environ["GOOGLE_REFRESH_TOKEN"] = "migration_stub"
_os.environ["GOOGLE_PROJECT_ID"] = "migration_stub"

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic can detect schema
from bot.database import Base  # noqa: E402  (must be after load_dotenv)
import bot.models  # noqa: E402

target_metadata = Base.metadata


def _sync_url(url: str) -> str:
    """Convert async URL variants to sync driver URLs for Alembic."""
    url = re.sub(r"^postgresql\+asyncpg://", "postgresql+psycopg2://", url)
    url = re.sub(r"^postgres://", "postgresql+psycopg2://", url)
    # sqlite+aiosqlite:// → sqlite:// (Alembic uses the sync driver)
    url = re.sub(r"^sqlite\+aiosqlite://", "sqlite:///", url)
    # Normalise any double-slash that results from the substitution
    url = re.sub(r"sqlite:////", "sqlite:///", url)
    return url


def get_url() -> str:
    raw = os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url", ""))
    return _sync_url(raw)


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
