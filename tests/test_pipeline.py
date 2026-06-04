"""Tests for the end-to-end pipeline orchestrator."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine import pipeline
from engine.models import Decision


def _fixed_run_id() -> UUID:
    return UUID("11111111-1111-1111-1111-111111111111")


def _patch_stage_commands(
    monkeypatch: pytest.MonkeyPatch,
    stage_impls: dict[str, Callable[..., Awaitable[None]]],
) -> None:
    monkeypatch.setattr(pipeline, "fetch_command", stage_impls["fetch"])
    monkeypatch.setattr(pipeline, "ingest_command", stage_impls["ingest"])
    monkeypatch.setattr(pipeline, "embed_command", stage_impls["embed"])
    monkeypatch.setattr(pipeline, "cluster_command", stage_impls["cluster"])
    monkeypatch.setattr(pipeline, "filter_command", stage_impls["filter"])
    monkeypatch.setattr(pipeline, "score_command", stage_impls["score"])
    monkeypatch.setattr(pipeline, "verify_command", stage_impls["verify"])
    monkeypatch.setattr(pipeline, "summarize_command", stage_impls["summarize"])


@pytest.mark.asyncio
async def test_run_once_calls_all_stages_in_order_with_shared_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_calls: list[tuple[str, UUID]] = []

    def make_stage(stage_name: str) -> Callable[..., Awaitable[None]]:
        async def stage(**kwargs: object) -> None:
            run_id = kwargs.get("run_id")
            assert isinstance(run_id, UUID)
            recorded_calls.append((stage_name, run_id))

        return stage

    _patch_stage_commands(
        monkeypatch,
        {stage_name: make_stage(stage_name) for stage_name in pipeline.STAGE_ORDER},
    )
    monkeypatch.setattr(pipeline, "uuid4", _fixed_run_id)

    async def empty_decisions(run_id: UUID) -> list[Decision]:
        del run_id
        return []

    monkeypatch.setattr(pipeline, "_load_decisions_by_run_id", empty_decisions)

    summary = await pipeline.run_once()

    assert summary.run_id == _fixed_run_id()
    assert [stage_name for stage_name, _ in recorded_calls] == list(pipeline.STAGE_ORDER)
    assert {run_id for _, run_id in recorded_calls} == {_fixed_run_id()}


@pytest.mark.asyncio
async def test_run_once_skips_requested_stages_and_keeps_shared_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_calls: list[tuple[str, UUID]] = []

    def make_stage(stage_name: str) -> Callable[..., Awaitable[None]]:
        async def stage(**kwargs: object) -> None:
            run_id = kwargs.get("run_id")
            assert isinstance(run_id, UUID)
            recorded_calls.append((stage_name, run_id))

        return stage

    _patch_stage_commands(
        monkeypatch,
        {stage_name: make_stage(stage_name) for stage_name in pipeline.STAGE_ORDER},
    )
    monkeypatch.setattr(pipeline, "uuid4", _fixed_run_id)

    async def empty_decisions(run_id: UUID) -> list[Decision]:
        del run_id
        return []

    monkeypatch.setattr(pipeline, "_load_decisions_by_run_id", empty_decisions)

    summary = await pipeline.run_once(skip={"embed", "cluster"})

    assert [stage_name for stage_name, _ in recorded_calls] == [
        "fetch",
        "ingest",
        "filter",
        "score",
        "verify",
        "summarize",
    ]
    assert summary.stages["embed"].status == "skipped"
    assert summary.stages["cluster"].status == "skipped"
    assert {run_id for _, run_id in recorded_calls} == {_fixed_run_id()}


@pytest.mark.asyncio
async def test_run_once_continues_or_stops_after_stage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_stage(**kwargs: object) -> None:
        del kwargs

    async def exploding_stage(**kwargs: object) -> None:
        del kwargs
        raise RuntimeError("boom")

    stage_impls = {stage_name: noop_stage for stage_name in pipeline.STAGE_ORDER}
    stage_impls["verify"] = exploding_stage

    continued_calls: list[str] = []

    def wrap(
        stage_name: str,
        func: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        async def stage(**kwargs: object) -> None:
            continued_calls.append(stage_name)
            await func(**kwargs)

        return stage

    _patch_stage_commands(
        monkeypatch,
        {stage_name: wrap(stage_name, func) for stage_name, func in stage_impls.items()},
    )
    monkeypatch.setattr(pipeline, "uuid4", _fixed_run_id)

    async def empty_decisions(run_id: UUID) -> list[Decision]:
        del run_id
        return []

    monkeypatch.setattr(pipeline, "_load_decisions_by_run_id", empty_decisions)

    continue_summary = await pipeline.run_once()
    assert continue_summary.stages["verify"].status == "error"
    assert "boom" in (continue_summary.stages["verify"].error or "")
    assert "summarize" in continued_calls

    stopped_calls: list[str] = []

    def wrap_stopped(
        stage_name: str,
        func: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        async def stage(**kwargs: object) -> None:
            stopped_calls.append(stage_name)
            await func(**kwargs)

        return stage

    _patch_stage_commands(
        monkeypatch,
        {stage_name: wrap_stopped(stage_name, func) for stage_name, func in stage_impls.items()},
    )
    stop_summary = await pipeline.run_once(stop_on_error=True)

    assert stop_summary.stages["verify"].status == "error"
    assert stop_summary.stages["summarize"].status == "skipped"
    assert "summarize" not in stopped_calls


@pytest.mark.asyncio
async def test_run_once_rolls_up_decisions_from_db(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_stage(**kwargs: object) -> None:
        del kwargs

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    _patch_stage_commands(
        monkeypatch,
        {stage_name: noop_stage for stage_name in pipeline.STAGE_ORDER},
    )
    monkeypatch.setattr(pipeline, "uuid4", _fixed_run_id)
    monkeypatch.setattr(pipeline, "session_scope", fake_session_scope)

    db_session.add_all(
        [
            Decision(
                run_id=_fixed_run_id(),
                stage_name="ingest",
                stage_version="v1",
                target_type="article",
                target_id=1,
                model=None,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                decision_json={"action": "inserted"},
            ),
            Decision(
                run_id=_fixed_run_id(),
                stage_name="ingest",
                stage_version="v1",
                target_type="article",
                target_id=2,
                model=None,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                decision_json={"action": "deduped_url"},
            ),
            Decision(
                run_id=_fixed_run_id(),
                stage_name="ingest",
                stage_version="v1",
                target_type="article",
                target_id=3,
                model="gpt-4o-mini",
                input_tokens=10,
                output_tokens=4,
                cost_usd=Decimal("0.000321"),
                decision_json={"action": "inserted"},
            ),
            Decision(
                run_id=_fixed_run_id(),
                stage_name="relevance",
                stage_version="v1",
                target_type="event",
                target_id=10,
                model="gpt-4o-mini",
                input_tokens=25,
                output_tokens=7,
                cost_usd=Decimal("0.000456"),
                decision_json={"action": "relevant"},
            ),
        ]
    )
    await db_session.flush()

    summary = await pipeline.run_once()

    assert summary.stages["ingest"].action_counts == {"inserted": 2, "deduped_url": 1}
    assert summary.stages["ingest"].total_input_tokens == 10
    assert summary.stages["ingest"].total_output_tokens == 4
    assert summary.stages["ingest"].total_cost_usd == Decimal("0.000321")
    assert summary.stages["score"].action_counts == {"relevant": 1}
    assert summary.stages["score"].total_input_tokens == 25
    assert summary.stages["score"].total_output_tokens == 7
    assert summary.stages["score"].total_cost_usd == Decimal("0.000456")
