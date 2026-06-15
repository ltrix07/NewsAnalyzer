"""Grounded single-turn discussion replies for delivered digests."""

from __future__ import annotations

import html
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.formatter import truncate_telegram_message
from engine.config import Settings
from engine.domain import Digest as DigestDTO
from engine.llm.client import LLMClient, LLMResponse
from engine.llm.prompts import render_prompt
from engine.llm.schemas import DiscussionReply
from engine.models import Decision, Digest
from engine.profile import load_profile
from engine.stages._event_context import EventArticle, load_event_articles

DISCUSSION_STAGE_NAME = "discussion"
DISCUSSION_STAGE_VERSION = "v1"
_DIGEST_NOT_FOUND_MESSAGE = (
    "Не нашёл этот разбор. Возможно, он уже недоступен."
)


async def answer_digest_question(
    *,
    session: AsyncSession,
    settings: Settings,
    llm_client: LLMClient,
    digest_id: int,
    question: str,
) -> str:
    """Answer one user question from the digest and cited event excerpts only."""

    digest_model = await session.scalar(select(Digest).where(Digest.id == digest_id))
    if digest_model is None:
        return _DIGEST_NOT_FOUND_MESSAGE

    digest = DigestDTO.model_validate(digest_model)
    articles = await load_event_articles(session, digest.event_id)
    profile = load_profile(digest.profile_name, settings.profile_root)

    response = await call_discussion_llm(
        settings=settings,
        llm_client=llm_client,
        digest=digest,
        articles=articles,
        question=question,
        output_language=profile.output_language,
    )

    session.add(
        Decision(
            run_id=uuid4(),
            stage_name=DISCUSSION_STAGE_NAME,
            stage_version=DISCUSSION_STAGE_VERSION,
            target_type="discussion",
            target_id=digest_id,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=Decimal(response.usage.cost_usd),
            decision_json={
                "question": question,
                "digest_id": digest_id,
                "event_id": digest.event_id,
                "profile_name": digest.profile_name,
                "output_language": profile.output_language,
                "article_count": len(articles),
            },
        )
    )
    await session.flush()

    # With max_tokens=700 the escaped answer should stay far below Telegram's
    # limit, so splitting an HTML entity during truncation is not expected.
    return truncate_telegram_message(html.escape(response.output.answer, quote=False))


async def call_discussion_llm(
    *,
    settings: Settings,
    llm_client: LLMClient,
    digest: DigestDTO,
    articles: list[EventArticle],
    question: str,
    output_language: str,
) -> LLMResponse[DiscussionReply]:
    """Render the discussion prompt and invoke the structured LLM call."""

    prompt = render_discussion_prompt(
        digest=digest,
        articles=articles,
        question=question,
        output_language=output_language,
    )
    return await llm_client.call_structured(
        model=settings.discussion_model,
        system="You answer follow-up questions about one delivered news digest.",
        prompt=prompt,
        output_schema=DiscussionReply,
        max_tokens=700,
    )


def render_discussion_prompt(
    *,
    digest: DigestDTO,
    articles: list[EventArticle],
    question: str,
    output_language: str,
) -> str:
    """Build the discussion prompt from the digest, excerpts, and user question."""

    return render_prompt(
        "discussion_v1.j2",
        digest=digest,
        articles=articles,
        question=question,
        output_language=output_language,
    )
