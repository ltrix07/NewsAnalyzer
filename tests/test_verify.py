"""DB-backed tests for the verify stage and CLI command."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import verify as verify_cli
from engine.config import get_settings
from engine.domain import Event as EventDTO
from engine.domain import ScoredEvent as ScoredEventDTO
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import RelevanceVerdict, VerificationReport
from engine.models import Article, Decision, Event, EventMember, Source
from engine.stages.base import Context
from engine.stages.verify import VerifyStage


class FakeLLMClient:
    def __init__(self, report: VerificationReport) -> None:
        self.report = report
        self.calls = 0

    async def call_structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        output_schema: type[VerificationReport],
        max_tokens: int = 1024,
    ) -> LLMResponse[VerificationReport]:
        self.calls += 1
        assert model == "gpt-4o"
        assert "VerificationReport object" in system
        assert "EVENT MEMBERS" in prompt
        return LLMResponse[VerificationReport](
            output=output_schema.model_validate(self.report.model_dump()),
            usage=LLMUsage(
                input_tokens=200,
                output_tokens=80,
                cost_usd=Decimal("0.001300"),
            ),
            model=model,
        )


async def _create_source(session: AsyncSession, name: str) -> Source:
    source = Source(name=name, kind="rss")
    session.add(source)
    await session.flush()
    return source


async def _seed_event_with_relevance_decision(
    session: AsyncSession,
    *,
    suffix: str,
    relevance_action: str = "relevant",
) -> tuple[Event, RelevanceVerdict]:
    first_source = await _create_source(session, f"verify-source-a-{suffix}")
    second_source = await _create_source(session, f"verify-source-b-{suffix}")

    first_article = Article(
        source_id=first_source.id,
        url=f"https://example.com/{suffix}/a",
        url_hash=f"{suffix}a".encode().ljust(8, b"0")[:8],
        content_hash=f"{suffix}A".encode().ljust(8, b"1")[:8],
        title=f"Title {suffix} A",
        raw_text="Regulators published an official update.",
        lang="en",
    )
    second_article = Article(
        source_id=second_source.id,
        url=f"https://example.com/{suffix}/b",
        url_hash=f"{suffix}b".encode().ljust(8, b"0")[:8],
        content_hash=f"{suffix}B".encode().ljust(8, b"1")[:8],
        title=f"Title {suffix} B",
        raw_text="Markets reacted to the regulator update.",
        lang="en",
    )
    event = Event(
        centroid=[0.0] * 1536,
        article_count=2,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        status="open",
    )
    session.add_all([first_article, second_article, event])
    await session.flush()
    session.add_all(
        [
            EventMember(event_id=event.id, article_id=first_article.id, similarity_to_centroid=1.0),
            EventMember(
                event_id=event.id, article_id=second_article.id, similarity_to_centroid=0.9
            ),
        ]
    )
    await session.flush()

    verdict = RelevanceVerdict(
        relevant=relevance_action == "relevant",
        categories=["Major EU policy decisions"] if relevance_action == "relevant" else [],
        why="Seeded relevance decision.",
        confidence=0.8,
    )
    session.add(
        Decision(
            run_id=uuid4(),
            stage_name="relevance",
            stage_version="v1",
            target_type="event",
            target_id=event.id,
            model="gpt-4o-mini",
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.000100"),
            decision_json={
                "action": relevance_action,
                "verdict": verdict.model_dump(),
            },
        )
    )
    await session.flush()
    return event, verdict


def _context(session: AsyncSession) -> Context:
    return Context(run_id=uuid4(), session=session, settings=get_settings())


def _report() -> VerificationReport:
    return VerificationReport(
        sources_count=2,
        primary_source_present=True,
        speaker_type="official",
        is_speculation=False,
        hype_score=0.2,
        contradictions=[],
        confidence=0.85,
        notes="Two independent sources agree on the official update.",
    )


@pytest.mark.asyncio
async def test_verify_stage_writes_decision_and_returns_verified_event(
    db_session: AsyncSession,
) -> None:
    event, verdict = await _seed_event_with_relevance_decision(db_session, suffix="stage")
    stage = VerifyStage(FakeLLMClient(_report()), "gpt-4o")

    result = await stage.run(
        ScoredEventDTO(event=EventDTO.model_validate(event), verdict=verdict),
        _context(db_session),
    )
    decision = await db_session.scalar(
        select(Decision).where(Decision.stage_name == "verify", Decision.target_id == event.id)
    )

    assert result.output is not None
    assert result.output.event.id == event.id
    assert result.output.verdict == verdict
    assert result.output.report == _report()
    assert decision is not None
    assert decision.decision_json["action"] == "verified"
    assert decision.decision_json["report"] == _report().model_dump()


@pytest.mark.asyncio
async def test_verify_cli_builds_scored_event_and_skips_on_rerun(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event, verdict = await _seed_event_with_relevance_decision(db_session, suffix="cli")
    fake_llm_client = FakeLLMClient(_report())

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(verify_cli, "make_llm_client", lambda settings: fake_llm_client)
    monkeypatch.setattr(verify_cli, "session_scope", fake_session_scope)

    candidates = await verify_cli.load_verify_candidates(db_session)
    assert len(candidates) == 1
    assert candidates[0].event.id == event.id
    assert candidates[0].verdict == verdict

    await verify_cli.verify_command(limit=10, profile=None, model="gpt-4o")
    first_count = await db_session.scalar(
        select(func.count()).select_from(Decision).where(Decision.stage_name == "verify")
    )

    await verify_cli.verify_command(limit=10, profile=None, model="gpt-4o")
    second_count = await db_session.scalar(
        select(func.count()).select_from(Decision).where(Decision.stage_name == "verify")
    )

    assert fake_llm_client.calls == 1
    assert first_count == 1
    assert second_count == 1


@pytest.mark.asyncio
async def test_verify_candidates_skip_events_whose_latest_relevance_is_irrelevant(
    db_session: AsyncSession,
) -> None:
    event, verdict = await _seed_event_with_relevance_decision(
        db_session,
        suffix="skip",
        relevance_action="irrelevant",
    )

    assert verdict.relevant is False
    candidates = await verify_cli.load_verify_candidates(db_session, event_id=event.id)
    verify_count = await db_session.scalar(
        select(func.count()).select_from(Decision).where(Decision.stage_name == "verify")
    )

    assert candidates == []
    assert verify_count == 0
