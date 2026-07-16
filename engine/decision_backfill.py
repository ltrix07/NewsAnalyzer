"""Backfill helpers for historical decisions created before multi-user selection."""

from __future__ import annotations

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from engine.models import Decision

PER_USER_STAGE_NAMES = ("keyword_filter", "relevance", "verify", "summarize")


def historical_profile_backfill_statement(default_profile: str) -> Update:
    """Build the idempotent update for pre-multi-user per-user decisions."""

    return (
        update(Decision)
        .where(
            Decision.profile_name.is_(None),
            Decision.stage_name.in_(PER_USER_STAGE_NAMES),
        )
        .values(profile_name=default_profile)
    )


async def backfill_historical_decision_profiles(
    session: AsyncSession,
    default_profile: str,
) -> int:
    """Attribute historical per-user decisions and return the number updated."""

    result = await session.execute(historical_profile_backfill_statement(default_profile))
    return result.rowcount if isinstance(result, CursorResult) else 0
