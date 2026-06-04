"""Typer commands for inspecting persisted digests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import select

from engine.cli._duration import parse_duration
from engine.cli._output import DIGEST_LABELS, safe_echo
from engine.config import get_settings
from engine.db import session_scope
from engine.models import Digest as DigestModel
from engine.profile import load_profile

app = typer.Typer(help="Inspect persisted digests.")

LIMIT_OPTION = typer.Option(default=10, min=1, help="Show at most this many digests.")
SINCE_OPTION = typer.Option("24h", "--since", help="Look back by duration such as 24h or 7d.")
MIN_CONFIDENCE_OPTION = typer.Option(
    "low",
    "--min-confidence",
    help="Filter by minimum confidence: high, medium, or low.",
)

_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


async def show_digests_command(
    *,
    limit: int = 10,
    since: str = "24h",
    min_confidence: str = "low",
) -> None:
    """Print recent digests in a plain-text human-readable format."""

    if min_confidence not in _CONFIDENCE_ORDER:
        raise typer.BadParameter("Expected high, medium, or low.", param_hint="--min-confidence")

    cutoff = datetime.now(UTC) - parse_duration(since)
    threshold = _CONFIDENCE_ORDER[min_confidence]
    async with session_scope() as session:
        digests = (
            await session.scalars(
                select(DigestModel)
                .where(DigestModel.created_at >= cutoff)
                .order_by(DigestModel.created_at.desc())
            )
        ).all()

    filtered = [
        digest
        for digest in digests
        if _CONFIDENCE_ORDER.get(digest.confidence_level, -1) >= threshold
    ][:limit]
    settings = get_settings()
    profile_root = (
        settings.profile_root if settings.profile_root.exists() else Path("config/profiles")
    )

    for digest in filtered:
        profile = load_profile(digest.profile_name, profile_root)
        labels = DIGEST_LABELS.get(profile.output_language, DIGEST_LABELS["en"])
        safe_echo("=" * 63)
        safe_echo(
            f"[{digest.created_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} | "
            f"{digest.confidence_level.upper()}] {digest.headline}"
        )
        safe_echo("-" * 63)
        safe_echo(digest.summary)
        safe_echo("")
        safe_echo(labels["why"])
        safe_echo(digest.why_it_matters)
        safe_echo("")
        for caveat in digest.caveats:
            safe_echo(f"! {caveat}")
        if digest.caveats:
            safe_echo("")
        safe_echo(labels["sources"])
        for citation in digest.citations:
            safe_echo(f"  * {citation['source']}: {citation['title']}")
            safe_echo(f"    {citation['url']}")


@app.command("show")
def show_digests(
    limit: int = LIMIT_OPTION,
    since: str = SINCE_OPTION,
    min_confidence: str = MIN_CONFIDENCE_OPTION,
) -> None:
    """Run the async digests show command inside a synchronous Typer wrapper."""

    asyncio.run(show_digests_command(limit=limit, since=since, min_confidence=min_confidence))
