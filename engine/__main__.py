"""CLI entrypoint for bootstrapping the news analyzer engine."""

from __future__ import annotations

import tomllib
from pathlib import Path

import typer

from engine.cli.cluster import cluster_command_sync_wrapper
from engine.cli.cost import cost_command_sync_wrapper
from engine.cli.digests import app as digests_app
from engine.cli.embed import embed_command_sync_wrapper
from engine.cli.events import app as events_app
from engine.cli.fetch import fetch_command_sync_wrapper
from engine.cli.filter import filter_command_sync_wrapper
from engine.cli.ingest import ingest_command_sync_wrapper
from engine.cli.inspect import app as inspect_app
from engine.cli.run import run_command_sync_wrapper
from engine.cli.score import score_command_sync_wrapper
from engine.cli.sources import app as sources_app
from engine.cli.summarize import summarize_command_sync_wrapper
from engine.cli.verify import verify_command_sync_wrapper
from engine.config import get_settings
from engine.observability import configure_logging

app = typer.Typer(help="CLI entrypoint for the news analyzer engine.")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_project_version() -> str:
    pyproject_path = _project_root() / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        data = tomllib.load(pyproject_file)

    project_table = data.get("project")
    if not isinstance(project_table, dict):
        msg = "The [project] table is missing from pyproject.toml."
        raise RuntimeError(msg)

    version = project_table.get("version")
    if not isinstance(version, str):
        msg = "The project version is missing from pyproject.toml."
        raise RuntimeError(msg)

    return version


@app.callback()
def cli() -> None:
    """Root command group for engine subcommands."""


@app.command()
def version() -> None:
    """Print the installed project version."""

    typer.echo(_read_project_version())


app.add_typer(sources_app, name="sources")
app.add_typer(events_app, name="events")
app.add_typer(digests_app, name="digests")
app.add_typer(inspect_app, name="inspect")
app.command("cost")(cost_command_sync_wrapper)
app.command("fetch")(fetch_command_sync_wrapper)
app.command("ingest")(ingest_command_sync_wrapper)
app.command("embed")(embed_command_sync_wrapper)
app.command("cluster")(cluster_command_sync_wrapper)
app.command("filter")(filter_command_sync_wrapper)
app.command("run")(run_command_sync_wrapper)
app.command("score")(score_command_sync_wrapper)
app.command("verify")(verify_command_sync_wrapper)
app.command("summarize")(summarize_command_sync_wrapper)


def main() -> None:
    """Configure process-wide services and dispatch the CLI."""

    configure_logging(get_settings())
    app()


if __name__ == "__main__":
    main()
