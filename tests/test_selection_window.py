"""Regression coverage for per-user selection recency and decision backfill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import consolidate as consolidate_cli
from engine.cli import filter as filter_cli
from engine.cli import score as score_cli
from engine.cli import summarize as summarize_cli
from engine.cli import verify as verify_cli
from engine.config import Settings
from engine.decision_backfill import backfill_historical_decision_profiles
from engine.llm.schemas import RelevanceVerdict, VerificationReport
from engine.models import Decision, Event

PROFILE_NAME = "volodymyr"


async def _event(session: AsyncSession, *, age_hours: int) -> Event:
    seen_at = datetime.now(UTC) - timedelta(hours=age_hours)
    event = Event(
        centroid=[0.0] * 1536,
        article_count=1,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        status="open",
    )
    session.add(event)
    await session.flush()
    return event


def _decision(
    *,
    stage_name: str,
    event_id: int,
    profile_name: str | None = PROFILE_NAME,
    decision_json: dict[str, object] | None = None,
) -> Decision:
    return Decision(
        run_id=uuid4(),
        stage_name=stage_name,
        stage_version="test",
        target_type="event",
        target_id=event_id,
        profile_name=profile_name,
        decision_json=decision_json or {"action": "test"},
    )


def _verdict() -> RelevanceVerdict:
    return RelevanceVerdict(
        relevant=True,
        categories=["policy"],
        why="Selection-window test.",
        confidence=0.9,
    )


def _report() -> VerificationReport:
    return VerificationReport(
        sources_count=1,
        primary_source_present=True,
        speaker_type="official",
        is_speculation=False,
        hype_score=0.1,
        contradictions=[],
        confidence=0.9,
        notes="Selection-window test.",
    )


@pytest.mark.asyncio
async def test_filter_candidates_respect_selection_window(db_session: AsyncSession) -> None:
    fresh = await _event(db_session, age_hours=2)
    await _event(db_session, age_hours=100)

    candidates = await filter_cli.load_filter_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=72,
    )

    assert [candidate.id for candidate in candidates] == [fresh.id]


@pytest.mark.asyncio
async def test_score_candidates_respect_selection_window(db_session: AsyncSession) -> None:
    fresh = await _event(db_session, age_hours=2)
    stale = await _event(db_session, age_hours=100)
    db_session.add_all(
        [
            _decision(
                stage_name="keyword_filter",
                event_id=event.id,
                decision_json={"action": "passed_keyword_filter"},
            )
            for event in (fresh, stale)
        ]
    )
    await db_session.flush()

    candidates = await score_cli.load_score_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=72,
    )

    assert [candidate.id for candidate in candidates] == [fresh.id]


@pytest.mark.asyncio
async def test_verify_candidates_respect_selection_window(db_session: AsyncSession) -> None:
    fresh = await _event(db_session, age_hours=2)
    stale = await _event(db_session, age_hours=100)
    db_session.add_all(
        [
            _decision(
                stage_name="relevance",
                event_id=event.id,
                decision_json={"action": "relevant", "verdict": _verdict().model_dump()},
            )
            for event in (fresh, stale)
        ]
    )
    await db_session.flush()

    candidates = await verify_cli.load_verify_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=72,
    )

    assert [candidate.event.id for candidate in candidates] == [fresh.id]


@pytest.mark.asyncio
async def test_summarize_candidates_respect_selection_window(db_session: AsyncSession) -> None:
    fresh = await _event(db_session, age_hours=2)
    stale = await _event(db_session, age_hours=100)
    for event in (fresh, stale):
        db_session.add_all(
            [
                _decision(
                    stage_name="relevance",
                    event_id=event.id,
                    decision_json={"action": "relevant", "verdict": _verdict().model_dump()},
                ),
                _decision(
                    stage_name="verify",
                    event_id=event.id,
                    decision_json={"action": "verified", "report": _report().model_dump()},
                ),
            ]
        )
    await db_session.flush()

    candidates = await summarize_cli.load_summarize_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=72,
    )

    assert [candidate.event.id for candidate in candidates] == [fresh.id]


@pytest.mark.asyncio
async def test_selection_window_is_configurable(db_session: AsyncSession) -> None:
    stale = await _event(db_session, age_hours=100)

    default_candidates = await filter_cli.load_filter_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=Settings().selection_window_hours,
    )
    wide_candidates = await filter_cli.load_filter_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=168,
    )

    assert default_candidates == []
    assert [candidate.id for candidate in wide_candidates] == [stale.id]


@pytest.mark.asyncio
async def test_shared_consolidate_stage_uses_its_own_window(db_session: AsyncSession) -> None:
    event = await _event(db_session, age_hours=2)
    settings = Settings(selection_window_hours=1, consolidate_window_hours=24)

    candidates = await consolidate_cli._load_candidates(
        db_session,
        window_hours=settings.consolidate_window_hours,
    )

    assert [candidate.id for candidate in candidates] == [event.id]


@pytest.mark.asyncio
async def test_backfill_prevents_archive_reselection_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    event = await _event(db_session, age_hours=2)
    per_user_decisions = [
        _decision(stage_name="keyword_filter", event_id=event.id, profile_name=None),
        _decision(stage_name="relevance", event_id=event.id, profile_name=None),
    ]
    shared_decision = _decision(stage_name="cluster", event_id=event.id, profile_name=None)
    db_session.add_all([*per_user_decisions, shared_decision])
    await db_session.flush()

    first_updated = await backfill_historical_decision_profiles(db_session, PROFILE_NAME)
    second_updated = await backfill_historical_decision_profiles(db_session, PROFILE_NAME)
    await db_session.flush()

    filter_candidates = await filter_cli.load_filter_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=72,
    )
    score_candidates = await score_cli.load_score_candidates(
        db_session,
        profile_name=PROFILE_NAME,
        selection_window_hours=72,
    )
    stored = list(await db_session.scalars(select(Decision).order_by(Decision.id)))

    assert first_updated == 2
    assert second_updated == 0
    assert filter_candidates == []
    assert score_candidates == []
    assert [decision.profile_name for decision in stored] == [
        PROFILE_NAME,
        PROFILE_NAME,
        None,
    ]
