"""DB-backed tests for the relevance stage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import compare_relevance
from engine.config import get_settings, load_country_registry
from engine.domain import Event as EventDTO
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import RelevanceVerdict
from engine.models import Article, Decision, Event, EventMember, Source
from engine.profile import Profile
from engine.stages._event_context import EventArticle
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


def _profile(residence_country: str | None = None, location: str = "PL (Warsaw)") -> Profile:
    return Profile.model_validate(
        {
            "name": "volodymyr",
            "location": location,
            "residence_country": residence_country,
            "citizenship": "UA",
            "languages": ["ru", "en"],
            "output_language": "ru",
            "interests": ["Major EU policy decisions"],
            "not_interested": ["Sports and esports"],
            "keyword_rules": {"keep_if_matches": [], "drop_if_matches": []},
        }
    )


def _render_v4(profile: Profile) -> str:
    stage = RelevanceStage(
        FakeLLMClient(RelevanceVerdict(relevant=False, categories=[], why="test", confidence=1.0)),
        profile,
        "gpt-4o-mini",
        use_v4=True,
    )
    return stage.render(
        [
            EventArticle(
                source_name="source", title="title", url="https://example.com", excerpt="text"
            )
        ]
    )


def test_relevance_v4_renders_polish_residence_slots() -> None:
    prompt = _render_v4(_profile("PL"))

    assert "Location: PL (Warsaw)" in prompt
    assert "B. POLAND AS IT AFFECTS FOREIGN RESIDENTS" in prompt
    assert "karta pobytu" in prompt
    assert "cudzoziemcy" in prompt
    assert "NBP" in prompt
    assert "PLN" in prompt
    assert "KNF" in prompt
    assert "C. UA-PL BILATERAL" in prompt


@pytest.mark.parametrize("country_code", [None])
def test_relevance_v4_unknown_residence_keeps_ua_tier_without_residence_categories(
    country_code: str | None,
) -> None:
    prompt = _render_v4(_profile(country_code, location="Unknown place"))

    assert "A. UKRAINE" in prompt
    assert "Ukrainian government / parliament / presidency" in prompt
    assert "B." not in prompt
    assert "C." not in prompt
    assert "ZZ" not in prompt
    assert "Location: Unknown place" in prompt
    assert (
        "do not reject an article merely because its reported development occurs outside Ukraine"
        in prompt
    )


def test_relevance_v4_zz_residence_renders_free_text_location_without_residence_tiers() -> None:
    prompt = _render_v4(_profile("ZZ", location="Portugal"))

    assert "Location: Portugal" in prompt
    assert "B." not in prompt
    assert "C." not in prompt


def test_relevance_v4_renders_every_configured_country() -> None:
    for country_code, country in load_country_registry().items():
        prompt = _render_v4(_profile(country_code))

        assert f"B. {country['labels']['en'].upper()} AS IT AFFECTS FOREIGN RESIDENTS" in prompt


@pytest.mark.parametrize("country_code", [*load_country_registry(), None, "ZZ"])
def test_relevance_v4_never_uses_language_for_selection(country_code: str | None) -> None:
    prompt = _render_v4(_profile(country_code))

    assert "Languages they read" not in prompt
    assert "ru or en" not in prompt
    assert "Do NOT reject an article because of the language it is written in" in prompt
    assert "whatever\n   that language is" in prompt


def test_relevance_v4_empty_country_slots_render_generic_category() -> None:
    prompt = _render_v4(_profile("DE", location="Germany"))

    assert "B. GERMANY AS IT AFFECTS FOREIGN RESIDENTS" in prompt
    assert "News about Germany insofar as it affects foreign residents" in prompt
    assert "C. UA-DE BILATERAL" in prompt
    assert "retail algorithmic trading" not in prompt


@pytest.mark.parametrize("country_code", ["PL", "DE", None, "ZZ"])
def test_relevance_v4_preserves_cohort_wide_safety_blocks(country_code: str | None) -> None:
    prompt = _render_v4(_profile(country_code))

    assert "STEP 2 — SIGNIFICANCE FILTER" in prompt
    assert "GROUNDING RULES" in prompt
    assert "ANTI-PATTERNS" in prompt
    assert "retail algorithmic trading" not in prompt


def test_relevance_stage_defaults_to_v3() -> None:
    stage = RelevanceStage(
        FakeLLMClient(RelevanceVerdict(relevant=False, categories=[], why="test", confidence=1.0)),
        _profile("PL"),
        "gpt-4o-mini",
    )

    assert stage.version == "v3"
    assert "USER'S PROFESSION (retail algorithmic trading)" in stage.render([])
    assert "Languages they read: ru, en" in stage.render([])


@pytest.mark.asyncio
async def test_comparison_command_writes_no_decisions(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = await _create_event_with_article(db_session, "comparison")

    class ComparingClient(FakeLLMClient):
        async def call_structured(
            self,
            *,
            model: str,
            system: str,
            prompt: str,
            output_schema: type[RelevanceVerdict],
            max_tokens: int = 1024,
        ) -> LLMResponse[RelevanceVerdict]:
            relevant = "USER'S PROFESSION" not in prompt
            self.verdict = RelevanceVerdict(
                relevant=relevant,
                categories=["Major EU policy decisions"] if relevant else [],
                why="v4" if relevant else "v3",
                confidence=1.0,
            )
            return await super().call_structured(
                model=model,
                system=system,
                prompt=prompt,
                output_schema=output_schema,
                max_tokens=max_tokens,
            )

    @asynccontextmanager
    async def current_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def candidates(*_args: object, **_kwargs: object) -> list[EventDTO]:
        return [EventDTO.model_validate(event)]

    async def profile(*_args: object, **_kwargs: object) -> Profile:
        return _profile("PL")

    client = ComparingClient(
        RelevanceVerdict(relevant=False, categories=[], why="initial", confidence=1.0)
    )
    monkeypatch.setattr(compare_relevance, "session_scope", current_session)
    monkeypatch.setattr(compare_relevance, "load_comparison_candidates", candidates)
    monkeypatch.setattr(compare_relevance, "resolve_profile", profile)
    monkeypatch.setattr(compare_relevance, "make_llm_client", lambda _settings: client)
    before = await db_session.scalar(select(func.count()).select_from(Decision))

    await compare_relevance.compare_relevance_command(limit=1, profile="volodymyr")

    after = await db_session.scalar(select(func.count()).select_from(Decision))
    assert after == before


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
