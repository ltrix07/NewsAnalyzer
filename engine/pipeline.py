"""Sequential pipeline orchestrator for one end-to-end engine run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select

from engine.cli.cluster import cluster_command
from engine.cli.embed import embed_command
from engine.cli.fetch import fetch_command
from engine.cli.filter import filter_command
from engine.cli.ingest import ingest_command
from engine.cli.score import score_command
from engine.cli.summarize import summarize_command
from engine.cli.verify import verify_command
from engine.db import session_scope
from engine.models import Decision

logger = structlog.get_logger(__name__)

STAGE_ORDER = (
    "fetch",
    "ingest",
    "embed",
    "cluster",
    "filter",
    "score",
    "verify",
    "summarize",
)
DECISION_STAGE_TO_PIPELINE_STAGE = {
    "keyword_filter": "filter",
    "relevance": "score",
}


class StageOutcome(BaseModel):
    status: Literal["ok", "skipped", "error"]
    error: str | None = None
    elapsed_seconds: float
    action_counts: dict[str, int] = Field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")


class RunSummary(BaseModel):
    run_id: UUID
    started_at: datetime
    elapsed_seconds: float
    stages: dict[str, StageOutcome]


def _stage_calls(
    *,
    run_id: UUID,
    source: str | None,
    limit_score: int | None,
    limit_verify: int | None,
    limit_summarize: int | None,
    profile_name: str | None,
) -> dict[str, Callable[[], Awaitable[None]]]:
    """Return ordered stage callables with orchestrator-owned argument plumbing."""

    return {
        "fetch": partial(fetch_command, source=source, run_id=run_id),
        "ingest": partial(ingest_command, run_id=run_id),
        "embed": partial(embed_command, run_id=run_id),
        "cluster": partial(cluster_command, run_id=run_id),
        "filter": partial(filter_command, profile=profile_name, run_id=run_id),
        "score": partial(score_command, limit=limit_score, profile=profile_name, run_id=run_id),
        "verify": partial(
            verify_command,
            limit=limit_verify,
            profile=profile_name,
            run_id=run_id,
        ),
        "summarize": partial(
            summarize_command,
            limit=limit_summarize,
            profile=profile_name,
            run_id=run_id,
        ),
    }


async def _load_decisions_by_run_id(run_id: UUID) -> list[Decision]:
    """Load every persisted decision row for one orchestrator run."""

    async with session_scope() as session:
        decisions = await session.scalars(
            select(Decision).where(Decision.run_id == run_id).order_by(Decision.id)
        )
        return list(decisions)


def _apply_decision_rollups(
    stages: Mapping[str, StageOutcome],
    decisions: Collection[Decision],
) -> None:
    """Accumulate action counts, tokens, and cost from persisted decisions."""

    for decision in decisions:
        pipeline_stage_name = DECISION_STAGE_TO_PIPELINE_STAGE.get(
            decision.stage_name,
            decision.stage_name,
        )
        outcome = stages.get(pipeline_stage_name)
        if outcome is None:
            continue

        action = decision.decision_json.get("action")
        if isinstance(action, str):
            outcome.action_counts[action] = outcome.action_counts.get(action, 0) + 1

        outcome.total_input_tokens += decision.input_tokens or 0
        outcome.total_output_tokens += decision.output_tokens or 0
        outcome.total_cost_usd += decision.cost_usd or Decimal("0")


async def run_once(
    *,
    source: str | None = None,
    skip: Collection[str] = frozenset(),
    limit_score: int | None = None,
    limit_verify: int | None = None,
    limit_summarize: int | None = None,
    profile_name: str | None = None,
    stop_on_error: bool = False,
) -> RunSummary:
    """Run all eight pipeline stages in order with one shared run id."""

    run_id = uuid4()
    started_at = datetime.now(UTC)
    started_perf = perf_counter()
    outcomes: dict[str, StageOutcome] = {}
    stage_calls = _stage_calls(
        run_id=run_id,
        source=source,
        limit_score=limit_score,
        limit_verify=limit_verify,
        limit_summarize=limit_summarize,
        profile_name=profile_name,
    )
    bound_contextvars = structlog.contextvars.bind_contextvars(run_id=str(run_id))
    halted = False

    try:
        for stage_name in STAGE_ORDER:
            if halted or stage_name in skip:
                outcomes[stage_name] = StageOutcome(status="skipped", elapsed_seconds=0.0)
                continue

            stage_started = perf_counter()
            try:
                await stage_calls[stage_name]()
            except Exception as exc:
                elapsed = perf_counter() - stage_started
                logger.exception(
                    "pipeline_stage_failed",
                    stage=stage_name,
                    exception_type=type(exc).__name__,
                )
                outcomes[stage_name] = StageOutcome(
                    status="error",
                    error=str(exc)[:200],
                    elapsed_seconds=elapsed,
                )
                if stop_on_error:
                    halted = True
            else:
                elapsed = perf_counter() - stage_started
                outcomes[stage_name] = StageOutcome(status="ok", elapsed_seconds=elapsed)
    finally:
        structlog.contextvars.reset_contextvars(**bound_contextvars)

    decisions = await _load_decisions_by_run_id(run_id)
    _apply_decision_rollups(outcomes, decisions)
    elapsed_seconds = perf_counter() - started_perf
    return RunSummary(
        run_id=run_id,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        stages=outcomes,
    )
