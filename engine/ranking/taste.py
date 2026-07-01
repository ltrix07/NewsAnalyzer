"""Feedback-driven taste vector for re-ranking digests at delivery.

A Rocchio taste vector is the mean of liked event centroids minus the mean of
disliked event centroids, in cosine space. The cosine of a candidate digest's
event centroid against it scores topic affinity to the user's likes.

This is a SOFT signal, intentionally never a hard filter: the probe
(``audit/taste_probe.py``) showed that major strike events the user *liked*
embed identically to routine strikes he disliked, so topic alone cannot tell
them apart. Ranking therefore blends taste with a significance term and keeps a
"major" tier floor so confident / multi-source events are never demoted below
routine ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engine.models import Event

_CONFIDENCE_VALUE = {"low": 0.0, "medium": 0.5, "high": 1.0}
_MULTI_SOURCE_SIGNIFICANCE = 0.6


@dataclass(frozen=True, slots=True)
class TasteVector:
    """Unit-norm taste direction plus the label counts it was built from."""

    vector: np.ndarray
    n_like: int
    n_dislike: int


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


async def build_taste_vector(
    session: AsyncSession,
    *,
    min_labels_per_class: int,
) -> TasteVector | None:
    """Build the Rocchio taste vector from the latest feedback per digest.

    Returns ``None`` when either class has fewer than ``min_labels_per_class``
    examples — i.e. there is not yet enough signal to steer ranking, so the
    caller should fall back to a taste-neutral order.
    """

    rows = (
        await session.execute(
            text(
                """
                WITH ranked AS (
                  SELECT df.*, row_number() OVER (
                           PARTITION BY digest_id, chat_id
                           ORDER BY created_at DESC, id DESC) AS rn
                  FROM digest_feedback df)
                SELECT d.event_id, r.feedback
                FROM ranked r JOIN digests d ON d.id = r.digest_id
                WHERE r.rn = 1
                  AND (r.feedback = 'like'
                       OR (r.feedback = 'dislike'
                           AND r.reason IS DISTINCT FROM 'weak_analysis'))
                """
            )
        )
    ).all()
    if not rows:
        return None

    labels: list[tuple[int, str]] = [(event_id, feedback) for event_id, feedback in rows]
    event_ids = {event_id for event_id, _ in labels}
    centroid_rows = (
        await session.execute(select(Event.id, Event.centroid).where(Event.id.in_(event_ids)))
    ).all()
    centroids: dict[int, list[float]] = {row[0]: row[1] for row in centroid_rows}

    positives: list[np.ndarray] = []
    negatives: list[np.ndarray] = []
    for event_id, feedback in labels:
        centroid = centroids.get(event_id)
        if centroid is None:
            continue
        unit = _unit(np.asarray(centroid, dtype=float))
        (positives if feedback == "like" else negatives).append(unit)

    if len(positives) < min_labels_per_class or len(negatives) < min_labels_per_class:
        return None

    vector = _unit(np.mean(positives, axis=0) - np.mean(negatives, axis=0))
    return TasteVector(vector=vector, n_like=len(positives), n_dislike=len(negatives))


def taste_cosine(centroid: object, taste: TasteVector) -> float:
    """Cosine of an event centroid against the taste direction, in [-1, 1]."""

    return float(np.dot(_unit(np.asarray(centroid, dtype=float)), taste.vector))


def significance_score(confidence_level: str, article_count: int) -> float:
    """Significance in [0, 1] from verify confidence and multi-source clustering."""

    base = _CONFIDENCE_VALUE.get(confidence_level.lower(), 0.0)
    multi = _MULTI_SOURCE_SIGNIFICANCE if article_count >= 2 else 0.0
    return max(base, multi)


def is_major(confidence_level: str, article_count: int) -> bool:
    """Whether an event is important enough to bypass any taste demotion."""

    return confidence_level.lower() == "high" or article_count >= 2


def blend_score(
    taste_cos: float | None,
    significance: float,
    *,
    taste_weight: float,
    significance_weight: float,
) -> float:
    """Blend topic taste (rescaled to [0, 1]) with significance.

    ``taste_cos`` is ``None`` when no taste vector exists yet (cold start); it
    then contributes a neutral 0.5 so ranking falls back to significance order.
    """

    taste_unit = (taste_cos + 1.0) / 2.0 if taste_cos is not None else 0.5
    return taste_weight * taste_unit + significance_weight * significance
