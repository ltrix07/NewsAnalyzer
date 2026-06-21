"""Probe whether a content-based 'taste vector' separates likes from dislikes.

Builds a Rocchio taste vector (mean of liked centroids minus mean of disliked
centroids, in cosine space) and evaluates it honestly with leave-one-out: for
each rated digest the taste vector is rebuilt WITHOUT that example, so the score
is never fit on the point it judges. Reports class separation, ROC-AUC, and a
sorted score table to eyeball.

Run ON THE SERVER (that is where the feedback lives):
    uv run python -m audit.taste_probe
"""

from __future__ import annotations

import asyncio

import numpy as np
from sqlalchemy import select, text

from engine.db import session_scope
from engine.models import Event


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _auc(scores: list[float], labels: list[int]) -> float:
    """Rank-based ROC-AUC = P(score(like) > score(dislike)); 0.5 = no signal."""

    pos = [s for s, y in zip(scores, labels, strict=True) if y == 1]
    neg = [s for s, y in zip(scores, labels, strict=True) if y == 0]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


async def main() -> None:
    async with session_scope() as session:
        # latest feedback per (digest, chat), joined to the event id + headline
        rows = (
            await session.execute(
                text(
                    """
                    WITH ranked AS (
                      SELECT df.*, row_number() OVER (
                               PARTITION BY digest_id, chat_id
                               ORDER BY created_at DESC, id DESC) AS rn
                      FROM digest_feedback df)
                    SELECT r.digest_id, r.feedback, d.event_id, d.headline
                    FROM ranked r JOIN digests d ON d.id = r.digest_id
                    WHERE r.rn = 1
                    ORDER BY r.created_at
                    """
                )
            )
        ).mappings().all()

        if not rows:
            print("No feedback rows — nothing to probe.")
            return

        event_ids = [row["event_id"] for row in rows]
        centroids = dict(
            (
                await session.execute(
                    select(Event.id, Event.centroid).where(Event.id.in_(event_ids))
                )
            ).all()
        )

        # (digest_id, label, unit-centroid, headline)
        samples: list[tuple[int, int, np.ndarray, str]] = []
        for row in rows:
            cen = centroids.get(row["event_id"])
            if cen is None:
                continue
            label = 1 if row["feedback"] == "like" else 0
            samples.append(
                (row["digest_id"], label, _unit(np.asarray(cen, dtype=float)), row["headline"])
            )

        n_pos = sum(1 for _, y, _, _ in samples if y == 1)
        n_neg = sum(1 for _, y, _, _ in samples if y == 0)
        print(f"samples: {len(samples)}  (👍 {n_pos} / 👎 {n_neg})")
        if n_pos < 2 or n_neg < 2:
            print("Need >=2 of each class for leave-one-out. Collect more ratings.")
            return

        # ---- Leave-one-out scoring ----
        loo_scores: list[float] = []
        loo_labels: list[int] = []
        table: list[tuple[float, int, int, str]] = []
        for i, (digest_id, label, x, headline) in enumerate(samples):
            pos = [s[2] for j, s in enumerate(samples) if j != i and s[1] == 1]
            neg = [s[2] for j, s in enumerate(samples) if j != i and s[1] == 0]
            if not pos or not neg:
                continue
            taste = _unit(np.mean(pos, axis=0) - np.mean(neg, axis=0))
            score = float(np.dot(x, taste))
            loo_scores.append(score)
            loo_labels.append(label)
            table.append((score, label, digest_id, headline))

        like_scores = [s for s, y in zip(loo_scores, loo_labels, strict=True) if y == 1]
        dislike_scores = [s for s, y in zip(loo_scores, loo_labels, strict=True) if y == 0]
        auc = _auc(loo_scores, loo_labels)

        print()
        print("=" * 70)
        print("LEAVE-ONE-OUT SEPARATION  (higher score = more 'your taste')")
        print("=" * 70)
        gap = float(np.mean(like_scores) - np.mean(dislike_scores))
        print(f"  mean taste-score  👍 likes   : {np.mean(like_scores):+.3f}")
        print(f"  mean taste-score  👎 dislikes: {np.mean(dislike_scores):+.3f}")
        print(f"  gap (like - dislike)         : {gap:+.3f}")
        print(f"  ROC-AUC                      : {auc:.3f}   (0.5=noise, 1.0=perfect)")

        # best-threshold LOO accuracy
        best_acc, best_thr = 0.0, 0.0
        for thr in sorted(loo_scores):
            acc = sum(
                1
                for s, y in zip(loo_scores, loo_labels, strict=True)
                if (s >= thr) == (y == 1)
            ) / len(loo_scores)
            if acc > best_acc:
                best_acc, best_thr = acc, thr
        base = max(n_pos, n_neg) / len(samples)
        print(f"  best LOO accuracy            : {best_acc:.3f} @ thr={best_thr:+.3f}"
              f"   (majority baseline {base:.3f})")

        print()
        print("=" * 70)
        print("SORTED BY TASTE-SCORE  (top = bot would rank first)")
        print("=" * 70)
        for score, label, digest_id, headline in sorted(table, reverse=True):
            mark = "👍" if label == 1 else "👎"
            print(f"  {score:+.3f} {mark} #{digest_id}  {headline[:70]}")


if __name__ == "__main__":
    asyncio.run(main())
