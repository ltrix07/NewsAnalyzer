"""Probe: why were same-story events not merged by cluster/consolidate?

Given a list of event ids that should have been one story, prints everything
that governs merging:

  * timestamps (created_at / first_seen_at / last_seen_at / article published_at)
    -> tests the "36h cluster window" hypothesis: cluster only considers events
       whose last_seen_at is within cluster_window_hours of now, and last_seen_at
       comes from the article's published_at, which a fetch backlog can scatter
       across days;
  * the full pairwise centroid similarity matrix, bucketed against both
    thresholds -> shows which pairs cluster should have caught (>= 0.82) and
    which fall in the consolidate band (0.50 .. 0.82);
  * whether a consolidate decision exists for the run and what it concluded
    -> separates "consolidate never looked" from "the LLM judge said no".

Run on the SERVER:

    cd /root/NewsAnalyzer && uv run python audit/why_not_merged.py 6056 6147 6129 6130 6160 6176
"""

import asyncio
import sys

from sqlalchemy import text

from engine.config import get_settings
from engine.db import session_scope

EVENT_IDS = [int(arg) for arg in sys.argv[1:] if arg.isdigit()]
if not EVENT_IDS:
    print("usage: uv run python audit/why_not_merged.py <event_id> <event_id> ...")
    raise SystemExit(1)


async def main() -> None:
    settings = get_settings()
    cluster_threshold = settings.cluster_similarity_threshold
    cluster_window = settings.cluster_window_hours

    async with session_scope() as s:
        print(
            f"cluster_threshold={cluster_threshold} cluster_window={cluster_window}h "
            f"consolidate_enabled={settings.consolidate_enabled} "
            f"consolidate_min_sim={settings.consolidate_candidate_min_similarity} "
            f"consolidate_max_neighbors={settings.consolidate_max_neighbors} "
            f"consolidate_window={settings.consolidate_window_hours}h"
        )
        print(f"taste_ranking_enabled={settings.taste_ranking_enabled}\n")

        print("=== event timestamps (all times Europe/Warsaw) ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT e.id, e.article_count,
                           e.created_at AT TIME ZONE 'Europe/Warsaw' AS created,
                           e.first_seen_at AT TIME ZONE 'Europe/Warsaw' AS first_seen,
                           e.last_seen_at AT TIME ZONE 'Europe/Warsaw' AS last_seen,
                           round(extract(epoch from (now() - e.last_seen_at)) / 3600.0, 1)
                             AS age_hours,
                           (SELECT string_agg(
                                     coalesce(
                                       to_char(a.published_at AT TIME ZONE 'Europe/Warsaw',
                                               'MM-DD HH24:MI'),
                                       'NULL') || ' [' || src.name || ']', ' | ')
                              FROM event_members m
                              JOIN articles a ON a.id = m.article_id
                              JOIN sources src ON src.id = a.source_id
                             WHERE m.event_id = e.id) AS articles
                    FROM events e
                    WHERE e.id = ANY(:ids)
                    ORDER BY e.id
                    """
                ),
                {"ids": EVENT_IDS},
            )
        ).all()
        for eid, count, created, first_seen, last_seen, age, articles in rows:
            fresh = age is not None and age <= cluster_window
            in_window = "IN window" if fresh else "OUT of window"
            print(f"  event={eid} articles={count} created={created}")
            print(f"    first_seen={first_seen} last_seen={last_seen} age={age}h [{in_window}]")
            print(f"    published: {articles}")

        print("\n=== pairwise centroid similarity ===")
        print("    >= cluster_threshold : cluster SHOULD have merged (consolidate ignores these)")
        print("    consolidate band     : consolidate SHOULD have judged")
        print("    below band           : invisible to both\n")
        for left in EVENT_IDS:
            for right in EVENT_IDS:
                if left >= right:
                    continue
                sim = (
                    await s.execute(
                        text(
                            """
                            SELECT 1 - (a.centroid <=> b.centroid)
                            FROM events a, events b
                            WHERE a.id = :left AND b.id = :right
                            """
                        ),
                        {"left": left, "right": right},
                    )
                ).scalar()
                if sim is None:
                    continue
                value = float(sim)
                if value >= cluster_threshold:
                    bucket = "CLUSTER-MISS (>= threshold, consolidate skips it)"
                elif value >= settings.consolidate_candidate_min_similarity:
                    bucket = "consolidate band"
                else:
                    bucket = "below band"
                print(f"  {left} <-> {right}  sim={value:.3f}  {bucket}")

        print("\n=== consolidate decisions touching these events ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT id, target_id,
                           created_at AT TIME ZONE 'Europe/Warsaw' AS created,
                           decision_json
                    FROM decisions
                    WHERE stage_name = 'consolidate'
                      AND created_at > now() - interval '3 days'
                    ORDER BY created_at DESC
                    LIMIT 40
                    """
                )
            )
        ).all()
        if not rows:
            print("  (NO consolidate decisions in 3 days -> the stage never ran or found nothing)")
        for did, target_id, created, payload in rows:
            print(f"  id={did} target={target_id} created={created} {payload}")

        print("\n=== cluster decisions for these events' articles ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT d.target_id, d.decision_json,
                           d.created_at AT TIME ZONE 'Europe/Warsaw' AS created
                    FROM decisions d
                    WHERE d.stage_name = 'cluster'
                      AND d.target_type = 'article'
                      AND d.target_id IN (
                        SELECT m.article_id FROM event_members m
                         WHERE m.event_id = ANY(:ids))
                    ORDER BY d.created_at
                    """
                ),
                {"ids": EVENT_IDS},
            )
        ).all()
        for target_id, payload, created in rows:
            print(f"  article={target_id} created={created} {payload}")


asyncio.run(main())
