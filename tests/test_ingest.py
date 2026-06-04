"""Tests for the ingest stage against the database-backed article store."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.models import Article as ArticleModel
from engine.models import Decision as DecisionModel
from engine.models import Source as SourceModel
from engine.sources.base import RawArticle
from engine.stages.base import Context
from engine.stages.ingest import IngestStage


async def _create_source(session: AsyncSession, name: str = "bankier_rss") -> SourceModel:
    """Insert one source row to support ingest-stage tests."""

    source = SourceModel(name=name, kind="rss", url="https://example.com/feed.xml")
    session.add(source)
    await session.flush()
    return source


def _context(session: AsyncSession) -> Context:
    """Construct a fresh stage context for one ingest test."""

    return Context(run_id=uuid4(), session=session, settings=get_settings())


@pytest.mark.asyncio
async def test_ingest_inserts_article_from_raw_html(db_session: AsyncSession) -> None:
    """HTML input should be extracted, persisted, and logged as inserted."""

    source_name = f"bankier_rss_{uuid4().hex}"
    await _create_source(db_session, name=source_name)
    raw = RawArticle(
        source_name=source_name,
        url="https://example.com/story",
        raw_html="<html><body><p>Hello world</p></body></html>",
        title="Story title",
    )

    result = await IngestStage().run(raw, _context(db_session))
    stored = await db_session.scalar(select(ArticleModel).where(ArticleModel.url == raw.url))

    assert result.output is not None
    assert result.draft.decision_json["action"] == "inserted"
    assert stored is not None
    assert stored.content_hash is not None
    assert stored.raw_text is not None


@pytest.mark.asyncio
async def test_ingest_dedupes_by_source_and_url_hash(db_session: AsyncSession) -> None:
    """A second ingest of the same URL should not create another article row."""

    source_name = f"bankier_rss_{uuid4().hex}"
    await _create_source(db_session, name=source_name)
    raw = RawArticle(
        source_name=source_name,
        url="https://example.com/story",
        raw_html="<html><body><p>Hello world</p></body></html>",
    )
    stage = IngestStage()

    first = await stage.run(raw, _context(db_session))
    second = await stage.run(raw, _context(db_session))
    article_count = await db_session.scalar(
        select(func.count())
        .select_from(ArticleModel)
        .join(SourceModel)
        .where(SourceModel.name == source_name)
    )

    assert first.output is not None
    assert second.output is None
    assert second.draft.decision_json["action"] == "deduped_url"
    assert article_count == 1


@pytest.mark.asyncio
async def test_ingest_reports_unknown_source(db_session: AsyncSession) -> None:
    """Raw articles whose source is not synced into the DB should be skipped."""

    raw = RawArticle(
        source_name="missing_source",
        url="https://example.com/story",
        raw_text="plain text",
    )

    result = await IngestStage().run(raw, _context(db_session))

    assert result.output is None
    assert result.draft.target_id == 0
    assert result.draft.decision_json["action"] == "unknown_source"


@pytest.mark.asyncio
async def test_ingest_reports_extraction_failed(db_session: AsyncSession) -> None:
    """HTML that yields no extracted text and has no fallback text should be skipped."""

    source_name = f"bankier_rss_{uuid4().hex}"
    source = await _create_source(db_session, name=source_name)
    raw = RawArticle(
        source_name=source_name,
        url="https://example.com/empty",
        raw_html="<html></html>",
    )

    result = await IngestStage().run(raw, _context(db_session))

    assert result.output is None
    assert result.draft.target_id == source.id
    assert result.draft.decision_json["action"] == "extraction_failed"


@pytest.mark.asyncio
async def test_ingest_persists_raw_text_when_no_html_is_present(db_session: AsyncSession) -> None:
    """Text-only raw articles should persist their provided raw_text unchanged."""

    source_name = f"bankier_rss_{uuid4().hex}"
    await _create_source(db_session, name=source_name)
    raw = RawArticle(
        source_name=source_name,
        url="https://example.com/raw-text",
        raw_text="already extracted text",
    )

    result = await IngestStage().run(raw, _context(db_session))
    stored = await db_session.scalar(select(ArticleModel).where(ArticleModel.url == raw.url))

    assert result.output is not None
    assert stored is not None
    assert stored.raw_text == "already extracted text"
    decision = await db_session.scalar(
        select(DecisionModel).where(DecisionModel.target_id == stored.id)
    )
    assert decision is not None
    assert decision.decision_json["extracted_via"] == "raw_text"
