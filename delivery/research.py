"""Tavily-backed post-discussion research answers."""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Literal, Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.formatter import split_telegram_message
from delivery.strings import t
from engine.config import Settings
from engine.domain import Digest as DigestDTO
from engine.llm.client import LLMClient
from engine.llm.prompts import render_prompt
from engine.llm.schemas import ResearchReply
from engine.models import Decision, Digest
from engine.profile import load_profile
from engine.search.tavily import SearchResult

RESEARCH_STAGE_NAME = "research"
RESEARCH_STAGE_VERSION = "v1"


class SearchClient(Protocol):
    """Search client interface used by the research workflow."""

    async def search(
        self,
        *,
        query: str,
        search_depth: Literal["basic", "advanced"],
        max_results: int,
    ) -> list[SearchResult]:
        """Run a web search."""


async def research_digest_question(
    *,
    session: AsyncSession,
    settings: Settings,
    llm_client: LLMClient,
    search_client: SearchClient,
    chat_id: int,
    digest_id: int,
    question: str,
) -> list[str]:
    """Answer one question with Tavily results and return Telegram chunks."""

    if await _daily_research_count(session) >= settings.research_daily_cap:
        return [t("research_daily_cap", settings.ui_language)]

    digest_model = await session.scalar(select(Digest).where(Digest.id == digest_id))
    if digest_model is None:
        return [t("research_digest_not_found", settings.ui_language)]

    digest = DigestDTO.model_validate(digest_model)
    profile = load_profile(digest.profile_name, settings.profile_root)
    query = _build_search_query(question=question, headline=digest.headline)
    results = await search_client.search(
        query=query,
        search_depth=settings.tavily_search_depth,
        max_results=settings.tavily_max_results,
    )

    prompt = render_prompt(
        "research_v1.j2",
        digest=digest,
        question=question,
        results=results,
        output_language=profile.output_language,
    )
    response = await llm_client.call_structured(
        model=settings.research_model,
        system="You synthesize web search results for one digest follow-up question.",
        prompt=prompt,
        output_schema=ResearchReply,
        max_tokens=900,
    )

    session.add(
        Decision(
            run_id=uuid4(),
            stage_name=RESEARCH_STAGE_NAME,
            stage_version=RESEARCH_STAGE_VERSION,
            target_type="research",
            target_id=digest_id,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=Decimal(response.usage.cost_usd),
            decision_json={
                "chat_id": chat_id,
                "question": question,
                "digest_id": digest_id,
                "event_id": digest.event_id,
                "profile_name": digest.profile_name,
                "output_language": profile.output_language,
                "n_results": len(results),
                "query": query,
            },
        )
    )
    await session.flush()

    return split_telegram_message(
        _format_research_answer(response.output.answer, results, lang=settings.ui_language)
    )


async def _daily_research_count(session: AsyncSession) -> int:
    today_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    count = await session.scalar(
        select(func.count())
        .select_from(Decision)
        .where(
            Decision.target_type == "research",
            Decision.created_at >= today_start,
        )
    )
    return int(count or 0)


def _build_search_query(*, question: str, headline: str) -> str:
    return f"{question.strip()} {headline.strip()}".strip()


def _format_research_answer(answer: str, results: Sequence[SearchResult], *, lang: str) -> str:
    sections = [
        html.escape(t("research_disclaimer", lang), quote=False),
        html.escape(answer, quote=False),
    ]
    if results:
        sources = [
            (
                f"{index}. {html.escape(result.title, quote=False)} — "
                f"{html.escape(result.url, quote=False)}"
            )
            for index, result in enumerate(results, start=1)
        ]
        sections.append(t("research_sources_header", lang) + "\n" + "\n".join(sources))
    return "\n\n".join(sections)
