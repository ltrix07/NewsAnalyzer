"""CLI command for post-hoc decision cost rollups."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import typer
from sqlalchemy import func, select

from engine.cli._duration import parse_duration
from engine.cli._output import safe_echo
from engine.db import session_scope
from engine.models import Decision
from engine.pipeline import DECISION_STAGE_TO_PIPELINE_STAGE, STAGE_ORDER

SINCE_OPTION = typer.Option("7d", "--since", help="Look back by duration such as 24h, 7d, or 1w.")
BY_OPTION = typer.Option("stage", "--by", help="Aggregate by stage, day, or model.")


def _display_stage_name(stage_name: str) -> str:
    return DECISION_STAGE_TO_PIPELINE_STAGE.get(stage_name, stage_name)


def _stage_order_index(stage_name: str) -> int:
    display_name = _display_stage_name(stage_name)
    try:
        return STAGE_ORDER.index(display_name)
    except ValueError:
        return len(STAGE_ORDER)


async def cost_command(*, since: str = "7d", by: str = "stage") -> None:
    """Print cost rollups from persisted decision rows."""

    if by not in {"stage", "day", "model"}:
        raise typer.BadParameter("Expected stage, day, or model.", param_hint="--by")

    cutoff = datetime.now(UTC) - parse_duration(since)
    async with session_scope() as session:
        if by == "stage":
            rows = (
                await session.execute(
                    select(
                        Decision.stage_name,
                        func.count().label("calls"),
                        func.coalesce(func.sum(Decision.input_tokens), 0).label("input_tokens"),
                        func.coalesce(func.sum(Decision.output_tokens), 0).label("output_tokens"),
                        func.coalesce(func.sum(Decision.cost_usd), Decimal("0")).label("cost"),
                    )
                    .where(Decision.created_at >= cutoff)
                    .group_by(Decision.stage_name)
                )
            ).all()
            ordered_stage_rows = sorted(
                rows,
                key=lambda row: (
                    -(row.cost or Decimal("0")),
                    _stage_order_index(row.stage_name),
                ),
            )
            safe_echo(
                f"{'Stage':<12} {'Calls':>7} {'Input tokens':>14} "
                f"{'Output tokens':>15} {'Cost':>11}"
            )
            total_cost = Decimal("0")
            for row in ordered_stage_rows:
                total_cost += row.cost or Decimal("0")
                safe_echo(
                    f"{_display_stage_name(row.stage_name):<12} "
                    f"{row.calls:>7} "
                    f"{int(row.input_tokens or 0):>14} "
                    f"{int(row.output_tokens or 0):>15} "
                    f"${(row.cost or Decimal('0')):.6f}"
                )
        elif by == "day":
            day_bucket = func.date_trunc("day", Decision.created_at)
            rows = (
                await session.execute(
                    select(
                        day_bucket.label("day"),
                        func.count().label("calls"),
                        func.coalesce(func.sum(Decision.cost_usd), Decimal("0")).label("cost"),
                    )
                    .where(Decision.created_at >= cutoff)
                    .group_by(day_bucket)
                )
            ).all()
            ordered_day_rows = sorted(rows, key=lambda row: row.day)
            safe_echo(f"{'Day':<12} {'Calls':>7} {'Cost':>11}")
            total_cost = Decimal("0")
            for row in ordered_day_rows:
                total_cost += row.cost or Decimal("0")
                safe_echo(
                    f"{row.day.astimezone(UTC).strftime('%Y-%m-%d'):<12} "
                    f"{row.calls:>7} "
                    f"${(row.cost or Decimal('0')):.6f}"
                )
        else:
            model_rows = (
                await session.execute(
                    select(
                        Decision.model,
                        func.count().label("calls"),
                        func.coalesce(func.sum(Decision.input_tokens), 0).label("input_tokens"),
                        func.coalesce(func.sum(Decision.output_tokens), 0).label("output_tokens"),
                        func.coalesce(func.sum(Decision.cost_usd), Decimal("0")).label("cost"),
                    )
                    .where(Decision.created_at >= cutoff, Decision.model.is_not(None))
                    .group_by(Decision.model)
                )
            ).all()
            normalized_rows = [
                (
                    str(row.model),
                    row.calls,
                    row.input_tokens,
                    row.output_tokens,
                    row.cost,
                )
                for row in model_rows
                if row.model is not None
            ]
            ordered_model_rows = sorted(
                normalized_rows,
                key=lambda row: -(row[4] or Decimal("0")),
            )
            safe_echo(
                f"{'Model':<20} {'Calls':>7} {'Input tokens':>14} "
                f"{'Output tokens':>15} {'Cost':>11}"
            )
            total_cost = Decimal("0")
            for model_row in ordered_model_rows:
                total_cost += model_row[4] or Decimal("0")
                safe_echo(
                    f"{model_row[0]:<20} "
                    f"{model_row[1]:>7} "
                    f"{int(model_row[2] or 0):>14} "
                    f"{int(model_row[3] or 0):>15} "
                    f"${(model_row[4] or Decimal('0')):.6f}"
                )

    safe_echo("-" * 63)
    safe_echo(f"Total: ${total_cost:.6f}")


def cost_command_sync_wrapper(since: str = SINCE_OPTION, by: str = BY_OPTION) -> None:
    """Run the async cost command inside a synchronous Typer wrapper."""

    asyncio.run(cost_command(since=since, by=by))
