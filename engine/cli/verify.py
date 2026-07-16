"""CLI command for OpenAI-based event verification."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import cast
from uuid import UUID, uuid4

import typer
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Event as EventDTO
from engine.domain import ScoredEvent as ScoredEventDTO
from engine.llm.client import make_llm_client
from engine.llm.schemas import RelevanceVerdict
from engine.models import Decision as DecisionModel
from engine.models import Event as EventModel
from engine.stages.base import Context
from engine.stages.verify import VerifyStage
from engine.users import resolve_profile

LIMIT_OPTION = typer.Option(default=None, min=1, help="Process at most this many events.")
PROFILE_OPTION = typer.Option(default=None, help="Override the configured profile name.")
MODEL_OPTION = typer.Option(default=None, help="Override the configured OpenAI model.")


async def _load_latest_relevance_decision(
    session: AsyncSession,
    event_id: int,
    profile_name: str,
) -> DecisionModel | None:
    """Load the most recent relevance decision for one event."""

    return cast(
        DecisionModel | None,
        await session.scalar(
            select(DecisionModel)
            .where(
                DecisionModel.stage_name == "relevance",
                DecisionModel.target_type == "event",
                DecisionModel.target_id == event_id,
                DecisionModel.profile_name == profile_name,
            )
            .order_by(DecisionModel.created_at.desc(), DecisionModel.id.desc())
            .limit(1)
        ),
    )


async def load_verify_candidates(
    session: AsyncSession,
    *,
    limit: int | None = None,
    event_id: int | None = None,
    profile_name: str,
    selection_window_hours: int,
) -> list[ScoredEventDTO]:
    """Load relevance-approved events that have not yet been verified."""

    cutoff = datetime.now(UTC) - timedelta(hours=selection_window_hours)
    has_any_relevance = exists(
        select(1).where(
            DecisionModel.stage_name == "relevance",
            DecisionModel.target_type == "event",
            DecisionModel.target_id == EventModel.id,
            DecisionModel.profile_name == profile_name,
        )
    )
    has_verify = exists(
        select(1).where(
            DecisionModel.stage_name == "verify",
            DecisionModel.target_type == "event",
            DecisionModel.target_id == EventModel.id,
            DecisionModel.profile_name == profile_name,
        )
    )
    stmt = (
        select(EventModel)
        .where(
            EventModel.last_seen_at >= cutoff,
            has_any_relevance,
            ~has_verify,
        )
        .order_by(EventModel.id)
    )
    if event_id is not None:
        stmt = stmt.where(EventModel.id == event_id)

    events = (await session.scalars(stmt)).all()
    candidates: list[ScoredEventDTO] = []
    for event in events:
        latest_relevance = await _load_latest_relevance_decision(session, event.id, profile_name)
        if latest_relevance is None:
            continue

        if latest_relevance.decision_json.get("action") != "relevant":
            continue

        verdict_payload = latest_relevance.decision_json.get("verdict")
        if not isinstance(verdict_payload, dict):
            msg = f"Relevance decision {latest_relevance.id} is missing a valid verdict payload."
            raise RuntimeError(msg)

        candidates.append(
            ScoredEventDTO(
                event=EventDTO.model_validate(event),
                verdict=RelevanceVerdict.model_validate(verdict_payload),
            )
        )

    return candidates if limit is None else candidates[:limit]


async def verify_command(
    limit: int | None = None,
    profile: str | None = None,
    model: str | None = None,
    run_id: UUID | None = None,
) -> None:
    """Verify relevant events that have not yet been processed by the verify stage."""

    settings = get_settings()
    stage = VerifyStage(make_llm_client(settings), model or settings.openai_model_verify)
    resolved_run_id = run_id or uuid4()
    action_counts: Counter[str] = Counter()
    total_tokens = 0
    total_cost = Decimal("0")
    started_at = perf_counter()

    async with session_scope() as session:
        username = profile or settings.profile_name
        await resolve_profile(username, session)
        profile_name = username
        candidates = await load_verify_candidates(
            session,
            limit=limit,
            profile_name=profile_name,
            selection_window_hours=settings.selection_window_hours,
        )
        ctx = Context(
            run_id=resolved_run_id,
            session=session,
            settings=settings,
            profile_name=profile_name,
        )

        for scored_event in candidates:
            result = await stage.run(scored_event, ctx)
            action_counts[str(result.draft.decision_json["action"])] += 1
            total_tokens += (result.draft.input_tokens or 0) + (result.draft.output_tokens or 0)
            total_cost += result.draft.cost_usd or Decimal("0")

    elapsed = perf_counter() - started_at
    typer.echo(
        f"Run {resolved_run_id}: {dict(action_counts)}; tokens={total_tokens}; "
        f"cost=${total_cost:.6f}; elapsed={elapsed:.1f}s"
    )


def verify_command_sync_wrapper(
    limit: int | None = LIMIT_OPTION,
    profile: str | None = PROFILE_OPTION,
    model: str | None = MODEL_OPTION,
) -> None:
    """Run the async verify command inside a synchronous Typer wrapper."""

    asyncio.run(verify_command(limit=limit, profile=profile, model=model))
