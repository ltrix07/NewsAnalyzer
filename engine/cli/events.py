"""Typer commands for inspecting clustered events."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import typer
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from engine.cli._duration import parse_duration
from engine.db import session_scope
from engine.models import Event as EventModel
from engine.models import EventMember as EventMemberModel

app = typer.Typer(help="Inspect clustered events.")

SINCE_OPTION = typer.Option("24h", "--since", help="Look back by duration such as 24h, 7d, or 1w.")


async def list_events_command(since: str = "24h") -> None:
    """Print recent events and their member articles."""

    cutoff = datetime.now(UTC) - parse_duration(since)
    async with session_scope() as session:
        stmt = (
            select(EventModel)
            .where(EventModel.last_seen_at >= cutoff)
            .options(selectinload(EventModel.members).selectinload(EventMemberModel.article))
            .order_by(EventModel.last_seen_at.desc())
        )
        events = (await session.scalars(stmt)).all()

    for event in events:
        typer.echo(
            f"Event {event.id} ({event.article_count} articles, "
            f"first={event.first_seen_at}, last={event.last_seen_at})"
        )
        for member in sorted(event.members, key=lambda item: item.id):
            title = member.article.title or "<untitled>"
            typer.echo(f"  {title} ({member.article.url})")


@app.command("list")
def list_events(since: str = SINCE_OPTION) -> None:
    """Run the async event listing command inside a synchronous Typer wrapper."""

    asyncio.run(list_events_command(since=since))
