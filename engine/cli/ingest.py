"""CLI commands for ingesting raw JSONL files into the database."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import typer

from engine.config import get_settings
from engine.db import session_scope
from engine.sources.base import RawArticle
from engine.stages.base import Context
from engine.stages.ingest import IngestStage

DATE_OPTION = typer.Option(
    None,
    "--date",
    help="UTC date to ingest.",
)


def _raw_date_dir(target_date: date) -> Path:
    return get_settings().raw_storage_path / target_date.isoformat()


async def ingest_command(target_date: date | None = None, run_id: UUID | None = None) -> None:
    """Ingest raw JSONL files for one UTC day into articles and decisions."""

    settings = get_settings()
    resolved_date = target_date or datetime.now(UTC).date()
    raw_dir = _raw_date_dir(resolved_date)
    jsonl_files = sorted(raw_dir.glob("*.jsonl"))
    stage = IngestStage()
    resolved_run_id = run_id or uuid4()
    action_counts: Counter[str] = Counter()
    started_at = perf_counter()

    async with session_scope() as session:
        ctx = Context(run_id=resolved_run_id, session=session, settings=settings)
        for jsonl_file in jsonl_files:
            with jsonl_file.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    raw_article = RawArticle.model_validate_json(line)
                    result = await stage.run(raw_article, ctx)
                    action = str(result.draft.decision_json["action"])
                    action_counts[action] += 1

    elapsed = perf_counter() - started_at
    typer.echo(f"Run {resolved_run_id}: {dict(action_counts)}")
    typer.echo(f"Ingested {sum(action_counts.values())} raw articles in {elapsed:.1f}s")


def ingest_command_sync_wrapper(
    target_date: str | None = DATE_OPTION,
) -> None:
    """Run the async ingest command inside a synchronous Typer wrapper."""

    parsed_date = None if target_date is None else date.fromisoformat(target_date)
    asyncio.run(ingest_command(target_date=parsed_date))
