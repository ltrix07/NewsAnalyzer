"""DB-backed tests for cost rollup CLI output."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import cost as cost_cli
from engine.models import Decision


async def _seed_cost_decisions(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    session.add_all(
        [
            Decision(
                run_id=uuid4(),
                stage_name="relevance",
                stage_version="v1",
                target_type="event",
                target_id=1,
                model="gpt-4o-mini",
                input_tokens=100,
                output_tokens=20,
                cost_usd=Decimal("0.001000"),
                decision_json={"action": "relevant"},
                created_at=now - timedelta(days=2),
            ),
            Decision(
                run_id=uuid4(),
                stage_name="verify",
                stage_version="v1",
                target_type="event",
                target_id=2,
                model="gpt-4o",
                input_tokens=200,
                output_tokens=40,
                cost_usd=Decimal("0.003000"),
                decision_json={"action": "verified"},
                created_at=now - timedelta(days=1),
            ),
            Decision(
                run_id=uuid4(),
                stage_name="ingest",
                stage_version="v1",
                target_type="article",
                target_id=3,
                model=None,
                input_tokens=None,
                output_tokens=None,
                cost_usd=Decimal("0"),
                decision_json={"action": "inserted"},
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    await session.flush()


@pytest.mark.asyncio
async def test_cost_by_stage_orders_by_cost_desc(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_cost_decisions(db_session)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(cost_cli, "session_scope", fake_session_scope)

    await cost_cli.cost_command(since="30d", by="stage")
    output = capsys.readouterr().out

    assert "verify" in output
    assert "score" in output
    assert output.index("verify") < output.index("score")
    assert "Total: $0.004000" in output


@pytest.mark.asyncio
async def test_cost_by_day_orders_by_day_asc(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_cost_decisions(db_session)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(cost_cli, "session_scope", fake_session_scope)

    await cost_cli.cost_command(since="30d", by="day")
    output = capsys.readouterr().out
    now = datetime.now(UTC)
    older_day = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    newer_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    assert older_day in output
    assert newer_day in output
    assert output.index(older_day) < output.index(newer_day)


@pytest.mark.asyncio
async def test_cost_by_model_filters_nulls_and_orders_by_cost(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_cost_decisions(db_session)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(cost_cli, "session_scope", fake_session_scope)

    await cost_cli.cost_command(since="30d", by="model")
    output = capsys.readouterr().out

    assert "gpt-4o" in output
    assert "gpt-4o-mini" in output
    assert "None" not in output
    assert output.index("gpt-4o") < output.index("gpt-4o-mini")
