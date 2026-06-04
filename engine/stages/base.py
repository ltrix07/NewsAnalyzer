"""Stage abstractions for pipeline processing and decision logging.

Unexpected exceptions raised by `process()` propagate without writing a
decision row. Only stage-emitted outcomes are recorded here; surrounding
pipeline code is responsible for logging crashes and other observability.
Subclasses override `process_batch()` only when there is a real batch
economy, such as one remote API call amortized across many inputs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import Settings
from engine.observability import record_decision

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


class DecisionDraft(BaseModel):
    """Draft decision payload emitted by a stage before persistence."""

    target_type: Literal["article", "event", "digest", "source"]
    target_id: int
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    decision_json: dict[str, Any]


@dataclass(slots=True)
class Context:
    """Execution context shared across stage runs."""

    run_id: UUID
    session: AsyncSession
    settings: Settings


@dataclass(frozen=True, slots=True)
class StageResult(Generic[TOut]):
    """Outcome of processing one item through one stage."""

    output: TOut | None
    draft: DecisionDraft
    cost_usd: float = 0.0


class Stage(ABC, Generic[TIn, TOut]):
    """Generic stage abstraction with decision logging around `process()`."""

    name: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    async def process(self, item: TIn, ctx: Context) -> StageResult[TOut]:
        """Process one input item and produce a typed stage result."""

    async def process_batch(self, items: list[TIn], ctx: Context) -> list[StageResult[TOut]]:
        """Process items in order, delegating to `process()` by default."""

        results: list[StageResult[TOut]] = []
        for item in items:
            results.append(await self.process(item, ctx))
        return results

    async def run(self, item: TIn, ctx: Context) -> StageResult[TOut]:
        """Run the stage and persist its emitted decision draft."""

        result = await self.process(item, ctx)
        await record_decision(
            ctx.session,
            run_id=ctx.run_id,
            stage_name=self.name,
            stage_version=self.version,
            draft=result.draft,
        )
        return result

    async def run_batch(self, items: list[TIn], ctx: Context) -> list[StageResult[TOut]]:
        """Run the stage for multiple items and persist each emitted decision."""

        results = await self.process_batch(items, ctx)
        for result in results:
            await record_decision(
                ctx.session,
                run_id=ctx.run_id,
                stage_name=self.name,
                stage_version=self.version,
                draft=result.draft,
            )
        return results
