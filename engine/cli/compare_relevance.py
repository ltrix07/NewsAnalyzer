"""Read-only offline comparison of relevance prompt versions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import typer
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Event as EventDTO
from engine.llm.client import make_llm_client
from engine.models import Decision, Event
from engine.stages.base import Context
from engine.stages.relevance import RelevanceStage
from engine.users import resolve_profile

LIMIT_OPTION = typer.Option(..., "--limit", min=1, help="Maximum events; each makes two LLM calls.")
PROFILE_OPTION = typer.Option(default=None, help="Override the configured profile name.")
MODEL_OPTION = typer.Option(default=None, help="Override the configured OpenAI model.")


async def load_comparison_candidates(
    session: AsyncSession, *, profile_name: str, selection_window_hours: int, limit: int
) -> list[EventDTO]:
    """Load recent events that passed this profile's keyword filter."""

    cutoff = datetime.now(UTC) - timedelta(hours=selection_window_hours)
    passed = exists(
        select(1).where(
            Decision.stage_name == "keyword_filter",
            Decision.target_type == "event",
            Decision.target_id == Event.id,
            Decision.profile_name == profile_name,
            Decision.decision_json["action"].astext == "passed_keyword_filter",
        )
    )
    statement = (
        select(Event)
        .where(Event.last_seen_at >= cutoff, passed)
        .order_by(Event.id.desc())
        .limit(limit)
    )
    return [EventDTO.model_validate(event) for event in (await session.scalars(statement)).all()]


async def compare_relevance_command(
    limit: int, profile: str | None = None, model: str | None = None
) -> None:
    """Print v3/v4 disagreements without writing pipeline decisions."""

    settings = get_settings()
    async with session_scope() as session:
        profile_name = profile or settings.profile_name
        resolved_profile = await resolve_profile(profile_name, session)
        client = make_llm_client(settings)
        selected_model = model or settings.openai_model_relevance
        v3 = RelevanceStage(client, resolved_profile, selected_model)
        v4 = RelevanceStage(client, resolved_profile, selected_model, use_v4=True)
        events = await load_comparison_candidates(
            session,
            profile_name=profile_name,
            selection_window_hours=settings.selection_window_hours,
            limit=limit,
        )
        ctx = Context(run_id=uuid4(), session=session, settings=settings)
        disagreements = 0
        for event in events:
            v3_result = await v3.evaluate(event, ctx)
            v4_result = await v4.evaluate(event, ctx)
            v3_verdict = v3_result.draft.decision_json["verdict"]
            v4_verdict = v4_result.draft.decision_json["verdict"]
            if v3_verdict["relevant"] != v4_verdict["relevant"]:
                disagreements += 1
                typer.echo(
                    f"Event {event.id}: v3={v3_verdict['relevant']} — {v3_verdict['why']}\n"
                    f"  v4={v4_verdict['relevant']} — {v4_verdict['why']}"
                )
        typer.echo(f"Compared {len(events)} events; disagreements={disagreements}")


def compare_relevance_sync_wrapper(
    limit: int = LIMIT_OPTION,
    profile: str | None = PROFILE_OPTION,
    model: str | None = MODEL_OPTION,
) -> None:
    """Run the async comparison command."""

    asyncio.run(compare_relevance_command(limit=limit, profile=profile, model=model))
