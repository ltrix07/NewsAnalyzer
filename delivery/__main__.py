"""CLI entrypoint for outbound digest delivery."""

from __future__ import annotations

import asyncio

import typer
from sqlalchemy import select

from delivery.dispatcher import deliver_pending, send_test_message
from engine.config import get_settings
from engine.db import session_scope
from engine.models import Digest
from engine.observability import configure_logging

app = typer.Typer(help="CLI entrypoint for Telegram digest delivery.")


async def _list_pending(limit: int | None) -> None:
    statement = (
        select(Digest).where(Digest.delivered_at.is_(None)).order_by(Digest.created_at.asc())
    )
    if limit is not None:
        statement = statement.limit(limit)

    async with session_scope() as session:
        digests = (await session.scalars(statement)).all()

    for digest in digests:
        typer.echo(f"[{digest.created_at.isoformat()}] {digest.headline}")


@app.command("send")
def send_command(limit: int | None = typer.Option(default=None, min=1)) -> None:
    """Send pending digests to Telegram."""

    report = asyncio.run(deliver_pending(limit=limit))
    typer.echo(f"sent={report.sent} failed={report.failed} skipped={report.skipped}")


@app.command("test")
def test_command() -> None:
    """Send a connectivity probe message without touching any digest rows."""

    asyncio.run(send_test_message())
    typer.echo("test message sent")


@app.command("list")
def list_command(limit: int | None = typer.Option(default=None, min=1)) -> None:
    """Print pending digest headlines without sending them."""

    asyncio.run(_list_pending(limit))


def main() -> None:
    """Configure process-wide services and dispatch the CLI."""

    configure_logging(get_settings())
    app()


if __name__ == "__main__":
    main()
