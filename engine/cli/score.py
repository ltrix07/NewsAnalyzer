"""CLI command for OpenAI-based event relevance scoring."""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal
from time import perf_counter
from uuid import UUID, uuid4

import typer
from sqlalchemy import exists, select

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Event as EventDTO
from engine.llm.client import make_llm_client
from engine.models import Decision as DecisionModel
from engine.models import Event as EventModel
from engine.profile import load_profile
from engine.stages.base import Context
from engine.stages.relevance import RelevanceStage

LIMIT_OPTION = typer.Option(default=None, min=1, help="Process at most this many events.")
PROFILE_OPTION = typer.Option(default=None, help="Override the configured profile name.")
MODEL_OPTION = typer.Option(default=None, help="Override the configured OpenAI model.")


async def score_command(
    limit: int | None = None,
    profile: str | None = None,
    model: str | None = None,
    run_id: UUID | None = None,
) -> None:
    """Score keyword-filtered events for personal relevance."""

    settings = get_settings()
    resolved_profile = load_profile(profile or settings.profile_name, settings.profile_root)
    stage = RelevanceStage(
        make_llm_client(settings),
        resolved_profile,
        model or settings.openai_model_relevance,
    )
    resolved_run_id = run_id or uuid4()
    action_counts: Counter[str] = Counter()
    total_tokens = 0
    total_cost = Decimal("0")
    started_at = perf_counter()

    async with session_scope() as session:
        passed_keyword_filter = exists(
            select(1).where(
                DecisionModel.stage_name == "keyword_filter",
                DecisionModel.target_type == "event",
                DecisionModel.target_id == EventModel.id,
                DecisionModel.decision_json["action"].astext == "passed_keyword_filter",
            )
        )
        already_scored = exists(
            select(1).where(
                DecisionModel.stage_name == "relevance",
                DecisionModel.target_type == "event",
                DecisionModel.target_id == EventModel.id,
            )
        )
        stmt = (
            select(EventModel).where(passed_keyword_filter, ~already_scored).order_by(EventModel.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        events = [EventDTO.model_validate(event) for event in (await session.scalars(stmt)).all()]
        ctx = Context(run_id=resolved_run_id, session=session, settings=settings)

        for event in events:
            result = await stage.run(event, ctx)
            action_counts[str(result.draft.decision_json["action"])] += 1
            total_tokens += (result.draft.input_tokens or 0) + (result.draft.output_tokens or 0)
            total_cost += result.draft.cost_usd or Decimal("0")

    elapsed = perf_counter() - started_at
    typer.echo(
        f"Run {resolved_run_id}: {dict(action_counts)}; tokens={total_tokens}; "
        f"cost=${total_cost:.6f}; elapsed={elapsed:.1f}s"
    )


def score_command_sync_wrapper(
    limit: int | None = LIMIT_OPTION,
    profile: str | None = PROFILE_OPTION,
    model: str | None = MODEL_OPTION,
) -> None:
    """Run the async score command inside a synchronous Typer wrapper."""

    asyncio.run(score_command(limit=limit, profile=profile, model=model))
