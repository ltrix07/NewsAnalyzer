"""DB-backed tests for event consolidation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import sqrt
from uuid import uuid4

import pytest
import xxhash
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import consolidate as consolidate_cli
from engine.config import get_settings
from engine.models import Article, Decision, Digest, Event, EventMember, Source


async def _create_source(session: AsyncSession) -> Source:
    source = Source(name=f"consolidate-source-{uuid4()}", kind="rss")
    session.add(source)
    await session.flush()
    return source


def _vector(index: int, *, common_dot: float = 0.70) -> list[float]:
    vector = [0.0] * 1536
    vector[0] = sqrt(common_dot)
    vector[index + 1] = sqrt(1.0 - common_dot)
    return vector


async def _create_event(
    session: AsyncSession,
    source: Source,
    *,
    suffix: str,
    vector: list[float],
    article_count: int = 1,
    first_seen_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Event:
    now = datetime.now(UTC)
    article = Article(
        source_id=source.id,
        url=f"https://example.com/consolidate/{suffix}",
        url_hash=xxhash.xxh64(f"url-{suffix}").digest(),
        content_hash=xxhash.xxh64(f"content-{suffix}").digest(),
        title=f"Kyiv attack fragment {suffix}",
        raw_text=f"Reports describe the same Kyiv attack fragment {suffix}.",
        lang="en",
    )
    event = Event(
        centroid=vector,
        article_count=article_count,
        first_seen_at=first_seen_at or now,
        last_seen_at=last_seen_at or now,
        status="open",
        created_at=created_at or now,
    )
    session.add_all([article, event])
    await session.flush()
    session.add(EventMember(event_id=event.id, article_id=article.id, similarity_to_centroid=0.95))
    await session.flush()
    return event


@asynccontextmanager
async def _fake_session_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


def _judgement(same_event: bool) -> consolidate_cli.PairJudgement:
    return consolidate_cli.PairJudgement(
        same_event=same_event,
        reason="test verdict",
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=3,
        cost_usd=Decimal("0.000004"),
    )


@pytest.mark.asyncio
async def test_consolidate_collapses_similar_events_into_canonical(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "consolidate_enabled", True)
    source = await _create_source(db_session)
    older = datetime.now(UTC) - timedelta(hours=2)
    newer = datetime.now(UTC)
    events = [
        await _create_event(
            db_session,
            source,
            suffix=f"same-{index}",
            vector=_vector(index),
            article_count=8 if index == 3 else 1,
            first_seen_at=older if index == 0 else newer,
            last_seen_at=newer if index != 1 else older,
        )
        for index in range(8)
    ]
    unrelated = await _create_event(
        db_session,
        source,
        suffix="unrelated",
        vector=[0.0, 1.0, *([0.0] * 1534)],
    )
    same_ids = {event.id for event in events}
    unrelated_id = unrelated.id
    canonical_id = events[3].id

    async def fake_adjudicator(
        session: AsyncSession,
        left: Event,
        right: Event,
    ) -> consolidate_cli.PairJudgement:
        del session
        return _judgement(left.id in same_ids and right.id in same_ids)

    monkeypatch.setattr(consolidate_cli, "session_scope", lambda: _fake_session_scope(db_session))

    await consolidate_cli.consolidate_command(
        min_similarity=0.60,
        max_neighbors=8,
        adjudicator=fake_adjudicator,
    )

    remaining_same_events = (
        await db_session.scalars(select(Event).where(Event.id.in_(same_ids)).order_by(Event.id))
    ).all()
    canonical = await db_session.get(Event, canonical_id)
    unrelated_after = await db_session.get(Event, unrelated_id)
    member_count = await db_session.scalar(
        select(func.count()).select_from(EventMember).where(EventMember.event_id == canonical_id)
    )
    decision = await db_session.scalar(select(Decision).where(Decision.stage_name == "consolidate"))

    assert [event.id for event in remaining_same_events] == [canonical_id]
    assert canonical is not None
    assert canonical.article_count == 15
    assert canonical.first_seen_at == older
    assert canonical.last_seen_at == newer
    assert len(canonical.centroid) == 1536
    assert member_count == 8
    assert unrelated_after is not None
    assert decision is not None
    assert decision.target_id == canonical_id
    assert decision.decision_json["absorbed_event_ids"] == [
        event.id for event in events if event.id != canonical_id
    ]
    assert decision.input_tokens > 0
    assert decision.cost_usd is not None


@pytest.mark.asyncio
async def test_consolidate_never_deletes_event_with_digest(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "consolidate_enabled", True)
    source = await _create_source(db_session)
    fresh = await _create_event(db_session, source, suffix="fresh", vector=_vector(1))
    delivered = await _create_event(db_session, source, suffix="delivered", vector=_vector(2))
    db_session.add(
        Digest(
            event_id=delivered.id,
            profile_name="volodymyr",
            headline="Delivered",
            summary="Already delivered.",
            why_it_matters="Already sent.",
            confidence_level="high",
            caveats=[],
            citations=[],
            stage_version="v3",
        )
    )
    await db_session.flush()

    async def same_adjudicator(
        session: AsyncSession,
        left: Event,
        right: Event,
    ) -> consolidate_cli.PairJudgement:
        del session, left, right
        return _judgement(True)

    monkeypatch.setattr(consolidate_cli, "session_scope", lambda: _fake_session_scope(db_session))

    await consolidate_cli.consolidate_command(adjudicator=same_adjudicator)

    assert await db_session.get(Event, fresh.id) is not None
    assert await db_session.get(Event, delivered.id) is not None
    assert await db_session.scalar(select(func.count()).select_from(Decision)) == 0


@pytest.mark.asyncio
async def test_consolidate_disabled_is_noop(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "consolidate_enabled", False)
    source = await _create_source(db_session)
    first = await _create_event(db_session, source, suffix="disabled-a", vector=_vector(4))
    second = await _create_event(db_session, source, suffix="disabled-b", vector=_vector(5))
    monkeypatch.setattr(consolidate_cli, "session_scope", lambda: _fake_session_scope(db_session))

    async def unused_adjudicator(
        session: AsyncSession,
        left: Event,
        right: Event,
    ) -> consolidate_cli.PairJudgement:
        del session, left, right
        return _judgement(True)

    await consolidate_cli.consolidate_command(adjudicator=unused_adjudicator)

    assert await db_session.get(Event, first.id) is not None
    assert await db_session.get(Event, second.id) is not None
    assert await db_session.scalar(select(func.count()).select_from(Decision)) == 0


@pytest.mark.asyncio
async def test_filter_candidates_only_include_canonical_after_consolidate(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "consolidate_enabled", True)
    source = await _create_source(db_session)
    first = await _create_event(
        db_session,
        source,
        suffix="filter-a",
        vector=_vector(7),
        article_count=2,
    )
    second = await _create_event(db_session, source, suffix="filter-b", vector=_vector(8))

    async def same_adjudicator(
        session: AsyncSession,
        left: Event,
        right: Event,
    ) -> consolidate_cli.PairJudgement:
        del session, left, right
        return _judgement(True)

    monkeypatch.setattr(consolidate_cli, "session_scope", lambda: _fake_session_scope(db_session))

    await consolidate_cli.consolidate_command(adjudicator=same_adjudicator)

    filter_candidate_ids = (
        await db_session.scalars(
            select(Event.id)
            .where(
                ~exists(
                    select(1).where(
                        Decision.stage_name == "keyword_filter",
                        Decision.target_type == "event",
                        Decision.target_id == Event.id,
                    )
                )
            )
            .order_by(Event.id)
        )
    ).all()

    assert filter_candidate_ids == [first.id]
    assert await db_session.get(Event, second.id) is None


@pytest.mark.asyncio
async def test_consolidate_candidates_remain_global_across_profiles(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    filtered = await _create_event(db_session, source, suffix="profiled", vector=_vector(10))
    untouched = await _create_event(db_session, source, suffix="untouched", vector=_vector(11))
    db_session.add(
        Decision(
            run_id=uuid4(),
            stage_name="keyword_filter",
            stage_version="v1",
            target_type="event",
            target_id=filtered.id,
            profile_name="alice",
            decision_json={"action": "passed_keyword_filter"},
        )
    )
    await db_session.flush()

    candidates = await consolidate_cli._load_candidates(db_session, window_hours=24)

    assert [event.id for event in candidates] == [untouched.id]


@pytest.mark.asyncio
async def test_consolidate_candidates_use_creation_time_not_story_time(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    now = datetime.now(UTC)
    fresh_backlog = await _create_event(
        db_session,
        source,
        suffix="fresh-backlog",
        vector=_vector(12),
        last_seen_at=now - timedelta(days=7),
        created_at=now,
    )
    await _create_event(
        db_session,
        source,
        suffix="old-ingestion",
        vector=_vector(13),
        last_seen_at=now,
        created_at=now - timedelta(days=7),
    )

    candidates = await consolidate_cli._load_candidates(db_session, window_hours=72)

    assert [event.id for event in candidates] == [fresh_backlog.id]


@pytest.mark.asyncio
async def test_candidate_pairs_include_similarity_above_cluster_threshold(
    db_session: AsyncSession,
) -> None:
    source = await _create_source(db_session)
    first = await _create_event(db_session, source, suffix="high-a", vector=_vector(14))
    second = await _create_event(db_session, source, suffix="high-b", vector=_vector(14))

    pairs = await consolidate_cli._candidate_pairs(
        db_session,
        [first, second],
        min_similarity=0.50,
        max_neighbors=1,
    )

    assert pairs == [(first.id, second.id)]
