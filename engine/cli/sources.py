"""Typer commands for source registry inspection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

import engine.sources.rss  # noqa: F401
from engine.db import session_scope
from engine.sources import registry

app = typer.Typer(help="Inspect configured content sources.")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sources_config_path() -> Path:
    return _project_root() / "config" / "sources.yaml"


@app.command("list")
def list_sources() -> None:
    """List configured sources and whether an implementation is registered."""

    sources = registry.load_sources_config(_sources_config_path())
    headers = ("name", "kind", "enabled", "poll_interval_seconds", "implemented")
    rows = [
        (
            source.name,
            source.kind,
            str(source.enabled).lower(),
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
