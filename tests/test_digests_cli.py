"""Tests for the digests show CLI output."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import digests as digests_cli
from engine.models import Digest, Event


async def _create_event(session: AsyncSession) -> Event:
    event = Event(
        centroid=[0.0] * 1536,
        article_count=1,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        status="open",
    )
    session.add(event)
    await session.flush()
    return event


async def _create_digest(
    session: AsyncSession,
    *,
    event_id: int,
    headline: str,
    confidence_level: str,
    created_at: datetime,
) -> Digest:
    digest = Digest(
        event_id=event_id,
        profile_name="volodymyr",
        headline=headline,
        summary=f"Summary for {headline}",
        why_it_matters=f"Why {headline}",
        confidence_level=confidence_level,
        caveats=[f"Caveat for {headline}"],
        citations=[
            {
                "source": "source",
                "title": f"Title {headline}",
                "url": f"https://example.com/{headline}",
            }
        ],
        stage_version="v1",
        created_at=created_at,
    )
    session.add(digest)
    await session.flush()
    return digest


@pytest.mark.asyncio
async def test_digests_show_orders_most_recent_first(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_one = await _create_event(db_session)
    event_two = await _create_event(db_session)
    now = datetime.now(UTC)
    await _create_digest(
        db_session,
        event_id=event_one.id,
        headline="Older digest",
        confidence_level="medium",
        created_at=now - timedelta(hours=2),
    )
    await _create_digest(
        db_session,
        event_id=event_two.id,
        headline="Newer digest",
        confidence_level="high",
        created_at=now - timedelta(minutes=10),
    )

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(digests_cli, "session_scope", fake_session_scope)

    await digests_cli.show_digests_command(limit=10, since="24h", min_confidence="low")
    output = capsys.readouterr().out

    assert "Newer digest" in output
    assert "Older digest" in output
    assert output.index("Newer digest") < output.index("Older digest")


@pytest.mark.asyncio
async def test_digests_show_filters_by_min_confidence(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_one = await _create_event(db_session)
    event_two = await _create_event(db_session)
    now = datetime.now(UTC)
    await _create_digest(
        db_session,
        event_id=event_one.id,
        headline="Medium digest",
        confidence_level="medium",
        created_at=now - timedelta(minutes=20),
    )
    await _create_digest(
        db_session,
        event_id=event_two.id,
        headline="High digest",
        confidence_level="high",
        created_at=now - timedelta(minutes=10),
    )

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(digests_cli, "session_scope", fake_session_scope)

    await digests_cli.show_digests_command(limit=10, since="24h", min_confidence="high")
    output = capsys.readouterr().out

    assert "High digest" in output
    assert "Medium digest" not in output
