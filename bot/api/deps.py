"""Shared FastAPI dependencies."""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database import SessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session and always close it afterwards."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
