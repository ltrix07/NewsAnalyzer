"""DB-backed tests for the summarize stage and CLI command."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import summarize as summarize_cli
from engine.config import get_settings
from engine.domain import Event as EventDTO
from engine.domain import VerifiedEvent as VerifiedEventDTO
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import Citation, DigestPayload, RelevanceVerdict, VerificationReport
from engine.models import Article, Decision, Digest, Event, EventMember, Source
from engine.profile import Profile
from engine.stages.base import Context
from engine.stages.summarize import SummarizeStage


class FakeLLMClient:
    def __init__(self, payload: DigestPayload) -> None:
        self.payload = payload
        self.calls = 0

    async def call_structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        output_schema: type[DigestPayload],
        max_tokens: int = 1024,
    ) -> LLMResponse[DigestPayload]:
        self.calls += 1
        assert model == "gpt-4o"
        assert "DigestPayload object" in system
        assert "UPSTREAM SIGNALS" in prompt
        return LLMResponse[DigestPayload](
            output=output_schema.model_validate(self.payload.model_dump()),
            usage=LLMUsage(
                input_tokens=300,
                output_tokens=120,
                cost_usd=Decimal("0.001950"),
            ),
            model=model,
        )


async def _create_source(session: AsyncSession, name: str) -> Source:
    source = Source(name=name, kind="rss")
    session.add(source)
    await session.flush()
    return source


def _profile() -> Profile:
    return Profile.model_validate(
        {
            "name": "volodymyr",
            "location": "PL (Warsaw)",
            "citizenship": "UA",
            "languages": ["ru", "en"],
            "output_language": "ru",
            "interests": ["Major EU policy decisions"],
            "not_interested": ["Sports and esports"],
            "keyword_rules": {"keep_if_matches": [], "drop_if_matches": []},
        }
    )


def _report() -> VerificationReport:
    return VerificationReport(
        sources_count=2,
        primary_source_present=True,
        speaker_type="official",
        is_speculation=False,
        hype_score=0.2,
        contradictions=[],
        confidence=0.9,
        notes="Official source confirmed by independent reporting.",
    )


def _payload() -> DigestPayload:
    return DigestPayload(
        headline="Ставка НБП без сюрпризов",
        summary=(
            "НБП сохранил ставку без изменений и опубликовал официальное пояснение. "
            "Рынок воспринял решение как осторожный сигнал без немедленного разворота политики. "
            "Независимые источники пересказывают одни и те же базовые факты без "
            "заметных противоречий."
        ),
        why_it_matters="Это влияет на ожидания по PLN и на оценку дальнейших решений регулятора.",
        confidence_level="high",
        caveats=["Заголовки у части пересказов звучат громче, чем сами факты."],
        citations=[
            Citation(
                source="nbp_official", title="NBP rate statement", url="https://example.com/nbp"
            ),
            Citation(
                source="reuters_rss", title="Market reaction", url="https://example.com/reuters"
            ),
        ],
    )


async def _seed_verified_event(
    session: AsyncSession,
    *,
    suffix: str,
    relevance_action: str = "relevant",
    verify_action: str = "verified",
) -> tuple[Event, RelevanceVerdict, VerificationReport]:
    first_source = await _create_source(session, f"summarize-source-a-{suffix}")
    second_source = await _create_source(session, f"summarize-source-b-{suffix}")
    first_article = Article(
        source_id=first_source.id,
        url=f"https://example.com/{suffix}/a",
        url_hash=f"{suffix}a".encode().ljust(8, b"0")[:8],
        content_hash=f"{suffix}A".encode().ljust(8, b"1")[:8],
        title=f"Title {suffix} A",
        raw_text="Official rate statement from the central bank.",
        lang="en",
    )
    second_article = Article(
        source_id=second_source.id,
        url=f"https://example.com/{suffix}/b",
        url_hash=f"{suffix}b".encode().ljust(8, b"0")[:8],
        content_hash=f"{suffix}B".encode().ljust(8, b"1")[:8],
        title=f"Title {suffix} B",
        raw_text="Reuters covered the same rate statement and market reaction.",
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
                event_id=event.id, article_id=second_article.id, similarity_to_centroid=0.95
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
    report = _report()
    session.add(
        Decision(
            run_id=uuid4(),
            stage_name="relevance",
            stage_version="v1",
            target_type="event",
            target_id=event.id,
            model="gpt-4o-mini",
            input_tokens=12,
            output_tokens=5,
            cost_usd=Decimal("0.000120"),
            decision_json={"action": relevance_action, "verdict": verdict.model_dump()},
        )
    )
    session.add(
        Decision(
            run_id=uuid4(),
            stage_name="verify",
            stage_version="v1",
            target_type="event",
            target_id=event.id,
            model="gpt-4o",
            input_tokens=40,
            output_tokens=20,
            cost_usd=Decimal("0.000300"),
            decision_json={"action": verify_action, "report": report.model_dump()},
        )
    )
    await session.flush()
    return event, verdict, report


def _context(session: AsyncSession) -> Context:
    return Context(run_id=uuid4(), session=session, settings=get_settings())


@pytest.mark.asyncio
async def test_summarize_stage_persists_digest_and_decision(db_session: AsyncSession) -> None:
    event, verdict, report = await _seed_verified_event(db_session, suffix="stage")
    stage = SummarizeStage(FakeLLMClient(_payload()), _profile(), "gpt-4o")

    result = await stage.run(
        VerifiedEventDTO(
            event=EventDTO.model_validate(event),
            verdict=verdict,
            report=report,
        ),
        _context(db_session),
    )
    digest_row = await db_session.scalar(select(Digest).where(Digest.event_id == event.id))
    decision = await db_session.scalar(
        select(Decision).where(Decision.stage_name == "summarize").order_by(Decision.id.desc())
    )

    assert result.output is not None
    assert digest_row is not None
    assert digest_row.profile_name == "volodymyr"
    assert digest_row.headline == _payload().headline
    assert digest_row.confidence_level == "high"
    assert decision is not None
    assert decision.target_type == "digest"
    assert decision.target_id == digest_row.id
    assert decision.decision_json["action"] == "digested"


@pytest.mark.asyncio
async def test_summarize_cli_reconstructs_verified_event_and_skips_on_rerun(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event, verdict, report = await _seed_verified_event(db_session, suffix="cli")
    fake_llm_client = FakeLLMClient(_payload())

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(summarize_cli, "make_llm_client", lambda settings: fake_llm_client)
    monkeypatch.setattr(summarize_cli, "session_scope", fake_session_scope)

    candidates = await summarize_cli.load_summarize_candidates(db_session)
    assert len(candidates) == 1
    assert candidates[0].event.id == event.id
    assert candidates[0].verdict == verdict
    assert candidates[0].report == report

    await summarize_cli.summarize_command(limit=10, profile=None, model="gpt-4o")
    first_digest_count = await db_session.scalar(select(func.count()).select_from(Digest))
    first_decision_count = await db_session.scalar(
        select(func.count()).select_from(Decision).where(Decision.stage_name == "summarize")
    )

    await summarize_cli.summarize_command(limit=10, profile=None, model="gpt-4o")
    second_digest_count = await db_session.scalar(select(func.count()).select_from(Digest))
    second_decision_count = await db_session.scalar(
        select(func.count()).select_from(Decision).where(Decision.stage_name == "summarize")
    )

    assert fake_llm_client.calls == 1
    assert first_digest_count == 1
    assert first_decision_count == 1
    assert second_digest_count == 1
    assert second_decision_count == 1


@pytest.mark.asyncio
async def test_summarize_candidates_skip_when_latest_verify_not_verified(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_verified_event(db_session, suffix="skip-verify", verify_action="pending_review")
    fake_llm_client = FakeLLMClient(_payload())

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(summarize_cli, "make_llm_client", lambda settings: fake_llm_client)
    monkeypatch.setattr(summarize_cli, "session_scope", fake_session_scope)

    candidates = await summarize_cli.load_summarize_candidates(db_session)
    await summarize_cli.summarize_command(limit=10, profile=None, model="gpt-4o")
    digest_count = await db_session.scalar(select(func.count()).select_from(Digest))

    assert candidates == []
    assert fake_llm_client.calls == 0
    assert digest_count == 0


@pytest.mark.asyncio
async def test_summarize_candidates_skip_when_latest_relevance_is_irrelevant(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_verified_event(db_session, suffix="skip-relevance", relevance_action="irrelevant")
    fake_llm_client = FakeLLMClient(_payload())

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(summarize_cli, "make_llm_client", lambda settings: fake_llm_client)
    monkeypatch.setattr(summarize_cli, "session_scope", fake_session_scope)

    candidates = await summarize_cli.load_summarize_candidates(db_session)
    await summarize_cli.summarize_command(limit=10, profile=None, model="gpt-4o")
    digest_count = await db_session.scalar(select(func.count()).select_from(Digest))

    assert candidates == []
    assert fake_llm_client.calls == 0
    assert digest_count == 0
