"""Typer commands for source registry inspection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import typer

import engine.sources.rss  # noqa: F401
from engine.config import get_settings
from engine.db import session_scope
from engine.sources import registry
from engine.sources.base import RawArticle, Source

app = typer.Typer(help="Inspect configured content sources.")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sources_config_path() -> Path:
    return _project_root() / "config" / "sources.yaml"


@app.command("list")
def list_sources() -> None:
    """List configured sources and whether an implementation is registered."""

    sources = registry.load_sources_config(_sources_config_path())
    headers = (
        "name",
        "kind",
        "enabled",
        "lang",
        "country",
        "topics",
        "poll_interval_seconds",
        "implemented",
    )
    rows = [
        (
            source.name,
            source.kind,
            str(source.enabled).lower(),
            source.lang,
            source.country or "-",
            ",".join(source.topics),
            str(source.poll_interval_seconds),
            str(source.kind in registry._REGISTRY).lower(),
        )
        for source in sources
    ]

    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]
    typer.echo("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        typer.echo("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


async def sync_sources_command() -> None:
    """Sync configured source definitions into the database."""

    configs = registry.load_sources_config(_sources_config_path())
    async with session_scope() as session:
        mapping = await registry.sync_sources_to_db(configs, session)
    typer.echo(str(mapping))


@app.command("sync")
def sync_sources() -> None:
    """Run source synchronization inside a synchronous Typer wrapper."""

    asyncio.run(sync_sources_command())


async def _validate_source(source: Source, timeout_seconds: float) -> int:
    """Fetch one source within its deadline and return its article count."""

    count = 0
    async with asyncio.timeout(timeout_seconds):
        articles: AsyncIterator[RawArticle] = source.fetch(use_cache=False)
        async for _article in articles:
            count += 1
    if count == 0:
        raise ValueError("no parseable articles returned")
    return count


async def validate_sources_command() -> bool:
    """Validate every enabled source using its registered fetch implementation."""

    configs = registry.load_sources_config(_sources_config_path())
    timeout_seconds = get_settings().http_timeout_seconds
    failed = False
    for config in configs:
        if not config.enabled:
            continue
        try:
            source = registry.build_source(config)
            count = await _validate_source(source, timeout_seconds)
        except Exception as exc:
            failed = True
            reason = str(exc).strip() or type(exc).__name__
            typer.echo(f"FAIL {config.name}: {reason}")
        else:
            typer.echo(f"OK {config.name} ({count} items)")
    return not failed


@app.command("validate")
def validate_sources() -> None:
    """Fetch enabled sources and fail if any yields no parseable articles."""

    if not asyncio.run(validate_sources_command()):
        raise typer.Exit(code=1)
