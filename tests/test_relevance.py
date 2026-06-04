"""DB-backed tests for the relevance stage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.domain import Event as EventDTO
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import RelevanceVerdict
from engine.models import Article, Decision, Event, EventMember, Source
from engine.profile import Profile
from engine.stages.base import Context
from engine.stages.relevance import RelevanceStage


class FakeLLMClient:
    def __init__(self, verdict: RelevanceVerdict) -> None:
        self.verdict = verdict

    async def call_structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        output_schema: type[RelevanceVerdict],
        max_tokens: int = 1024,
    ) -> LLMResponse[RelevanceVerdict]:
        assert model == "gpt-4o-mini"
        assert system == "You output only via the submit_verdict tool."
        assert "USER PROFILE" in prompt
        return LLMResponse[RelevanceVerdict](
            output=output_schema.model_validate(self.verdict.model_dump()),
            usage=LLMUsage(
                input_tokens=120,
                output_tokens=40,
                cost_usd=Decimal("0.000320"),
            ),
            model=model,
        )


async def _create_source(session: AsyncSession, name: str = "relevance-source") -> Source:
    source = Source(name=name, kind="rss")
    session.add(source)
    await session.flush()
    return source


async def _create_event_with_article(session: AsyncSession, suffix: str) -> Event:
    source = await _create_source(session, name=f"relevance-source-{suffix}")
    article = Article(
        source_id=source.id,
        url=f"https://example.com/{suffix}",
        url_hash=suffix.encode("utf-8").ljust(8, b"0")[:8],
        content_hash=suffix[::-1].encode("utf-8").ljust(8, b"1")[:8],
        title=f"Title {suffix}",
        raw_text="ECB and KNF discussed market regulation updates.",
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


def _context(session: AsyncSession) -> Context:
    return Context(run_id=uuid4(), session=session, settings=get_settings())


@pytest.mark.asyncio
async def test_relevance_stage_returns_scored_event_when_relevant(
    db_session: AsyncSession,
) -> None:
    event = await _create_event_with_article(db_session, "relevant")
    verdict = RelevanceVerdict(
        relevant=True,
        categories=["Major EU policy decisions"],
        why="This matches the user's EU policy interest.",
        confidence=0.9,
    )
    stage = RelevanceStage(FakeLLMClient(verdict), _profile(), "gpt-4o-mini")

    result = await stage.run(EventDTO.model_validate(event), _context(db_session))

    assert result.output is not None
    assert result.output.verdict == verdict
    assert result.draft.decision_json["action"] == "relevant"


@pytest.mark.asyncio
async def test_relevance_stage_returns_none_when_irrelevant(db_session: AsyncSession) -> None:
    event = await _create_event_with_article(db_session, "irrelevant")
    verdict = RelevanceVerdict(
        relevant=False,
        categories=[],
        why="This does not intersect the user's interests.",
        confidence=0.2,
    )
    stage = RelevanceStage(FakeLLMClient(verdict), _profile(), "gpt-4o-mini")

    result = await stage.run(EventDTO.model_validate(event), _context(db_session))

    assert result.output is None
    assert result.draft.decision_json["action"] == "irrelevant"


@pytest.mark.asyncio
async def test_relevance_stage_decision_verdict_round_trips(db_session: AsyncSession) -> None:
    event = await _create_event_with_article(db_session, "roundtrip")
    verdict = RelevanceVerdict(
        relevant=True,
        categories=["Major EU policy decisions"],
        why="This matches the user's interests.",
        confidence=0.8,
    )
    stage = RelevanceStage(FakeLLMClient(verdict), _profile(), "gpt-4o-mini")

    await stage.run(EventDTO.model_validate(event), _context(db_session))
    decision = await db_session.scalar(
        select(Decision).where(Decision.stage_name == "relevance", Decision.target_id == event.id)
    )

    assert decision is not None
    restored = RelevanceVerdict.model_validate(decision.decision_json["verdict"])
    assert restored == verdict
