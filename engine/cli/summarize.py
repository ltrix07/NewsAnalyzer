"""CLI command for OpenAI-based digest generation."""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal
from time import perf_counter
from typing import cast
from uuid import UUID, uuid4

import structlog
import typer
from sqlalchemy import String, exists, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Event as EventDTO
from engine.domain import VerifiedEvent as VerifiedEventDTO
from engine.llm.client import make_llm_client
from engine.llm.schemas import RelevanceVerdict, VerificationReport
from engine.models import Decision as DecisionModel
from engine.models import Event as EventModel
from engine.profile import load_profile
from engine.stages.base import Context
from engine.stages.summarize import SummarizeStage

logger = structlog.get_logger(__name__)

LIMIT_OPTION = typer.Option(default=None, min=1, help="Process at most this many events.")
PROFILE_OPTION = typer.Option(default=None, help="Override the configured profile name.")
MODEL_OPTION = typer.Option(default=None, help="Override the configured OpenAI model.")


async def _load_latest_stage_decision(
    session: AsyncSession,
    *,
    stage_name: str,
    event_id: int,
) -> DecisionModel | None:
    """Load the most recent decision row for one stage/event pair."""

    return cast(
        DecisionModel | None,
        await session.scalar(
            select(DecisionModel)
            .where(
                DecisionModel.stage_name == stage_name,
                DecisionModel.target_type == "event",
                DecisionModel.target_id == event_id,
            )
            .order_by(DecisionModel.created_at.desc(), DecisionModel.id.desc())
            .limit(1)
        ),
    )


async def load_summarize_candidates(
    session: AsyncSession,
    *,
    limit: int | None = None,
) -> list[VerifiedEventDTO]:
    """Load verified events that do not yet have a summarize decision."""

    has_verify = exists(
        select(1).where(
            DecisionModel.stage_name == "verify",
            DecisionModel.target_type == "event",
            DecisionModel.target_id == EventModel.id,
        )
    )
    has_summarize = exists(
        select(1).where(
            DecisionModel.stage_name == "summarize",
            DecisionModel.target_type == "digest",
            DecisionModel.decision_json["event_id"].astext == sql_cast(EventModel.id, String),
        )
    )
    stmt = select(EventModel).where(has_verify, ~has_summarize).order_by(EventModel.id)
    events = (await session.scalars(stmt)).all()

    candidates: list[VerifiedEventDTO] = []
    for event in events:
        relevance_decision = await _load_latest_stage_decision(
            session,
            stage_name="relevance",
            event_id=event.id,
        )
        verify_decision = await _load_latest_stage_decision(
            session,
            stage_name="verify",
            event_id=event.id,
        )

        if relevance_decision is None or verify_decision is None:
            logger.warning(
                "summarize_missing_upstream_decision",
                event_id=event.id,
                has_relevance=relevance_decision is not None,
                has_verify=verify_decision is not None,
            )
            continue

        if relevance_decision.decision_json.get("action") != "relevant":
            continue

        if verify_decision.decision_json.get("action") != "verified":
            continue

        verdict_payload = relevance_decision.decision_json.get("verdict")
        report_payload = verify_decision.decision_json.get("report")
        if not isinstance(verdict_payload, dict) or not isinstance(report_payload, dict):
            logger.warning(
                "summarize_malformed_upstream_decision",
                event_id=event.id,
                relevance_decision_id=relevance_decision.id,
                verify_decision_id=verify_decision.id,
            )
            continue

        candidates.append(
            VerifiedEventDTO(
                event=EventDTO.model_validate(event),
                verdict=RelevanceVerdict.model_validate(verdict_payload),
                report=VerificationReport.model_validate(report_payload),
            )
        )

    return candidates if limit is None else candidates[:limit]


async def summarize_command(
    limit: int | None = None,
    profile: str | None = None,
    model: str | None = None,
    run_id: UUID | None = None,
) -> None:
    """Summarize verified events into persisted digest rows."""

    settings = get_settings()
    resolved_profile = load_profile(profile or settings.profile_name, settings.profile_root)
    stage = SummarizeStage(
        make_llm_client(settings),
        resolved_profile,
        model or settings.openai_model_summarize,
    )
    resolved_run_id = run_id or uuid4()
    action_counts: Counter[str] = Counter()
    total_tokens = 0
    total_cost = Decimal("0")
    started_at = perf_counter()

    async with session_scope() as session:
        candidates = await load_summarize_candidates(session, limit=limit)
        ctx = Context(run_id=resolved_run_id, session=session, settings=settings)

        for verified_event in candidates:
            result = await stage.run(verified_event, ctx)
            action_counts[str(result.draft.decision_json["action"])] += 1
            total_tokens += (result.draft.input_tokens or 0) + (result.draft.output_tokens or 0)
            total_cost += result.draft.cost_usd or Decimal("0")

    elapsed = perf_counter() - started_at
    typer.echo(
        f"Run {resolved_run_id}: {dict(action_counts)}; tokens={total_tokens}; "
        f"cost=${total_cost:.6f}; elapsed={elapsed:.1f}s"
    )


def summarize_command_sync_wrapper(
    limit: int | None = LIMIT_OPTION,
    profile: str | None = PROFILE_OPTION,
    model: str | None = MODEL_OPTION,
) -> None:
    """Run the async summarize command inside a synchronous Typer wrapper."""

    asyncio.run(summarize_command(limit=limit, profile=profile, model=model))
