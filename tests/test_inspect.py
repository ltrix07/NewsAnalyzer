"""DB-backed tests for inspect CLI commands."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import typer
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from engine.cli import inspect as inspect_cli
from engine.models import Article, Decision, Digest, Event, EventMember, Source


async def _seed_event_with_digest(session: AsyncSession) -> tuple[Event, str]:
    created_at = datetime.now(UTC) - timedelta(hours=1)
    source_one = Source(name="bankier_rss", kind="rss")
    source_two = Source(name="reuters_rss", kind="rss")
    session.add_all([source_one, source_two])
    await session.flush()

    article_one = Article(
        source_id=source_one.id,
        url="https://example.com/bankier",
        url_hash=b"art00001",
        content_hash=b"cnt00001",
        title="Bankier article",
        raw_text="Bankier excerpt text.",
        published_at=created_at,
        fetched_at=created_at,
    )
    article_two = Article(
        source_id=source_two.id,
        url="https://example.com/reuters",
        url_hash=b"art00002",
        content_hash=b"cnt00002",
        title="Reuters article",
        raw_text="Reuters excerpt text.",
        published_at=created_at + timedelta(minutes=1),
        fetched_at=created_at + timedelta(minutes=1),
    )
    event = Event(
        created_at=created_at,
        first_seen_at=created_at,
        last_seen_at=created_at + timedelta(minutes=2),
        centroid=[0.0] * 1536,
        article_count=2,
        status="open",
    )
    session.add_all([article_one, article_two, event])
    await session.flush()

    session.add_all(
        [
            EventMember(event_id=event.id, article_id=article_one.id, similarity_to_centroid=0.9),
            EventMember(event_id=event.id, article_id=article_two.id, similarity_to_centroid=0.8),
        ]
    )
    await session.flush()

    digest = Digest(
        event_id=event.id,
        profile_name="volodymyr",
        headline="Резюме события",
        summary="Полный текст дайджеста на русском языке.",
        why_it_matters="Это важно для пользователя.",
        confidence_level="high",
        caveats=["Первое замечание", "Второе замечание"],
        citations=[
            {
                "source": "bankier_rss",
                "title": "Bankier article",
                "url": "https://example.com/bankier",
            }
        ],
        stage_version="v1",
        created_at=created_at + timedelta(minutes=6),
    )
    session.add(digest)
    await session.flush()

    run_id = uuid4()
    session.add_all(
        [
            Decision(
                run_id=run_id,
                stage_name="ingest",
                stage_version="v1",
                target_type="article",
                target_id=article_one.id,
                cost_usd=Decimal("0"),
                decision_json={"action": "inserted"},
                created_at=created_at + timedelta(seconds=1),
            ),
            Decision(
                run_id=run_id,
                stage_name="ingest",
                stage_version="v1",
                target_type="article",
                target_id=article_two.id,
                cost_usd=Decimal("0"),
                decision_json={"action": "inserted"},
                created_at=created_at + timedelta(seconds=2),
            ),
            Decision(
                run_id=run_id,
                stage_name="embed",
                stage_version="v1",
                target_type="article",
                target_id=article_one.id,
                input_tokens=105,
                output_tokens=0,
                cost_usd=Decimal("0.000002"),
                decision_json={"action": "embedded"},
                created_at=created_at + timedelta(seconds=5),
            ),
            Decision(
                run_id=run_id,
                stage_name="embed",
                stage_version="v1",
                target_type="article",
                target_id=article_two.id,
                input_tokens=120,
                output_tokens=0,
                cost_usd=Decimal("0.000002"),
                decision_json={"action": "embedded"},
                created_at=created_at + timedelta(seconds=6),
            ),
            Decision(
                run_id=run_id,
                stage_name="cluster",
                stage_version="v1",
                target_type="article",
                target_id=article_one.id,
                cost_usd=Decimal("0"),
                decision_json={"action": "created_event"},
                created_at=created_at + timedelta(seconds=8),
            ),
            Decision(
                run_id=run_id,
                stage_name="cluster",
                stage_version="v1",
                target_type="article",
                target_id=article_two.id,
                cost_usd=Decimal("0"),
                decision_json={"action": "joined_event"},
                created_at=created_at + timedelta(seconds=9),
            ),
            Decision(
                run_id=run_id,
                stage_name="keyword_filter",
                stage_version="v1",
                target_type="event",
                target_id=event.id,
                cost_usd=Decimal("0"),
                decision_json={"action": "passed_keyword_filter"},
                created_at=created_at + timedelta(seconds=10),
            ),
            Decision(
                run_id=run_id,
                stage_name="relevance",
                stage_version="v1",
                target_type="event",
                target_id=event.id,
                model="gpt-4o-mini",
                input_tokens=432,
                output_tokens=24,
                cost_usd=Decimal("0.000178"),
                decision_json={
                    "action": "relevant",
                    "verdict": {
                        "relevant": True,
                        "categories": ["Geopolitics affecting the UA-PL corridor"],
                        "why": "Important geopolitical signal.",
                        "confidence": 0.9,
                    },
                },
                created_at=created_at + timedelta(seconds=20),
            ),
            Decision(
                run_id=run_id,
                stage_name="verify",
                stage_version="v1",
                target_type="event",
                target_id=event.id,
                model="gpt-4o",
                input_tokens=1452,
                output_tokens=158,
                cost_usd=Decimal("0.005210"),
                decision_json={
                    "action": "verified",
                    "report": {
                        "speaker_type": "official",
                        "sources_count": 2,
                        "hype_score": 0.3,
                        "notes": "Verification notes text.",
                    },
                },
                created_at=created_at + timedelta(seconds=30),
            ),
            Decision(
                run_id=run_id,
                stage_name="summarize",
                stage_version="v1",
                target_type="digest",
                target_id=digest.id,
                model="gpt-4o",
                input_tokens=1192,
                output_tokens=288,
                cost_usd=Decimal("0.005850"),
                decision_json={
                    "action": "digested",
                    "event_id": event.id,
                    "confidence_level": "high",
                    "headline": digest.headline,
                },
                created_at=created_at + timedelta(seconds=40),
            ),
        ]
    )
    await session.flush()
    return event, str(run_id)


@pytest.mark.asyncio
async def test_inspect_event_prints_full_timeline(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event, run_id = await _seed_event_with_digest(db_session)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(inspect_cli, "session_scope", fake_session_scope)

    await inspect_cli.inspect_event_command(event.id)
    event_output = capsys.readouterr().out

    assert "https://example.com/bankier" in event_output
    assert "https://example.com/reuters" in event_output
    for stage_name in (
        "ingest",
        "embed",
        "cluster",
        "keyword_filter",
        "relevance",
        "verify",
        "summarize",
    ):
        assert stage_name in event_output
    assert "Резюме события" in event_output
    assert "TOTAL COST FOR EVENT:" in event_output

    await inspect_cli.inspect_run_command(run_id)
    run_output = capsys.readouterr().out

    assert run_id in run_output
    assert "score" in run_output
    assert "summarize" in run_output


def test_inspect_event_missing_id_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()

    async def fake_inspect_event_command(event_id: int) -> None:
        raise typer.BadParameter(f"Event {event_id} was not found.", param_hint="event_id")

    monkeypatch.setattr(inspect_cli, "inspect_event_command", fake_inspect_event_command)
    result = runner.invoke(inspect_cli.app, ["event", "999999"])

    assert result.exit_code != 0
    assert "Event 999999 was not found." in result.output
