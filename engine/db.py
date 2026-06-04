"""Async database engine and session factory definitions for the engine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from engine.config import get_settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the shared async SQLAlchemy engine."""

    settings = get_settings()
    return create_async_engine(settings.require_database_url())


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the shared async session factory."""

    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session that commits on success and rolls back on error."""

    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
