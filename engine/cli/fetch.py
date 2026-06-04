"""CLI commands for fetching raw source data to on-disk JSONL files."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID

import structlog
import typer

import engine.sources.rss  # noqa: F401
from engine.config import get_settings
from engine.sources.base import RawArticle, SourceConfig
from engine.sources.registry import build_source, load_sources_config

logger = structlog.get_logger(__name__)

SOURCE_OPTION = typer.Option(default=None, help="Fetch only one configured source by name.")
SINCE_OPTION = typer.Option(
    default=None,
    help="Only emit articles published on or after this ISO datetime.",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sources_config_path() -> Path:
    return _project_root() / "config" / "sources.yaml"


def _select_source_config(
    source_name: str | None,
    source_configs: list[SourceConfig],
) -> list[SourceConfig]:
    """Select either one named source or all enabled sources."""

    if source_name is None:
        return [config for config in source_configs if config.enabled]

    for config in source_configs:
        if config.name == source_name:
            return [config]

    raise typer.BadParameter(f"Unknown source: {source_name}", param_hint="--source")


async def fetch_command(
    source: str | None = None,
    since: datetime | None = None,
    run_id: UUID | None = None,
) -> None:
    """Fetch raw articles and append them to JSONL files under raw storage."""

    del run_id

    settings = get_settings()
    source_configs = load_sources_config(_sources_config_path())
    selected_configs = _select_source_config(source, source_configs)
    selected_sources = [build_source(config) for config in selected_configs]

    started_at = perf_counter()
    total_articles = 0
    storage_day = datetime.now(UTC).date().isoformat()
    output_dir = settings.raw_storage_path / storage_day
    output_dir.mkdir(parents=True, exist_ok=True)

    failed_sources: list[str] = []
    for selected_source in selected_sources:
        article_count = 0
        output_path = output_dir / f"{selected_source.name}.jsonl"
        try:
            articles: AsyncIterator[RawArticle] = selected_source.fetch(since=since)
            with output_path.open("a", encoding="utf-8") as output_file:
                async for article in articles:
                    output_file.write(f"{article.model_dump_json()}\n")
                    article_count += 1
        except Exception as exc:
            failed_sources.append(selected_source.name)
            logger.warning(
                "source_fetch_failed",
                source=selected_source.name,
                exception_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            typer.echo(f"{selected_source.name}: FAILED ({type(exc).__name__})")
            continue

        total_articles += article_count
        typer.echo(f"{selected_source.name}: {article_count} articles -> {output_path.as_posix()}")

    elapsed = perf_counter() - started_at
    ok_count = len(selected_sources) - len(failed_sources)
    summary = (
        f"Fetched {total_articles} articles from {ok_count}/{len(selected_sources)} sources "
        f"in {elapsed:.1f}s"
    )
    if failed_sources:
        summary += f"; failed: {', '.join(failed_sources)}"
    typer.echo(summary)


def fetch_command_sync_wrapper(
    source: str | None = SOURCE_OPTION,
    since: datetime | None = SINCE_OPTION,
) -> None:
    """Run the async fetch command inside a synchronous Typer wrapper."""

    asyncio.run(fetch_command(source=source, since=since))
