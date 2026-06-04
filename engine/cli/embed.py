"""CLI command for generating and persisting article embeddings."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from math import ceil
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import typer
from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Article as ArticleDTO
from engine.llm.embeddings import make_default_embedder
from engine.models import Article as ArticleModel
from engine.models import Source as SourceModel
from engine.stages.base import Context, StageResult
from engine.stages.embed import EmbedStage

LIMIT_OPTION = typer.Option(default=None, min=1, help="Process at most this many articles.")
BATCH_SIZE_OPTION = typer.Option(default=100, min=1, help="Embed this many articles per batch.")
SOURCE_OPTION = typer.Option(default=None, help="Restrict embedding to one source name.")


def _chunked(items: list[ArticleDTO], batch_size: int) -> list[list[ArticleDTO]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def _result_action_counts(results: Sequence[StageResult[Any]]) -> Counter[str]:
    return Counter(str(result.draft.decision_json["action"]) for result in results)


def _result_tokens(results: Sequence[StageResult[Any]]) -> int:
    return sum(result.draft.input_tokens or 0 for result in results)


def _result_cost(results: Sequence[StageResult[Any]]) -> Decimal:
    return sum(
        (result.draft.cost_usd or Decimal("0") for result in results),
        Decimal("0"),
    )


async def embed_command(
    limit: int | None = None,
    batch_size: int = 100,
    source: str | None = None,
    run_id: UUID | None = None,
) -> None:
    """Generate embeddings for stored articles and log per-article decisions."""

    settings = get_settings()
    stage = EmbedStage(make_default_embedder(settings))
    resolved_run_id = run_id or uuid4()
    started_at = perf_counter()
    total_action_counts: Counter[str] = Counter()
    total_tokens = 0
    total_cost = Decimal("0")

    async with session_scope() as session:
        stmt = select(ArticleModel).join(SourceModel).order_by(ArticleModel.id)
        if source is not None:
            stmt = stmt.where(SourceModel.name == source)
        if limit is not None:
            stmt = stmt.limit(limit)

        articles = [
            ArticleDTO.model_validate(article) for article in (await session.scalars(stmt)).all()
        ]
        batches = _chunked(articles, batch_size)
        ctx = Context(run_id=resolved_run_id, session=session, settings=settings)

        for batch_index, batch in enumerate(batches, start=1):
            results = await stage.run_batch(batch, ctx)
            batch_action_counts = _result_action_counts(results)
            batch_tokens = _result_tokens(results)
            batch_cost = _result_cost(results)
            total_action_counts.update(batch_action_counts)
            total_tokens += batch_tokens
            total_cost += batch_cost

            typer.echo(
                f"Batch {batch_index}/{len(batches)}: {len(batch)} articles "
                f"({batch_action_counts.get('embedded', 0)} embedded, "
                f"{batch_action_counts.get('already_embedded', 0)} already_embedded), "
                f"tokens {batch_tokens}, cost ${batch_cost:.6f}"
            )

    elapsed = perf_counter() - started_at
    total_batches = ceil(len(articles) / batch_size) if articles else 0
    typer.echo(
        f"Run {resolved_run_id}: {dict(total_action_counts)}; batches={total_batches}; "
        f"tokens={total_tokens}; cost=${total_cost:.6f}; elapsed={elapsed:.1f}s"
    )


def embed_command_sync_wrapper(
    limit: int | None = LIMIT_OPTION,
    batch_size: int = BATCH_SIZE_OPTION,
    source: str | None = SOURCE_OPTION,
) -> None:
    """Run the async embed command inside a synchronous Typer wrapper."""

    asyncio.run(embed_command(limit=limit, batch_size=batch_size, source=source))
