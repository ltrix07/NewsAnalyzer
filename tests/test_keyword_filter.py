"""DB-backed tests for the keyword filter stage."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.domain import Event as EventDTO
from engine.models import Article, Decision, Event, EventMember, Source
from engine.profile import KeywordRules
from engine.stages.base import Context
from engine.stages.filter_rules import KeywordFilterStage


async def _create_source(session: AsyncSession, name: str = "keyword-source") -> Source:
    source = Source(name=name, kind="rss")
    session.add(source)
    await session.flush()
    return source


async def _create_event_with_article(
    session: AsyncSession,
    *,
    title: str,
    raw_text: str,
    suffix: str,
) -> Event:
    source = await _create_source(session, name=f"keyword-source-{suffix}")
    article = Article(
        source_id=source.id,
        url=f"https://example.com/{suffix}",
        url_hash=suffix.encode("utf-8").ljust(8, b"0")[:8],
        content_hash=suffix[::-1].encode("utf-8").ljust(8, b"1")[:8],
        title=title,
        raw_text=raw_text,
        lang="en",
    )
    event = Event(
        centroid=[0.0] * 1536,
        article_count=1,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        status="open",
    )
    session.add_all([article, event])
    await session.flush()
    session.add(EventMember(event_id=event.id, article_id=article.id, similarity_to_centroid=1.0))
    await session.flush()
    return event


def _context(session: AsyncSession) -> Context:
    return Context(run_id=uuid4(), session=session, settings=get_settings())


@pytest.mark.asyncio
async def test_keyword_filter_stage_passes_keep_match(db_session: AsyncSession) -> None:
    event = await _create_event_with_article(
        db_session,
        title="ECB updates policy",
        raw_text="The ECB changed rates.",
        suffix="keep",
    )
    stage = KeywordFilterStage(
        KeywordRules(
            keep_if_matches=[r"(?i)\bECB\b"],
            drop_if_matches=[r"(?i)\bconcert\b"],
        )
    )

    result = await stage.run(EventDTO.model_validate(event), _context(db_session))
    decision = await db_session.scalar(select(Decision).where(Decision.target_id == event.id))

    assert result.output is not None
    assert result.draft.decision_json["action"] == "passed_keyword_filter"
    assert result.draft.decision_json["rule"] == "keep"
    assert decision is not None


@pytest.mark.asyncio
async def test_keyword_filter_stage_drops_drop_only_match(db_session: AsyncSession) -> None:
    event = await _create_event_with_article(
        db_session,
        title="City concert announced",
        raw_text="A major concert opens tonight.",
        suffix="drop",
    )
    stage = KeywordFilterStage(
        KeywordRules(
            keep_if_matches=[r"(?i)\bECB\b"],
            drop_if_matches=[r"(?i)\bconcert\b"],
        )
    )

    result = await stage.run(EventDTO.model_validate(event), _context(db_session))

    assert result.output is None
    assert result.draft.decision_json["action"] == "dropped_by_keyword_filter"
    assert result.draft.decision_json["rule"] == "drop"


@pytest.mark.asyncio
async def test_keyword_filter_stage_defaults_to_pass_when_no_patterns_match(
    db_session: AsyncSession,
) -> None:
    event = await _create_event_with_article(
        db_session,
        title="Routine municipal budget meeting",
        raw_text="Officials discussed administrative updates.",
        suffix="default",
    )
    stage = KeywordFilterStage(
        KeywordRules(
            keep_if_matches=[r"(?i)\bECB\b"],
            drop_if_matches=[r"(?i)\bconcert\b"],
        )
    )

    result = await stage.run(EventDTO.model_validate(event), _context(db_session))

    assert result.output is not None
    assert result.draft.decision_json["action"] == "passed_keyword_filter"
    assert result.draft.decision_json["rule"] == "default"
