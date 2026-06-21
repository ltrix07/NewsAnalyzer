"""Shared pytest configuration for repository-local imports."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import get_settings
from engine.models import (
    Article,
    Decision,
    Digest,
    DigestFeedback,
    DiscussionPending,
    Embedding,
    Event,
    EventMember,
    Impression,
    ResearchPending,
    Source,
    TelegramCursor,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield a rollback-only async DB session, or skip if no DB is configured."""

    try:
        database_url = get_settings().database_url
        if database_url is None:
            raise RuntimeError("DATABASE_URL is not configured")

        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                session_factory = async_sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                session = session_factory()

                try:
                    for model in (
                        TelegramCursor,
                        ResearchPending,
                        DiscussionPending,
                        DigestFeedback,
                        Impression,
                        EventMember,
                        Digest,
                        Embedding,
                        Article,
                        Event,
                        Source,
                        Decision,
                    ):
                        await session.execute(delete(model))
                    await session.flush()
                    yield session
                finally:
                    await session.close()
                    await transaction.rollback()
        finally:
            await engine.dispose()
    except (OperationalError, RuntimeError, ValidationError) as exc:
        pytest.skip(f"Database is not reachable: {exc}")
