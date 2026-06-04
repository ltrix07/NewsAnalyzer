"""Typer commands for inspecting decision trails for events and runs."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from textwrap import shorten
from uuid import UUID

import typer
from sqlalchemy import BigInteger, select, union_all
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli._output import DIGEST_LABELS, safe_echo, wrap_text
from engine.config import get_settings
from engine.db import session_scope
from engine.models import Article, Decision, Digest, Event, EventMember, Source
from engine.pipeline import DECISION_STAGE_TO_PIPELINE_STAGE, STAGE_ORDER
from engine.profile import load_profile

app = typer.Typer(help="Inspect event and run audit trails.")
VERBOSE_INDENT = " " * 71


@dataclass
class RunBucket:
    actions: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal("0")


def _format_dt(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_timeline_dt(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _format_target(target_type: str, target_id: int) -> str:
    return f"{target_type}:{target_id}"


def _format_cost(value: Decimal | None) -> str:
    return f"${(value or Decimal('0')):.6f}"


def _format_tokens(input_tokens: int | None, output_tokens: int | None) -> str | None:
    if input_tokens is None and output_tokens is None:
        return None
    return f"(tokens {input_tokens or 0}+{output_tokens or 0})"


def _display_stage_name(stage_name: str) -> str:
    return DECISION_STAGE_TO_PIPELINE_STAGE.get(stage_name, stage_name)


def _stage_order_index(stage_name: str) -> int:
    pipeline_stage_name = DECISION_STAGE_TO_PIPELINE_STAGE.get(stage_name, stage_name)
    try:
        return STAGE_ORDER.index(pipeline_stage_name)
    except ValueError:
        return len(STAGE_ORDER)


async def _load_event_articles(
    session: AsyncSession,
    event_id: int,
) -> list[tuple[int, str, str | None, str, datetime | None]]:
    rows = (
        await session.execute(
            select(Article.id, Source.name, Article.title, Article.url, Article.published_at)
            .select_from(EventMember)
            .join(Article, Article.id == EventMember.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(EventMember.event_id == event_id)
            .order_by(EventMember.id)
        )
    ).all()
    return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]


async def inspect_event_command(event_id: int) -> None:
    """Print the full article/event/digest decision trail for one event."""

    async with session_scope() as session:
        event = await session.get(Event, event_id)
        if event is None:
            raise typer.BadParameter(f"Event {event_id} was not found.", param_hint="event_id")

        article_rows = await _load_event_articles(session, event_id)
        article_ids = [article_id for article_id, *_ in article_rows]

        article_decisions = select(
            Decision.id,
            Decision.created_at,
            Decision.stage_name,
            Decision.stage_version,
            Decision.target_type,
            Decision.target_id,
            Decision.model,
            Decision.input_tokens,
            Decision.output_tokens,
            Decision.cost_usd,
            Decision.decision_json,
        ).where(Decision.target_type == "article", Decision.target_id.in_(article_ids))
        event_decisions = select(
            Decision.id,
            Decision.created_at,
            Decision.stage_name,
            Decision.stage_version,
            Decision.target_type,
            Decision.target_id,
            Decision.model,
            Decision.input_tokens,
            Decision.output_tokens,
            Decision.cost_usd,
            Decision.decision_json,
        ).where(Decision.target_type == "event", Decision.target_id == event_id)
        digest_decisions = select(
            Decision.id,
            Decision.created_at,
            Decision.stage_name,
            Decision.stage_version,
            Decision.target_type,
            Decision.target_id,
            Decision.model,
            Decision.input_tokens,
            Decision.output_tokens,
            Decision.cost_usd,
            Decision.decision_json,
        ).where(
            Decision.target_type == "digest",
            sql_cast(Decision.decision_json["event_id"].astext, BigInteger) == event_id,
        )
        timeline_stmt = union_all(article_decisions, event_decisions, digest_decisions).subquery()
        timeline_rows = (
            (
                await session.execute(
                    select(timeline_stmt).order_by(
                        timeline_stmt.c.created_at.asc(),
                        timeline_stmt.c.id.asc(),
                    )
                )
            )
            .mappings()
            .all()
        )
        latest_digest = await session.scalar(
            select(Digest)
            .where(Digest.event_id == event_id)
            .order_by(Digest.created_at.desc(), Digest.id.desc())
            .limit(1)
        )

    safe_echo("=" * 63)
    safe_echo(
        f"Event {event.id} created {_format_dt(event.created_at)}, "
        f"{len(article_rows)} article{'s' if len(article_rows) != 1 else ''}, status={event.status}"
    )
    safe_echo(
        f"first_seen={_format_dt(event.first_seen_at)}, last_seen={_format_dt(event.last_seen_at)}"
    )
    safe_echo("-" * 63)
    safe_echo("ARTICLES:")
    for article_id, source_name, title, url, published_at in article_rows:
        title_text = shorten(title or "(no title)", width=70, placeholder="...")
        safe_echo(f"  [#{article_id} {source_name}] {title_text}")
        safe_echo(f"                           {url}")
        published_display = _format_dt(published_at) if published_at is not None else "unknown"
        safe_echo(f"                           published: {published_display}")
    safe_echo("-" * 63)
    safe_echo("DECISIONS TIMELINE:")

    total_cost = Decimal("0")
    for row in timeline_rows:
        action = row["decision_json"].get("action", "-")
        safe_echo(
            f"  {_format_timeline_dt(row['created_at'])}  "
            f"{str(row['stage_name']):<14} {str(row['stage_version']):<3}  "
            f"{_format_target(str(row['target_type']), int(row['target_id'])):<13} "
            f"{str(action):<15} {_format_cost(row['cost_usd'])}"
        )
        total_cost += row["cost_usd"] or Decimal("0")

        token_line = _format_tokens(row["input_tokens"], row["output_tokens"])
        if token_line is not None:
            token_suffix = f", {row['model']}" if row["model"] else ""
            safe_echo(f"{VERBOSE_INDENT}{token_line}{token_suffix}")

        if row["stage_name"] == "relevance":
            verdict = row["decision_json"].get("verdict", {})
            categories = verdict.get("categories", [])
            why = verdict.get("why")
            if categories:
                safe_echo(f"{VERBOSE_INDENT}categories: {categories}")
            if isinstance(why, str):
                for line in wrap_text(f'why: "{why}"', indent=VERBOSE_INDENT):
                    safe_echo(line)
        elif row["stage_name"] == "verify":
            report = row["decision_json"].get("report", {})
            safe_echo(
                f"{VERBOSE_INDENT}speaker={report.get('speaker_type')}, "
                f"sources={report.get('sources_count')}, "
                f"hype={report.get('hype_score')}"
            )
            notes = report.get("notes")
            if isinstance(notes, str):
                for line in wrap_text(f'notes: "{notes}"', indent=VERBOSE_INDENT):
                    safe_echo(line)
        elif row["stage_name"] == "summarize":
            confidence = row["decision_json"].get("confidence_level")
            if isinstance(confidence, str):
                safe_echo(f"{VERBOSE_INDENT}confidence={confidence}")

    if latest_digest is not None:
        settings = get_settings()
        profile_root = (
            settings.profile_root if settings.profile_root.exists() else Path("config/profiles")
        )
        profile = load_profile(latest_digest.profile_name, profile_root)
        labels = DIGEST_LABELS.get(profile.output_language, DIGEST_LABELS["en"])

        safe_echo("-" * 63)
        safe_echo(
            f"DIGEST #{latest_digest.id} (created {_format_dt(latest_digest.created_at)}, "
            f"{profile.output_language}):"
        )
        safe_echo(f"[{latest_digest.confidence_level.upper()}] {latest_digest.headline}")
        safe_echo("")
        for line in wrap_text(latest_digest.summary):
            safe_echo(line)
        safe_echo("")
        for line in wrap_text(f"{labels['why']} {latest_digest.why_it_matters}"):
            safe_echo(line)
        safe_echo("")
        for caveat in latest_digest.caveats:
            for line in wrap_text(f"! {caveat}"):
                safe_echo(line)
        if latest_digest.caveats:
            safe_echo("")
        safe_echo(labels["sources"])
        for citation in latest_digest.citations:
            safe_echo(f"  * {citation['source']}: {citation['title']}")
            safe_echo(f"    {citation['url']}")

    safe_echo("-" * 63)
    safe_echo(f"TOTAL COST FOR EVENT: ${total_cost:.6f}")
    safe_echo("=" * 63)


async def inspect_run_command(run_id: str) -> None:
    """Print a post-hoc summary for one orchestrator run id."""

    try:
        parsed_run_id = UUID(run_id)
    except ValueError as exc:
        raise typer.BadParameter(f"Malformed run_id: {run_id}", param_hint="run_id") from exc

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Decision.stage_name,
                    Decision.decision_json["action"].astext.label("action"),
                    sql_cast(Decision.input_tokens, BigInteger).label("input_tokens"),
                    sql_cast(Decision.output_tokens, BigInteger).label("output_tokens"),
                    Decision.cost_usd,
                ).where(Decision.run_id == parsed_run_id)
            )
        ).all()

    grouped: dict[str, RunBucket] = defaultdict(RunBucket)
    for stage_name, action, input_tokens, output_tokens, cost_usd in rows:
        display_stage = _display_stage_name(stage_name)
        bucket = grouped[display_stage]
        if isinstance(action, str):
            bucket.actions[action] = bucket.actions.get(action, 0) + 1
        bucket.input_tokens += input_tokens or 0
        bucket.output_tokens += output_tokens or 0
        bucket.cost += cost_usd or Decimal("0")

    ordered_stage_names = sorted(grouped, key=_stage_order_index)
    total_cost = sum((grouped[name].cost for name in ordered_stage_names), Decimal("0"))

    safe_echo(f"Run {parsed_run_id} ({len(ordered_stage_names)} stages with activity)")
    safe_echo("-" * 63)
    safe_echo(f"{'Stage':<12} {'Actions':<40} {'Tokens':>8} {'Cost':>11}")
    for stage_name in ordered_stage_names:
        bucket = grouped[stage_name]
        total_tokens = bucket.input_tokens + bucket.output_tokens
        token_display = "-" if total_tokens == 0 else str(total_tokens)
        safe_echo(
            f"{stage_name:<12} "
            f"{str(bucket.actions):<40.40} "
            f"{token_display:>8} "
            f"{_format_cost(bucket.cost):>11}"
        )
    safe_echo("-" * 63)
    safe_echo(f"Total: ${total_cost:.6f}")


@app.command("event")
def inspect_event(event_id: int) -> None:
    """Run the async event inspection command inside a synchronous Typer wrapper."""

    asyncio.run(inspect_event_command(event_id))


@app.command("run")
def inspect_run(run_id: str) -> None:
    """Run the async run inspection command inside a synchronous Typer wrapper."""

    asyncio.run(inspect_run_command(run_id))
