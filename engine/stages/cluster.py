"""Cluster embedded articles into events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from engine.domain import Article as ArticleDTO
from engine.domain import Event as EventDTO
from engine.models import Embedding as EmbeddingModel
from engine.models import Event as EventModel
from engine.models import EventMember as EventMemberModel
from engine.stages.base import Context, DecisionDraft, Stage, StageResult


class ClusterStage(Stage[ArticleDTO, EventDTO]):
    """Assign one embedded article to the nearest recent event or create a new event."""

    name = "cluster"
    version = "v1"

    def __init__(self, similarity_threshold: float, window_hours: int) -> None:
        self.similarity_threshold = similarity_threshold
        self.window_hours = window_hours

    async def process(self, item: ArticleDTO, ctx: Context) -> StageResult[EventDTO]:
        """Cluster one article into an existing event or create a new event."""

        existing_member = await ctx.session.scalar(
            select(EventMemberModel).where(EventMemberModel.article_id == item.id)
        )
        if existing_member is not None:
            return StageResult(
                output=None,
                draft=DecisionDraft(
                    target_type="article",
                    target_id=item.id,
                    decision_json={
                        "action": "already_clustered",
                        "event_id": existing_member.event_id,
                    },
                ),
            )

        embedding = await ctx.session.scalar(
            select(EmbeddingModel).where(EmbeddingModel.article_id == item.id)
        )
        if embedding is None:
            return StageResult(
                output=None,
                draft=DecisionDraft(
                    target_type="article",
                    target_id=item.id,
                    decision_json={"action": "no_embedding"},
                ),
            )

        effective_time = item.published_at or item.fetched_at
        vector = [float(value) for value in embedding.vector]
        window_cutoff = datetime.now(UTC) - timedelta(hours=self.window_hours)
        distance_expr = EventModel.centroid.cosine_distance(vector).label("distance")
        nearest_row = (
            await ctx.session.execute(
                select(EventModel, distance_expr)
                .where(EventModel.last_seen_at >= window_cutoff)
                .order_by(distance_expr)
                .limit(1)
            )
        ).first()

        if nearest_row is not None:
            nearest_event, distance = nearest_row
            similarity = 1.0 - float(distance)
            if similarity >= self.similarity_threshold:
                current_count = nearest_event.article_count
                current_centroid = [float(value) for value in nearest_event.centroid]
                nearest_event.centroid = [
                    ((current_centroid[index] * current_count) + vector[index])
                    / (current_count + 1)
                    for index in range(len(vector))
                ]
                nearest_event.article_count = current_count + 1
                nearest_event.first_seen_at = min(nearest_event.first_seen_at, effective_time)
                nearest_event.last_seen_at = max(nearest_event.last_seen_at, effective_time)
                ctx.session.add(
                    EventMemberModel(
                        event_id=nearest_event.id,
                        article_id=item.id,
                        similarity_to_centroid=similarity,
                    )
                )
                await ctx.session.flush()
                return StageResult(
                    output=EventDTO.model_validate(nearest_event),
                    draft=DecisionDraft(
                        target_type="article",
                        target_id=item.id,
                        decision_json={
                            "action": "joined_event",
                            "event_id": nearest_event.id,
                            "similarity": round(similarity, 4),
                        },
                    ),
                )

        new_event = EventModel(
            centroid=vector,
            article_count=1,
            first_seen_at=effective_time,
            last_seen_at=effective_time,
            status="open",
        )
        ctx.session.add(new_event)
        await ctx.session.flush()
        ctx.session.add(
            EventMemberModel(
                event_id=new_event.id,
                article_id=item.id,
                similarity_to_centroid=1.0,
            )
        )
        await ctx.session.flush()
        return StageResult(
            output=EventDTO.model_validate(new_event),
            draft=DecisionDraft(
                target_type="article",
                target_id=item.id,
                decision_json={"action": "created_event", "event_id": new_event.id},
            ),
        )
