"""Tests for stage execution and decision persistence."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.models import Decision
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


class DummyStage(Stage[str, str]):
    """Stage that emits a deterministic draft for decision logging tests."""

    name = "dummy"
    version = "v1"

    async def process(self, item: str, ctx: Context) -> StageResult[str]:
        return StageResult(
            output=item.upper(),
            draft=DecisionDraft(
                target_type="article",
                target_id=123,
                model="test-model",
                input_tokens=10,
                output_tokens=5,
                cost_usd=Decimal("0.001000"),
                decision_json={"action": "processed", "item": item},
            ),
            cost_usd=0.001,
        )


class ExplodingStage(Stage[str, str]):
    """Stage whose process method raises before emitting a decision draft."""

    name = "exploding"
    version = "v1"

    async def process(self, item: str, ctx: Context) -> StageResult[str]:
        raise RuntimeError("boom")


class TrackingStage(Stage[str, str]):
    """Stage that records input order while using the default batch path."""

    name = "tracking"
    version = "v1"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def process(self, item: str, ctx: Context) -> StageResult[str]:
        self.seen.append(item)
        return StageResult(
            output=item.upper(),
            draft=DecisionDraft(
                target_type="article",
                target_id=len(self.seen),
                decision_json={"action": "processed", "item": item},
            ),
        )


@pytest.mark.asyncio
async def test_stage_run_writes_one_decision_row(db_session: AsyncSession) -> None:
    """Running a stage should persist exactly one decision row."""

    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())
    stage = DummyStage()

    result = await stage.run("hello", ctx)
    stored = await db_session.scalar(
        select(Decision).where(Decision.stage_name == "dummy", Decision.run_id == ctx.run_id)
    )

    assert result.output == "HELLO"
    assert stored is not None
    assert stored.stage_name == "dummy"
    assert stored.stage_version == "v1"
    assert stored.run_id == ctx.run_id
    assert stored.target_type == "article"
    assert stored.target_id == 123
    assert stored.decision_json == {"action": "processed", "item": "hello"}


@pytest.mark.asyncio
async def test_stage_run_does_not_log_when_process_raises(db_session: AsyncSession) -> None:
    """Unexpected process exceptions should leave the decisions table unchanged."""

    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())
    stage = ExplodingStage()
    before = await db_session.scalar(select(func.count()).select_from(Decision))

    with pytest.raises(RuntimeError, match="boom"):
        await stage.run("hello", ctx)

    after = await db_session.scalar(select(func.count()).select_from(Decision))
    assert after == before


@pytest.mark.asyncio
async def test_default_process_batch_preserves_order(db_session: AsyncSession) -> None:
    """The default batch implementation should delegate item-by-item in order."""

    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())
    stage = TrackingStage()

    results = await stage.process_batch(["first", "second", "third"], ctx)

    assert stage.seen == ["first", "second", "third"]
    assert [result.output for result in results] == ["FIRST", "SECOND", "THIRD"]


@pytest.mark.asyncio
async def test_default_run_batch_writes_one_decision_per_result(db_session: AsyncSession) -> None:
    """Running a batch should persist one decision row for each emitted result."""

    ctx = Context(run_id=uuid4(), session=db_session, settings=get_settings())
    stage = TrackingStage()

    results = await stage.run_batch(["first", "second"], ctx)
    stored = (
        await db_session.scalars(
            select(Decision)
            .where(Decision.stage_name == stage.name, Decision.run_id == ctx.run_id)
            .order_by(Decision.id)
        )
    ).all()

    assert len(results) == 2
    assert len(stored) == 2
    assert [decision.decision_json["item"] for decision in stored] == ["first", "second"]
