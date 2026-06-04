"""Smoke tests for the async database layer and core ORM models."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engine.config import get_settings
from engine.models import Article, Decision, Source


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction that is rolled back at teardown."""

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
                    yield session
                finally:
                    await session.close()
                    await transaction.rollback()
        finally:
            await engine.dispose()
    except (OperationalError, RuntimeError, ValidationError) as exc:
        pytest.skip(f"Database is not reachable: {exc}")


@pytest.mark.asyncio
async def test_insert_source_round_trip(db_session: AsyncSession) -> None:
    """A source row should be inserted and selected back unchanged."""

    source = Source(name="unit-test-source", kind="rss", url="https://example.com/feed.xml")
    db_session.add(source)
    await db_session.flush()

    stored_source = await db_session.scalar(select(Source).where(Source.id == source.id))

    assert stored_source is not None
    assert stored_source.name == "unit-test-source"
    assert stored_source.kind == "rss"
    assert stored_source.enabled is True
    assert stored_source.poll_interval_seconds == 1800


@pytest.mark.asyncio
async def test_insert_article_round_trip(db_session: AsyncSession) -> None:
    """An article row should persist with its source foreign key."""

    source = Source(name="article-source", kind="html")
    db_session.add(source)
    await db_session.flush()

    article = Article(
        source_id=source.id,
        url="https://example.com/story",
        url_hash=b"12345678",
        content_hash=b"abcdefgh",
        title="Story title",
        raw_text="raw body",
        lang="en",
    )
    db_session.add(article)
    await db_session.flush()

    stored_article = await db_session.scalar(select(Article).where(Article.id == article.id))

    assert stored_article is not None
    assert stored_article.source_id == source.id
    assert stored_article.url == "https://example.com/story"
    assert stored_article.url_hash == b"12345678"
    assert stored_article.content_hash == b"abcdefgh"


@pytest.mark.asyncio
async def test_insert_decision_round_trip(db_session: AsyncSession) -> None:
    """A decision row should persist without foreign keys to its target row."""

    source = Source(name="decision-source", kind="rss")
    db_session.add(source)
    await db_session.flush()

    article = Article(
        source_id=source.id,
        url="https://example.com/decision",
        url_hash=b"87654321",
    )
    db_session.add(article)
    await db_session.flush()

    decision = Decision(
        run_id=uuid4(),
        stage_name="ingest",
        stage_version="v1",
        target_type="article",
        target_id=article.id,
        decision_json={"verdict": "ok"},
    )
    db_session.add(decision)
    await db_session.flush()

    stored_decision = await db_session.scalar(select(Decision).where(Decision.id == decision.id))

    assert stored_decision is not None
    assert stored_decision.target_type == "article"
    assert stored_decision.target_id == article.id
    assert stored_decision.decision_json == {"verdict": "ok"}


@pytest.mark.asyncio
async def test_duplicate_article_url_hash_raises_integrity_error(db_session: AsyncSession) -> None:
    """The unique constraint on (source_id, url_hash) should reject duplicates."""

    source = Source(name="duplicate-source", kind="rss")
    db_session.add(source)
    await db_session.flush()

    original = Article(source_id=source.id, url="https://example.com/first", url_hash=b"dupehash")
    duplicate = Article(source_id=source.id, url="https://example.com/second", url_hash=b"dupehash")
    db_session.add_all([original, duplicate])

    with pytest.raises(IntegrityError):
        await db_session.flush()
