"""Load-bearing coverage for profile-scoped selection over shared events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import xxhash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import filter as filter_cli
from engine.cli import score as score_cli
from engine.cli import summarize as summarize_cli
from engine.cli import verify as verify_cli
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import DigestPayload, RelevanceVerdict, VerificationReport
from engine.models import Article, Decision, Digest, Event, EventMember, Source, User
from engine.users import resolve_profile


class _AlwaysPassLLMClient:
    async def call_structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        output_schema: type[RelevanceVerdict | VerificationReport | DigestPayload],
        max_tokens: int = 1024,
    ) -> LLMResponse[RelevanceVerdict | VerificationReport | DigestPayload]:
        del system, prompt, max_tokens
        if output_schema is RelevanceVerdict:
            output = RelevanceVerdict(
                relevant=True,
                categories=["policy"],
                why="Relevant to this profile.",
                confidence=0.9,
            )
        elif output_schema is VerificationReport:
            output = VerificationReport(
                sources_count=1,
                primary_source_present=True,
                speaker_type="official",
                is_speculation=False,
                hype_score=0.1,
                contradictions=[],
                confidence=0.9,
                notes="Verified for test.",
            )
        else:
            output = DigestPayload(
                headline="Shared event",
                summary=(
                    "A shared event summary long enough for the schema validation requirements."
                ),
                why_it_matters="It matters independently to this specific user profile.",
                confidence_level="high",
                caveats=[],
                citations=[],
            )
        return LLMResponse(
            output=output,
            usage=LLMUsage(input_tokens=10, output_tokens=5, cost_usd=Decimal("0.000001")),
            model=model,
        )


def _profile(display_name: str) -> dict[str, object]:
    return {
        "name": display_name,
        "location": "PL",
        "citizenship": "UA",
        "languages": ["en"],
        "output_language": "en",
        "interests": ["policy"],
        "not_interested": [],
        "keyword_rules": {"keep_if_matches": [], "drop_if_matches": []},
    }


@pytest.mark.asyncio
async def test_two_users_select_same_full_event_set_independently(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usernames = ["alice", "bob"]
    display_name = "Alex"
    db_session.add_all([User(username=name, profile=_profile(display_name)) for name in usernames])
    source = Source(name=f"multiuser-{uuid4()}", kind="rss")
    db_session.add(source)
    await db_session.flush()

    events: list[Event] = []
    for index in range(2):
        article = Article(
            source_id=source.id,
            url=f"https://example.com/multiuser/{index}",
            url_hash=xxhash.xxh64(f"url-{index}").digest(),
            content_hash=xxhash.xxh64(f"content-{index}").digest(),
            title=f"Policy event {index}",
            raw_text="An official policy update relevant to both profiles.",
            lang="en",
        )
        event = Event(
            centroid=[0.0] * 1536,
            article_count=1,
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            status="open",
        )
        db_session.add_all([article, event])
        await db_session.flush()
        db_session.add(
            EventMember(event_id=event.id, article_id=article.id, similarity_to_centroid=1.0)
        )
        events.append(event)
    await db_session.flush()

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    client = _AlwaysPassLLMClient()
    for module in (filter_cli, score_cli, verify_cli, summarize_cli):
        monkeypatch.setattr(module, "session_scope", fake_session_scope)
    for module in (score_cli, verify_cli, summarize_cli):
        monkeypatch.setattr(module, "make_llm_client", lambda settings: client)

    for username in usernames:
        await filter_cli.filter_command(profile=username)
        await score_cli.score_command(profile=username)
        await verify_cli.verify_command(profile=username)
        await summarize_cli.summarize_command(profile=username)

    event_ids = {event.id for event in events}
    decisions = list(
        await db_session.scalars(
            select(Decision).where(
                Decision.stage_name.in_(("keyword_filter", "relevance", "verify", "summarize"))
            )
        )
    )
    digests = list(
        await db_session.scalars(select(Digest).order_by(Digest.profile_name, Digest.id))
    )

    assert {decision.profile_name for decision in decisions} == set(usernames)
    assert all(decision.profile_name != display_name for decision in decisions)
    assert {digest.profile_name for digest in digests} == set(usernames)

    for username in usernames:
        for stage_name in ("keyword_filter", "relevance", "verify"):
            assert {
                decision.target_id
                for decision in decisions
                if decision.stage_name == stage_name and decision.profile_name == username
            } == event_ids
        assert sum(
            decision.stage_name == "summarize" and decision.profile_name == username
            for decision in decisions
        ) == len(event_ids)
        assert {
            digest.event_id for digest in digests if digest.profile_name == username
        } == event_ids

    for digest in digests:
        resolved_profile = await resolve_profile(digest.profile_name, db_session)
        assert resolved_profile.name == display_name
