"""Read-only report over Telegram feedback, impressions, and discussions.

Run: uv run python -m audit.feedback_report
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from engine.db import session_scope


async def _scalar(session, sql: str) -> object:
    return await session.scalar(text(sql))


async def main() -> None:
    async with session_scope() as session:
        print("=" * 70)
        print("OVERVIEW")
        print("=" * 70)
        totals = (
            await session.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM digests)                          AS digests,
                      (SELECT count(*) FROM digests WHERE delivered_at IS NOT NULL) AS delivered,
                      (SELECT count(*) FROM impressions)                       AS impressions,
                      (SELECT count(*) FROM digest_feedback)                   AS feedback_rows,
                      (SELECT count(*) FROM digest_feedback WHERE feedback='like')    AS likes,
                      (SELECT count(*) FROM digest_feedback WHERE feedback='dislike') AS dislikes,
                      (SELECT count(DISTINCT digest_id) FROM digest_feedback)  AS rated_digests
                    """
                )
            )
        ).mappings().one()
        for k, v in totals.items():
            print(f"  {k:>16}: {v}")

        print()
        print("=" * 70)
        print("LATEST (net) FEEDBACK PER DIGEST  — what you actually pressed")
        print("=" * 70)
        rows = (
            await session.execute(
                text(
                    """
                    WITH ranked AS (
                      SELECT df.*,
                             row_number() OVER (
                               PARTITION BY digest_id, chat_id
                               ORDER BY created_at DESC, id DESC
                             ) AS rn
                      FROM digest_feedback df
                    )
                    SELECT r.created_at, r.feedback, r.digest_id,
                           d.profile_name, d.headline, d.confidence_level,
                           s.name AS source
                    FROM ranked r
                    JOIN digests d ON d.id = r.digest_id
                    JOIN events e ON e.id = d.event_id
                    LEFT JOIN event_members em ON em.event_id = e.id
                    LEFT JOIN articles a ON a.id = em.article_id
                    LEFT JOIN sources s ON s.id = a.source_id
                    WHERE r.rn = 1
                    ORDER BY r.created_at DESC
                    """
                )
            )
        ).mappings().all()
        # dedupe sources per digest (event may have many members)
        seen: dict[int, dict] = {}
        for row in rows:
            d = seen.setdefault(
                row["digest_id"],
                {
                    "created_at": row["created_at"],
                    "feedback": row["feedback"],
                    "profile": row["profile_name"],
                    "headline": row["headline"],
                    "conf": row["confidence_level"],
                    "sources": set(),
                },
            )
            if row["source"]:
                d["sources"].add(row["source"])
        for digest_id, d in sorted(seen.items(), key=lambda x: x[1]["created_at"], reverse=True):
            mark = "👍" if d["feedback"] == "like" else "👎"
            srcs = ",".join(sorted(d["sources"])) or "?"
            ts = d["created_at"].strftime("%m-%d %H:%M")
            print(f"  {mark} [{ts}] #{digest_id} ({d['profile']}/{d['conf']}/{srcs})")
            print(f"       {d['headline'][:90]}")

        print()
        print("=" * 70)
        print("LIKE/DISLIKE BY PROFILE  (latest feedback per digest)")
        print("=" * 70)
        prof = (
            await session.execute(
                text(
                    """
                    WITH ranked AS (
                      SELECT df.*, row_number() OVER (
                               PARTITION BY digest_id, chat_id
                               ORDER BY created_at DESC, id DESC) AS rn
                      FROM digest_feedback df)
                    SELECT d.profile_name,
                           count(*) FILTER (WHERE r.feedback='like')    AS likes,
                           count(*) FILTER (WHERE r.feedback='dislike') AS dislikes
                    FROM ranked r JOIN digests d ON d.id=r.digest_id
                    WHERE r.rn=1 GROUP BY d.profile_name ORDER BY 2 DESC
                    """
                )
            )
        ).mappings().all()
        for row in prof:
            print(f"  {row['profile_name']:>22}: 👍 {row['likes']}  👎 {row['dislikes']}")

        print()
        print("=" * 70)
        print("LIKE/DISLIKE BY SOURCE  (latest feedback per digest; event-deduped)")
        print("=" * 70)
        src = (
            await session.execute(
                text(
                    """
                    WITH ranked AS (
                      SELECT df.*, row_number() OVER (
                               PARTITION BY digest_id, chat_id
                               ORDER BY created_at DESC, id DESC) AS rn
                      FROM digest_feedback df),
                    latest AS (SELECT * FROM ranked WHERE rn=1),
                    digest_source AS (
                      SELECT DISTINCT l.digest_id, l.feedback, s.name AS source
                      FROM latest l
                      JOIN digests d ON d.id=l.digest_id
                      JOIN events e ON e.id=d.event_id
                      JOIN event_members em ON em.event_id=e.id
                      JOIN articles a ON a.id=em.article_id
                      JOIN sources s ON s.id=a.source_id)
                    SELECT source,
                           count(*) FILTER (WHERE feedback='like')    AS likes,
                           count(*) FILTER (WHERE feedback='dislike') AS dislikes
                    FROM digest_source GROUP BY source ORDER BY 2 DESC, 3 DESC
                    """
                )
            )
        ).mappings().all()
        for row in src:
            print(f"  {row['source']:>24}: 👍 {row['likes']}  👎 {row['dislikes']}")

        print()
        print("=" * 70)
        print("ENGAGEMENT: impressions vs feedback (CTR-like)")
        print("=" * 70)
        imp = totals["impressions"] or 0
        rated = totals["rated_digests"] or 0
        if imp:
            print(f"  rated/impressions = {rated}/{imp} = {100*rated/imp:.1f}%")
        fb_rows = totals["feedback_rows"] or 0
        like_share = (100 * totals["likes"] / fb_rows) if fb_rows else 0.0
        print(f"  like share of rated = {like_share:.1f}%")

        print()
        print("=" * 70)
        print("DISCUSSIONS asked (decisions target_type='discussion')")
        print("=" * 70)
        disc = (
            await session.execute(
                text(
                    """
                    SELECT created_at, target_id, model, cost_usd, decision_json
                    FROM decisions WHERE target_type='discussion'
                    ORDER BY created_at DESC LIMIT 50
                    """
                )
            )
        ).mappings().all()
        print(f"  total discussion answers: {len(disc)}")
        for row in disc:
            ts = row["created_at"].strftime("%m-%d %H:%M")
            dj = row["decision_json"] or {}
            q = dj.get("question") or dj.get("q") or ""
            print(f"  [{ts}] digest#{row['target_id']} {row['model']} ${row['cost_usd']}")
            if q:
                print(f"        Q: {str(q)[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
