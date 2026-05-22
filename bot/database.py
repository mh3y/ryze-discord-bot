from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from bot import config


class Base(DeclarativeBase):
    pass


def _async_url(url: str) -> str:
    """Convert postgresql:// to postgresql+asyncpg:// for the async engine."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        # Railway / Heroku shorthand
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def build_engine(database_url: str | None = None) -> AsyncEngine:
    url = _async_url(database_url or config.DATABASE_URL)
    return create_async_engine(url, echo=False, pool_pre_ping=True)


engine: AsyncEngine = build_engine()

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all_tables() -> None:
    """Create all tables (dev/test only — use Alembic in production)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
