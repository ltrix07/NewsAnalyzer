"""Structured logging, decision writing, metrics, and cost rollups will live here."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import Settings
from engine.models import Decision

if TYPE_CHECKING:
    from engine.stages.base import DecisionDraft


def _coerce_log_level(log_level: str) -> int:
    return logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)


def configure_logging(settings: Settings) -> None:
    """Configure structlog for the current runtime environment."""

    level = _coerce_log_level(settings.log_level)
    renderer: structlog.typing.Processor
    if settings.app_env == "prod":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)

    # Silence noisy third-party loggers that print parser warnings on RSS
    # fragments. These are expected and handled by the ingest stage fallback.
    for noisy in ("trafilatura", "trafilatura.utils", "trafilatura.htmlprocessing", "htmldate"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


async def record_decision(
    session: AsyncSession,
    *,
    run_id: UUID,
    stage_name: str,
    stage_version: str,
    draft: DecisionDraft,
) -> int:
    """Persist a decision row and return its id. Flushes the session."""

    row = Decision(
        run_id=run_id,
        stage_name=stage_name,
        stage_version=stage_version,
        target_type=draft.target_type,
        target_id=draft.target_id,
        model=draft.model,
        input_tokens=draft.input_tokens,
        output_tokens=draft.output_tokens,
        cost_usd=draft.cost_usd,
        decision_json=draft.decision_json,
    )
    session.add(row)
    await session.flush()
    return row.id
