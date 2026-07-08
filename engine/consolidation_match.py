"""Shared same-event adjudication helpers for consolidation and delivery threading."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import Settings
from engine.llm.client import LLMResponse, make_llm_client
from engine.llm.prompts import render_prompt
from engine.llm.schemas import SameEventVerdict
from engine.models import Article, Event, EventMember, Source
from engine.stages._event_context import EventArticle


@dataclass(frozen=True, slots=True)
class PairJudgement:
    """Verdict and accounting for one candidate event pair."""

    same_event: bool
    reason: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


Adjudicator = Callable[[AsyncSession, Event, Event], Awaitable[PairJudgement]]


async def load_event_articles(
    session: AsyncSession,
    event_id: int,
    *,
    limit: int = 3,
) -> list[EventArticle]:
    """Load short member context for same-event adjudication."""

    rows = (
        await session.execute(
            select(Source.name, Article.title, Article.url, Article.raw_text)
            .select_from(EventMember)
            .join(Article, Article.id == EventMember.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(EventMember.event_id == event_id)
            .order_by(EventMember.similarity_to_centroid.desc(), EventMember.id)
            .limit(limit)
        )
    ).all()
    return [
        EventArticle(
            source_name=source_name,
            title=title,
            url=url,
            excerpt=(raw_text or "")[:600],
        )
        for source_name, title, url, raw_text in rows
    ]


async def same_event(
    session: AsyncSession,
    event_a: Event,
    event_b: Event,
    *,
    settings: Settings,
) -> PairJudgement:
    """Ask the cheap structured-output model whether two events are the same story."""

    client = make_llm_client(settings)
    response: LLMResponse[SameEventVerdict] = await client.call_structured(
        model=settings.openai_model_consolidate,
        system="You respond with a single SameEventVerdict object.",
        prompt=render_prompt(
            "consolidate_v1.j2",
            event_a=await load_event_articles(session, event_a.id, limit=3),
            event_b=await load_event_articles(session, event_b.id, limit=3),
        ),
        output_schema=SameEventVerdict,
        max_tokens=256,
    )
    return PairJudgement(
        same_event=response.output.same_event,
        reason=response.output.reason,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=response.usage.cost_usd,
    )


def default_adjudicator(settings: Settings) -> Adjudicator:
    """Build a settings-bound adjudicator suitable for injection points."""

    async def adjudicate(session: AsyncSession, left: Event, right: Event) -> PairJudgement:
        return await same_event(session, left, right, settings=settings)

    return adjudicate
