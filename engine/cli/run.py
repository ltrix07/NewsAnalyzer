"""Typer command for orchestrating the full pipeline end to end."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import typer

from engine.pipeline import STAGE_ORDER, RunSummary, run_once

SOURCE_OPTION = typer.Option(default=None, help="Fetch only one configured source by name.")
SKIP_OPTION = typer.Option(default=None, help="Skip one stage. Repeat the flag to skip multiple.")
LIMIT_SCORE_OPTION = typer.Option(
    default=None,
    min=1,
    help="Process at most this many events in the score stage.",
)
LIMIT_VERIFY_OPTION = typer.Option(
    default=None,
    min=1,
    help="Process at most this many events in the verify stage.",
)
LIMIT_SUMMARIZE_OPTION = typer.Option(
    default=None,
    min=1,
    help="Process at most this many events in the summarize stage.",
)
PROFILE_OPTION = typer.Option(default=None, help="Override the configured profile name.")
STOP_ON_ERROR_OPTION = typer.Option(
    default=False,
    help="Stop after the first failed stage instead of continuing.",
)


def _format_elapsed(summary: RunSummary, stage_name: str) -> str:
    outcome = summary.stages[stage_name]
    if outcome.status == "skipped":
        return "-"
    return f"{outcome.elapsed_seconds:.1f}s"


def _format_actions(summary: RunSummary, stage_name: str) -> str:
    outcome = summary.stages[stage_name]
    if outcome.status == "error" and outcome.error:
        return outcome.error
    if not outcome.action_counts:
        return "-"
    return str(outcome.action_counts)


def _format_tokens(summary: RunSummary, stage_name: str) -> str:
    outcome = summary.stages[stage_name]
    total_tokens = outcome.total_input_tokens + outcome.total_output_tokens
    if outcome.status == "skipped" or total_tokens == 0:
        return "-"
    return str(total_tokens)


def _format_cost(summary: RunSummary, stage_name: str) -> str:
    outcome = summary.stages[stage_name]
    if outcome.status == "skipped":
        return "-"
    return f"${outcome.total_cost_usd:.6f}"


def _print_summary(summary: RunSummary) -> None:
    typer.echo("=" * 63)
    typer.echo(f"Pipeline run {summary.run_id} finished in {summary.elapsed_seconds:.1f}s")
    typer.echo("-" * 63)
    typer.echo(
        f"{'Stage':<12} {'Status':<8} {'Elapsed':<8} {'Actions':<40} {'Tokens':>8} {'Cost':>11}"
    )
    for stage_name in STAGE_ORDER:
        outcome = summary.stages[stage_name]
        typer.echo(
            f"{stage_name:<12} "
            f"{outcome.status:<8} "
            f"{_format_elapsed(summary, stage_name):<8} "
            f"{_format_actions(summary, stage_name):<40.40} "
            f"{_format_tokens(summary, stage_name):>8} "
            f"{_format_cost(summary, stage_name):>11}"
        )
    typer.echo("-" * 63)
    total_cost = sum(
        (
            outcome.total_cost_usd
            for outcome in summary.stages.values()
            if outcome.status != "skipped"
        ),
        Decimal("0"),
    )
    typer.echo(f"Total cost: ${total_cost:.6f}")
    typer.echo("=" * 63)


async def run_command(
    source: str | None = None,
    skip: list[str] | None = None,
    limit_score: int | None = None,
    limit_verify: int | None = None,
    limit_summarize: int | None = None,
    profile: str | None = None,
    stop_on_error: bool = False,
) -> None:
    """Run the entire pipeline end to end with one shared run id."""

    summary = await run_once(
        source=source,
        skip=set(skip or []),
        limit_score=limit_score,
        limit_verify=limit_verify,
        limit_summarize=limit_summarize,
        profile_name=profile,
        stop_on_error=stop_on_error,
    )
    _print_summary(summary)


def run_command_sync_wrapper(
    source: str | None = SOURCE_OPTION,
    skip: list[str] | None = SKIP_OPTION,
    limit_score: int | None = LIMIT_SCORE_OPTION,
    limit_verify: int | None = LIMIT_VERIFY_OPTION,
    limit_summarize: int | None = LIMIT_SUMMARIZE_OPTION,
    profile: str | None = PROFILE_OPTION,
    stop_on_error: bool = STOP_ON_ERROR_OPTION,
) -> None:
    """Run the async orchestrator inside a synchronous Typer wrapper."""

    asyncio.run(
        run_command(
            source=source,
            skip=skip,
            limit_score=limit_score,
            limit_verify=limit_verify,
            limit_summarize=limit_summarize,
            profile=profile,
            stop_on_error=stop_on_error,
        )
    )
