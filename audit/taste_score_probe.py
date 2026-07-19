"""Probe: why is taste ranking producing no taste_cosine in impressions.context?

Every impression logged by the dispatcher should carry a ranking context with
`ranking_version`, `taste_cosine` and `taste_labels` (delivery/dispatcher.py:134-141).
Production shows a null taste score throughout. There are exactly three code paths
that produce that, and they need different fixes:

  A. context has no `ranking_version` at all
     -> `taste_ranking_enabled` was False in the process that delivered
        (dispatcher.py:102 returns RankedDigest(context=None)).
        The flag is not reaching delivery — check .env and whether the process
        that ran `delivery send` was started before the flag was set.

  B. `ranking_version` present, but both `taste_cosine` and `taste_labels` null
     -> build_taste_vector returned None (engine/ranking/taste.py:96):
        fewer than `taste_min_labels_per_class` usable likes OR dislikes.
        Note the eligibility filter: dislikes with reason 'weak_analysis' are
        excluded on purpose, and only the LATEST feedback per (digest, chat)
        counts. This is a data-volume problem, not a bug.

  C. `taste_labels` non-null but `taste_cosine` null
     -> the taste vector existed but the delivered event had no centroid
        (dispatcher.py:123-125). That is a real defect worth chasing.

The probe reuses build_taste_vector's own SQL verbatim so it cannot disagree
with production logic about which labels count.

Read-only. Run on the SERVER — feedback and impressions exist only there:

    cd /root/NewsAnalyzer && uv run python audit/taste_score_probe.py
"""

import asyncio
from collections import Counter

from sqlalchemy import text

from engine.config import get_settings
from engine.db import session_scope

# Verbatim from engine/ranking/taste.py:56-75 — keep in sync if that query changes.
ELIGIBLE_LABELS_SQL = """
WITH ranked AS (
  SELECT df.*, row_number() OVER (
           PARTITION BY digest_id, chat_id
           ORDER BY created_at DESC, id DESC) AS rn
  FROM digest_feedback df
  WHERE df.chat_id = :chat_id)
SELECT d.event_id, r.feedback
FROM ranked r JOIN digests d ON d.id = r.digest_id
WHERE r.rn = 1
  AND (r.feedback = 'like'
       OR (r.feedback = 'dislike'
           AND r.reason IS DISTINCT FROM 'weak_analysis'))
"""


async def main() -> None:
    settings = get_settings()
    threshold = settings.taste_min_labels_per_class

    async with session_scope() as session:
        print("=== settings as this process sees them ===")
        print(f"  taste_ranking_enabled     = {settings.taste_ranking_enabled}")
        print(f"  taste_min_labels_per_class= {threshold}")
        print(f"  taste_weight              = {settings.taste_weight}")
        print(f"  significance_weight       = {settings.significance_weight}")
        print("  NOTE: a long-running listener caches settings at startup; this")
        print("        reflects THIS process, not necessarily the delivering one.\n")

        print("=== impressions by outcome, per day (Europe/Warsaw) ===")
        print("    A = no ranking_version  B = no taste vector  C = vector but no centroid")
        rows = (
            await session.execute(
                text(
                    """
                    SELECT date_trunc('day', shown_at AT TIME ZONE 'Europe/Warsaw')::date AS day,
                           count(*) AS total,
                           count(*) FILTER (
                             WHERE context->>'ranking_version' IS NULL) AS case_a,
                           count(*) FILTER (
                             WHERE context->>'ranking_version' IS NOT NULL
                               AND context->>'taste_cosine' IS NULL
                               AND context->>'taste_labels' IS NULL) AS case_b,
                           count(*) FILTER (
                             WHERE context->>'taste_labels' IS NOT NULL
                               AND context->>'taste_cosine' IS NULL) AS case_c,
                           count(*) FILTER (
                             WHERE context->>'taste_cosine' IS NOT NULL) AS scored
                    FROM impressions
                    GROUP BY day
                    ORDER BY day DESC
                    LIMIT 30
                    """
                )
            )
        ).all()
        for day, total, case_a, case_b, case_c, scored in rows:
            print(
                f"  {day}  total={total:<4} A={case_a:<4} B={case_b:<4} "
                f"C={case_c:<4} scored={scored}"
            )
        if not rows:
            print("  (no impressions at all — delivery never logged one)")

        print("\n=== one sample context per outcome ===")
        for label, predicate in (
            ("A no ranking_version", "context->>'ranking_version' IS NULL"),
            (
                "B no taste vector",
                "context->>'ranking_version' IS NOT NULL AND context->>'taste_labels' IS NULL",
            ),
            (
                "C no centroid",
                "context->>'taste_labels' IS NOT NULL AND context->>'taste_cosine' IS NULL",
            ),
            ("scored", "context->>'taste_cosine' IS NOT NULL"),
        ):
            sample = (
                await session.execute(
                    text(
                        f"""
                        SELECT context FROM impressions
                        WHERE {predicate}
                        ORDER BY shown_at DESC LIMIT 1
                        """  # noqa: S608 - predicates are literals defined above
                    )
                )
            ).scalar()
            print(f"  {label}: {sample if sample is not None else '(none)'}")

        print("\n=== feedback inventory per chat ===")
        chat_rows = (
            await session.execute(
                text(
                    """
                    SELECT chat_id, feedback, coalesce(reason, '-') AS reason, count(*)
                    FROM digest_feedback
                    GROUP BY chat_id, feedback, reason
                    ORDER BY chat_id, feedback, reason
                    """
                )
            )
        ).all()
        if not chat_rows:
            print("  (no feedback rows at all)")
        for chat_id, feedback, reason, count in chat_rows:
            print(f"  chat={chat_id} {feedback}/{reason}: {count}")

        print("\n=== would build_taste_vector succeed? (per chat, replaying its own SQL) ===")
        chat_ids = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT DISTINCT chat_id FROM digest_feedback ORDER BY chat_id")
                )
            ).all()
        ]
        for chat_id in chat_ids:
            labels = (await session.execute(text(ELIGIBLE_LABELS_SQL), {"chat_id": chat_id})).all()
            counts = Counter(feedback for _event_id, feedback in labels)
            event_ids = [event_id for event_id, _ in labels]
            missing_centroid = 0
            if event_ids:
                missing_centroid = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM events WHERE id = ANY(:ids) AND centroid IS NULL"
                        ),
                        {"ids": event_ids},
                    )
                ).scalar() or 0
            usable_like = counts.get("like", 0)
            usable_dislike = counts.get("dislike", 0)
            verdict = (
                "OK — vector builds"
                if usable_like >= threshold and usable_dislike >= threshold
                else f"NONE — needs >= {threshold} of each class"
            )
            print(
                f"  chat={chat_id} eligible_like={usable_like} "
                f"eligible_dislike={usable_dislike} "
                f"labelled_events_without_centroid={missing_centroid} -> {verdict}"
            )

        print("\n=== centroid health on recently delivered events ===")
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS delivered,
                           count(*) FILTER (WHERE e.centroid IS NULL) AS no_centroid
                    FROM digests d
                    JOIN events e ON e.id = d.event_id
                    WHERE d.delivered_at > now() - interval '30 days'
                    """
                )
            )
        ).first()
        if row is not None:
            print(f"  delivered_30d={row[0]} without_centroid={row[1]}")


asyncio.run(main())
