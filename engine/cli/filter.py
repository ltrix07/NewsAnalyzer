"""CLI command for deterministic event filtering via profile keyword rules."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import UUID, uuid4

import typer
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Event as EventDTO
from engine.models import Decision as DecisionModel
from engine.models import Event as EventModel
from engine.stages.base import Context
from engine.stages.filter_rules import KeywordFilterStage
from engine.users import resolve_profile

LIMIT_OPTION = typer.Option(default=None, min=1, help="Process at most this many events.")
PROFILE_OPTION = typer.Option(default=None, help="Override the configured profile name.")


async def load_filter_candidates(
    session: AsyncSession,
    *,
    profile_name: str,
    selection_window_hours: int,
    limit: int | None = None,
) -> list[EventDTO]:
    """Load fresh events that have not yet been filtered for this profile."""

    cutoff = datetime.now(UTC) - timedelta(hours=selection_window_hours)
    stmt = (
        select(EventModel)
        .where(
            EventModel.last_seen_at >= cutoff,
            ~exists(
                select(1).where(
                    DecisionModel.stage_name == "keyword_filter",
                    DecisionModel.target_type == "event",
                    DecisionModel.target_id == EventModel.id,
                    DecisionModel.profile_name == profile_name,
                )
            ),
        )
        .order_by(EventModel.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    return [EventDTO.model_validate(event) for event in (await session.scalars(stmt)).all()]


async def filter_command(
    limit: int | None = None,
    profile: str | None = None,
    run_id: UUID | None = None,
) -> None:
    """Run the cheap keyword filter over events that have not yet been filtered."""

    settings = get_settings()
    resolved_run_id = run_id or uuid4()
    action_counts: Counter[str] = Counter()
    started_at = perf_counter()

    async with session_scope() as session:
        username = profile or settings.profile_name
        resolved_profile = await resolve_profile(username, session)
        profile_name = username
        stage = KeywordFilterStage(resolved_profile.keyword_rules)
        events = await load_filter_candidates(
            session,
            profile_name=profile_name,
            selection_window_hours=settings.selection_window_hours,
            limit=limit,
        )
        ctx = Context(
            run_id=resolved_run_id,
            session=session,
            settings=settings,
            profile_name=profile_name,
        )

        for event in events:
            result = await stage.run(event, ctx)
            action_counts[str(result.draft.decision_json["action"])] += 1

    elapsed = perf_counter() - started_at
    typer.echo(f"Run {resolved_run_id}: {dict(action_counts)}; elapsed={elapsed:.1f}s")


def filter_command_sync_wrapper(
    limit: int | None = LIMIT_OPTION,
    profile: str | None = PROFILE_OPTION,
) -> None:
    """Run the async filter command inside a synchronous Typer wrapper."""

    asyncio.run(filter_command(limit=limit, profile=profile))
