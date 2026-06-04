"""DB-backed tests for the clustering stage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.domain import Article as ArticleDTO
from engine.models import Article, Decision, Embedding, Event, EventMember, Source
from engine.stages.base import Context
from engine.stages.cluster import ClusterStage


async def _create_source(session: AsyncSession, name: str = "cluster-source") -> Source:
    source = Source(name=name, kind="rss")
    session.add(source)
    await session.flush()
    return source


async def _create_article_with_embedding(
    session: AsyncSession,
    source_id: int,
    suffix: str,
    vector: list[float],
    *,
    published_at: datetime | None = None,
) -> Article:
    article = Article(
        source_id=source_id,
        url=f"https://example.com/{suffix}",
        url_hash=suffix.encode("utf-8").ljust(8, b"0")[:8],
        content_hash=suffix[::-1].encode("utf-8").ljust(8, b"1")[:8],
        title=f"Title {suffix}",
        raw_text=f"Body {suffix}",
        lang="en",
        published_at=published_at,
    )
    session.add(article)
    await session.flush()
    session.add(
        Embedding(
            article_id=article.id,
            vector=vector,
            model="test-embedding-model",
        )
    )
    await session.flush()
    return article


def _context(session: AsyncSession) -> Context:
    return Context(run_id=uuid4(), session=session, settings=get_settings())


def _vector(*values: float) -> list[float]:
    return [*values, *([0.0] * (1536 - len(values)))]


async def _events_for_articles(session: AsyncSession, article_ids: list[int]) -> list[Event]:
    return (
        (
            await session.scalars(
                select(Event)
                .join(EventMember)
                .where(EventMember.article_id.in_(article_ids))
                .order_by(Event.id)
            )
        )
        .unique()
        .all()
    )


@pytest.mark.asyncio
async def test_cluster_stage_groups_identical_vectors_into_one_event(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    first = await _create_article_with_embedding(db_session, source.id, "a1", _vector(1.0))
    second = await _create_article_with_embedding(db_session, source.id, "a2", _vector(1.0))
    stage = ClusterStage(similarity_threshold=0.82, window_hours=36)
    ctx = _context(db_session)

    first_result = await stage.run(ArticleDTO.model_validate(first), ctx)
    second_result = await stage.run(ArticleDTO.model_validate(second), ctx)
    events = await _events_for_articles(db_session, [first.id, second.id])
    decisions = (
        await db_session.scalars(
            select(Decision).where(Decision.stage_name == "cluster").order_by(Decision.id)
        )
    ).all()

    assert first_result.output is not None
    assert second_result.output is not None
    assert len(events) == 1
    assert events[0].article_count == 2
    assert [decision.decision_json["action"] for decision in decisions] == [
        "created_event",
        "joined_event",
    ]


@pytest.mark.asyncio
async def test_cluster_stage_splits_orthogonal_vectors_into_separate_events(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    first = await _create_article_with_embedding(db_session, source.id, "b1", _vector(1.0, 0.0))
    second = await _create_article_with_embedding(db_session, source.id, "b2", _vector(0.0, 1.0))
    stage = ClusterStage(similarity_threshold=0.82, window_hours=36)
    ctx = _context(db_session)

    await stage.run(ArticleDTO.model_validate(first), ctx)
    await stage.run(ArticleDTO.model_validate(second), ctx)
    event_count = await db_session.scalar(
        select(func.count(func.distinct(Event.id)))
        .select_from(Event)
        .join(EventMember)
        .where(EventMember.article_id.in_([first.id, second.id]))
    )

    assert event_count == 2


@pytest.mark.asyncio
async def test_cluster_stage_does_not_join_events_outside_time_window(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    now = datetime.now(UTC)
    old_article = await _create_article_with_embedding(
        db_session,
        source.id,
        "c1",
        _vector(1.0),
        published_at=now - timedelta(hours=100),
    )
    recent_article = await _create_article_with_embedding(
        db_session,
        source.id,
        "c2",
        _vector(1.0),
        published_at=now,
    )
    stage = ClusterStage(similarity_threshold=0.82, window_hours=36)
    ctx = _context(db_session)

    await stage.run(ArticleDTO.model_validate(old_article), ctx)
    recent_result = await stage.run(ArticleDTO.model_validate(recent_article), ctx)
    events = await _events_for_articles(db_session, [old_article.id, recent_article.id])

    assert recent_result.draft.decision_json["action"] == "created_event"
    assert len(events) == 2


@pytest.mark.asyncio
async def test_cluster_stage_is_idempotent_for_already_clustered_articles(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    article = await _create_article_with_embedding(db_session, source.id, "d1", _vector(1.0))
    stage = ClusterStage(similarity_threshold=0.82, window_hours=36)
    ctx = _context(db_session)

    first_result = await stage.run(ArticleDTO.model_validate(article), ctx)
    second_result = await stage.run(ArticleDTO.model_validate(article), ctx)
    event_count = await db_session.scalar(
        select(func.count(func.distinct(Event.id)))
        .select_from(Event)
        .join(EventMember)
        .where(EventMember.article_id == article.id)
    )
    member_count = await db_session.scalar(
        select(func.count()).select_from(EventMember).where(EventMember.article_id == article.id)
    )

    assert first_result.output is not None
    assert second_result.output is None
    assert second_result.draft.decision_json["action"] == "already_clustered"
    assert event_count == 1
    assert member_count == 1


@pytest.mark.asyncio
async def test_cluster_stage_reports_missing_embedding_without_creating_rows(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    article = Article(
        source_id=source.id,
        url="https://example.com/no-embedding",
        url_hash=b"missing0",
        content_hash=b"missing1",
        title="Missing embedding",
        raw_text="Body",
        lang="en",
    )
    db_session.add(article)
    await db_session.flush()
    stage = ClusterStage(similarity_threshold=0.82, window_hours=36)
    ctx = _context(db_session)

    result = await stage.run(ArticleDTO.model_validate(article), ctx)
    event_count = await db_session.scalar(
        select(func.count(func.distinct(Event.id)))
        .select_from(Event)
        .join(EventMember)
        .where(EventMember.article_id == article.id)
    )
    member_count = await db_session.scalar(
        select(func.count()).select_from(EventMember).where(EventMember.article_id == article.id)
    )

    assert result.output is None
    assert result.draft.decision_json["action"] == "no_embedding"
    assert event_count == 0
    assert member_count == 0


@pytest.mark.asyncio
async def test_cluster_stage_updates_centroid_as_running_average(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    stage = ClusterStage(similarity_threshold=0.0, window_hours=36)
    ctx = _context(db_session)
    articles = [
        await _create_article_with_embedding(db_session, source.id, "e1", _vector(1.0, 0.0, 0.0)),
        await _create_article_with_embedding(db_session, source.id, "e2", _vector(0.0, 1.0, 0.0)),
        await _create_article_with_embedding(db_session, source.id, "e3", _vector(0.0, 0.0, 1.0)),
    ]

    for article in articles:
        await stage.run(ArticleDTO.model_validate(article), ctx)

    stored_event = await db_session.scalar(
        select(Event).join(EventMember).where(EventMember.article_id == articles[0].id)
    )

    assert stored_event is not None
    assert stored_event.article_count == 3
    assert stored_event.centroid[:3] == pytest.approx([1 / 3, 1 / 3, 1 / 3], abs=0.001)
