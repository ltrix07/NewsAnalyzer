"""CLI command for merging fresh event fragments into canonical stories."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import UUID, uuid4

import typer
from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engine.config import get_settings
from engine.consolidation_match import Adjudicator, PairJudgement, default_adjudicator
from engine.db import session_scope
from engine.models import Decision, Digest, Event, EventMember
from engine.observability import record_decision
from engine.stages.base import DecisionDraft

STAGE_NAME = "consolidate"
STAGE_VERSION = "v2"

WINDOW_HOURS_OPTION = typer.Option(
    default=None,
    min=1,
    help="Override the consolidation recency window in hours.",
)
MIN_SIMILARITY_OPTION = typer.Option(
    default=None,
    min=0.0,
    max=1.0,
    help="Override the minimum event similarity judged for consolidation.",
)
MAX_NEIGHBORS_OPTION = typer.Option(
    default=None,
    min=1,
    help="Override the maximum nearest neighbors judged per event.",
)


class _UnionFind:
    """Small union-find for transitive same-story edges."""

    def __init__(self, event_ids: list[int]) -> None:
        self._parent = {event_id: event_id for event_id in event_ids}

    def find(self, event_id: int) -> int:
        parent = self._parent[event_id]
        if parent != event_id:
            parent = self.find(parent)
            self._parent[event_id] = parent
        return parent

    def union(self, left_id: int, right_id: int) -> None:
        left_root = self.find(left_id)
        right_root = self.find(right_id)
        if left_root != right_root:
            self._parent[right_root] = left_root

    def groups(self) -> list[list[int]]:
        grouped: dict[int, list[int]] = {}
        for event_id in self._parent:
            grouped.setdefault(self.find(event_id), []).append(event_id)
        return [sorted(group) for group in grouped.values() if len(group) > 1]


async def _load_candidates(session: AsyncSession, *, window_hours: int) -> list[Event]:
    """Load fresh events that are still upstream of keyword filtering and delivery."""

    window_cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = await session.scalars(
        select(Event)
        .where(
            Event.created_at >= window_cutoff,
            ~exists(
                select(1).where(
                    Decision.stage_name == "keyword_filter",
                    Decision.target_type == "event",
                    Decision.target_id == Event.id,
                )
            ),
            ~exists(select(1).where(Digest.event_id == Event.id)),
        )
        .order_by(Event.id)
    )
    return list(rows)


async def _candidate_pairs(
    session: AsyncSession,
    candidates: list[Event],
    *,
    min_similarity: float,
    max_neighbors: int,
) -> list[tuple[int, int]]:
    """Find unordered candidate event pairs by centroid proximity."""

    candidate_by_id = {event.id: event for event in candidates}
    candidate_ids = set(candidate_by_id)
    pairs: set[tuple[int, int]] = set()

    for event in candidates:
        vector = [float(value) for value in event.centroid]
        distance_expr = Event.centroid.cosine_distance(vector).label("distance")
        rows = (
            await session.execute(
                select(Event.id, distance_expr)
                .where(Event.id.in_(candidate_ids - {event.id}))
                .order_by(distance_expr)
                .limit(max_neighbors)
            )
        ).all()

        for other_id, distance in rows:
            similarity = 1.0 - float(distance)
            if similarity >= min_similarity:
                resolved_other_id = int(other_id)
                pairs.add((min(event.id, resolved_other_id), max(event.id, resolved_other_id)))

    return sorted(pairs)


def _canonical_event(events: list[Event]) -> Event:
    return sorted(events, key=lambda event: (-event.article_count, event.id))[0]


def _weighted_centroid(left: Event, right: Event) -> list[float]:
    left_weight = left.article_count
    right_weight = right.article_count
    total = left_weight + right_weight
    return [
        (
            (float(left.centroid[index]) * left_weight)
            + (float(right.centroid[index]) * right_weight)
        )
        / total
        for index in range(len(left.centroid))
    ]


async def _merge_group(
    session: AsyncSession,
    *,
    run_id: UUID,
    group_events: list[Event],
    judgements: dict[tuple[int, int], PairJudgement],
) -> tuple[int, int, Decimal]:
    """Merge a same-story group into its canonical event and record one decision."""

    canonical = _canonical_event(group_events)
    absorbed = [
        event for event in sorted(group_events, key=lambda event: event.id) if event != canonical
    ]

    original_ids = {event.id for event in group_events}
    group_judgements = [
        judgement
        for pair, judgement in judgements.items()
        if judgement.same_event and pair[0] in original_ids and pair[1] in original_ids
    ]
    input_tokens = sum(judgement.input_tokens for judgement in group_judgements)
    output_tokens = sum(judgement.output_tokens for judgement in group_judgements)
    cost_usd = sum((judgement.cost_usd for judgement in group_judgements), Decimal("0"))
    model = next((judgement.model for judgement in group_judgements if judgement.model), None)

    for event in absorbed:
        canonical.centroid = _weighted_centroid(canonical, event)
        canonical.article_count += event.article_count
        canonical.first_seen_at = min(canonical.first_seen_at, event.first_seen_at)
        canonical.last_seen_at = max(canonical.last_seen_at, event.last_seen_at)
        await session.execute(
            update(EventMember)
            .where(EventMember.event_id == event.id)
            .values(event_id=canonical.id)
        )
        await session.execute(delete(Event).where(Event.id == event.id))

    await session.flush()
    await record_decision(
        session,
        run_id=run_id,
        stage_name=STAGE_NAME,
        stage_version=STAGE_VERSION,
        draft=DecisionDraft(
            target_type="event",
            target_id=canonical.id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            decision_json={
                "action": "merged",
                "canonical_event_id": canonical.id,
                "absorbed_event_ids": [event.id for event in absorbed],
                "group_size": len(group_events),
            },
        ),
    )
    return 1, len(absorbed), cost_usd


async def consolidate_command(
    window_hours: int | None = None,
    min_similarity: float | None = None,
    max_neighbors: int | None = None,
    run_id: UUID | None = None,
    adjudicator: Adjudicator | None = None,
) -> None:
    """Merge fresh near-duplicate events before keyword filtering sees them."""

    settings = get_settings()
    resolved_run_id = run_id or uuid4()
    started_at = perf_counter()

    if not settings.consolidate_enabled:
        elapsed = perf_counter() - started_at
        typer.echo(f"Run {resolved_run_id}: merged=0 (disabled); elapsed={elapsed:.1f}s")
        return

    resolved_window_hours = (
        settings.consolidate_window_hours if window_hours is None else window_hours
    )
    resolved_min_similarity = (
        settings.consolidate_candidate_min_similarity if min_similarity is None else min_similarity
    )
    resolved_max_neighbors = (
        settings.consolidate_max_neighbors if max_neighbors is None else max_neighbors
    )
    resolved_adjudicator = adjudicator or default_adjudicator(settings)

    merged_groups = 0
    absorbed_events = 0
    pairs_judged = 0
    total_cost = Decimal("0")

    async with session_scope() as session:
        candidates = await _load_candidates(session, window_hours=resolved_window_hours)
        if len(candidates) >= 2:
            candidate_by_id = {event.id: event for event in candidates}
            pairs = await _candidate_pairs(
                session,
                candidates,
                min_similarity=resolved_min_similarity,
                max_neighbors=resolved_max_neighbors,
            )
            union_find = _UnionFind([event.id for event in candidates])
            judgements: dict[tuple[int, int], PairJudgement] = {}

            for left_id, right_id in pairs:
                judgement = await resolved_adjudicator(
                    session,
                    candidate_by_id[left_id],
                    candidate_by_id[right_id],
                )
                pairs_judged += 1
                judgements[(left_id, right_id)] = judgement
                if judgement.same_event:
                    union_find.union(left_id, right_id)

            for group_ids in union_find.groups():
                group_events = [candidate_by_id[event_id] for event_id in group_ids]
                groups_delta, absorbed_delta, cost_delta = await _merge_group(
                    session,
                    run_id=resolved_run_id,
                    group_events=group_events,
                    judgements=judgements,
                )
                merged_groups += groups_delta
                absorbed_events += absorbed_delta
                total_cost += cost_delta

    elapsed = perf_counter() - started_at
    typer.echo(
        f"Run {resolved_run_id}: merged={merged_groups} absorbed={absorbed_events} "
        f"pairs_judged={pairs_judged}; cost=${total_cost:.6f}; elapsed={elapsed:.1f}s"
    )


def consolidate_command_sync_wrapper(
    window_hours: int | None = WINDOW_HOURS_OPTION,
    min_similarity: float | None = MIN_SIMILARITY_OPTION,
    max_neighbors: int | None = MAX_NEIGHBORS_OPTION,
) -> None:
    """Run the async consolidate command inside a synchronous Typer wrapper."""

    asyncio.run(
        consolidate_command(
            window_hours=window_hours,
            min_similarity=min_similarity,
            max_neighbors=max_neighbors,
        )
    )
