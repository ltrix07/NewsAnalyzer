"""CLI command for clustering embedded articles into events."""

from __future__ import annotations

import asyncio
from collections import Counter
from time import perf_counter
from uuid import UUID, uuid4

import typer
from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Article as ArticleDTO
from engine.models import Article as ArticleModel
from engine.models import Embedding as EmbeddingModel
from engine.models import EventMember as EventMemberModel
from engine.stages.base import Context
from engine.stages.cluster import ClusterStage

LIMIT_OPTION = typer.Option(default=None, min=1, help="Process at most this many articles.")
THRESHOLD_OPTION = typer.Option(
    default=None,
    min=0.0,
    max=1.0,
    help="Override the cluster similarity threshold.",
)
WINDOW_HOURS_OPTION = typer.Option(
    default=None,
    min=1,
    help="Override the cluster recency window in hours.",
)


async def cluster_command(
    limit: int | None = None,
    threshold: float | None = None,
    window_hours: int | None = None,
    run_id: UUID | None = None,
) -> None:
    """Cluster embedded articles that have not yet been assigned to an event."""

    settings = get_settings()
    stage = ClusterStage(
        similarity_threshold=(
            settings.cluster_similarity_threshold if threshold is None else threshold
        ),
        window_hours=settings.cluster_window_hours if window_hours is None else window_hours,
    )
    resolved_run_id = run_id or uuid4()
    action_counts: Counter[str] = Counter()
    started_at = perf_counter()

    async with session_scope() as session:
        stmt = (
            select(ArticleModel)
            .join(EmbeddingModel, EmbeddingModel.article_id == ArticleModel.id)
            .outerjoin(EventMemberModel, EventMemberModel.article_id == ArticleModel.id)
            .where(EventMemberModel.id.is_(None))
            .order_by(ArticleModel.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        articles = [
            ArticleDTO.model_validate(article) for article in (await session.scalars(stmt)).all()
        ]
        ctx = Context(run_id=resolved_run_id, session=session, settings=settings)

        for article in articles:
            result = await stage.run(article, ctx)
            action_counts[str(result.draft.decision_json["action"])] += 1

    elapsed = perf_counter() - started_at
    typer.echo(f"Run {resolved_run_id}: {dict(action_counts)}; elapsed={elapsed:.1f}s")


def cluster_command_sync_wrapper(
    limit: int | None = LIMIT_OPTION,
    threshold: float | None = THRESHOLD_OPTION,
    window_hours: int | None = WINDOW_HOURS_OPTION,
) -> None:
    """Run the async cluster command inside a synchronous Typer wrapper."""

    asyncio.run(cluster_command(limit=limit, threshold=threshold, window_hours=window_hours))
