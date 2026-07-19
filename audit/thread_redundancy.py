"""Probe: what do redundant thread updates actually look like?

Reconstructs delivered story threads from ``impressions.context`` and prints,
for every chain of 2+ digests, the full text of each update next to cheap
novelty signals measured against its parent:

  centroid_sim   cosine similarity of the two event centroids (what threading
                 already uses to decide "same story")
  text_jaccard   token overlap of headline+summary+why_it_matters
  new_numbers    numeric tokens in the child absent from the parent (casualty
                 counts, dates, sums — usually where real news lives)
  new_caps       capitalised tokens in the child absent from the parent (new
                 actors/places)
  article_reuse  share of the child event's article URLs already cited by the
                 parent digest — 1.0 means literally the same reporting

The point is to see which of these separates "genuinely developed" from
"said the same thing again", before hardcoding a threshold.

Run on the SERVER:

    cd /root/NewsAnalyzer && uv run python audit/thread_redundancy.py [--days 7]
"""

import asyncio
import re
import sys

from sqlalchemy import text

from engine.db import session_scope

DAYS = 7
if "--days" in sys.argv:
    DAYS = int(sys.argv[sys.argv.index("--days") + 1])

WORD_RE = re.compile(r"[\w'’-]+", re.UNICODE)
NUMBER_RE = re.compile(r"\d[\d\s.,]*")

STOPWORDS = {
    "и", "в", "на", "с", "по", "что", "не", "для", "как", "это", "от", "за",
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "at", "by",
}


def tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in WORD_RE.findall(value or "")
        if len(token) > 2 and token.lower() not in STOPWORDS
    }


def numbers(value: str) -> set[str]:
    return {match.group().strip().rstrip(".,") for match in NUMBER_RE.finditer(value or "")}


def capitalised(value: str) -> set[str]:
    return {
        token
        for token in WORD_RE.findall(value or "")
        if len(token) > 2 and token[:1].isupper()
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def full_text(row: dict) -> str:
    return f"{row['headline']}\n{row['summary']}\n{row['why_it_matters']}"


async def main() -> None:
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT d.id, d.event_id, d.profile_name, d.headline, d.summary,
                           d.why_it_matters, d.citations,
                           d.delivered_at AT TIME ZONE 'Europe/Warsaw' AS delivered,
                           (i.context->>'threaded_parent_digest_id')::bigint AS parent_id,
                           (i.context->>'taste_score')::float AS taste_score,
                           e.article_count,
                           (SELECT f.feedback || coalesce(':' || f.reason, '')
                              FROM digest_feedback f
                             WHERE f.digest_id = d.id
                             ORDER BY f.created_at DESC LIMIT 1) AS feedback
                    FROM digests d
                    JOIN events e ON e.id = d.event_id
                    LEFT JOIN LATERAL (
                      SELECT context FROM impressions
                      WHERE digest_id = d.id
                      ORDER BY shown_at DESC, id DESC LIMIT 1) i ON true
                    WHERE d.delivered_at >= now() - make_interval(days => :days)
                    ORDER BY d.delivered_at
                    """
                ),
                {"days": DAYS},
            )
        ).mappings().all()

        by_id = {row["id"]: dict(row) for row in rows}
        children: dict[int, list[int]] = {}
        for row in rows:
            if row["parent_id"] is not None:
                children.setdefault(row["parent_id"], []).append(row["id"])

        roots = [
            row["id"]
            for row in rows
            if row["parent_id"] is None or row["parent_id"] not in by_id
        ]

        def chain(root_id: int) -> list[int]:
            ordered = [root_id]
            queue = [root_id]
            while queue:
                current = queue.pop(0)
                for child in sorted(children.get(current, [])):
                    ordered.append(child)
                    queue.append(child)
            return ordered

        threads = [chain(root) for root in roots]
        threads = [t for t in threads if len(t) >= 2]
        threads.sort(key=len, reverse=True)

        print(f"delivered digests in last {DAYS}d: {len(rows)}")
        print(f"threads with 2+ updates: {len(threads)}")
        singles = len(rows) - sum(len(t) for t in threads)
        print(f"standalone digests: {singles}\n")

        if not threads:
            print("(no multi-digest threads found — check impressions.context is populated)")
            return

        for index, thread in enumerate(threads, start=1):
            print("=" * 78)
            print(f"THREAD {index} — {len(thread)} digests")
            print("=" * 78)
            previous: dict | None = None
            for position, digest_id in enumerate(thread):
                row = by_id[digest_id]
                citations = row["citations"] or []
                urls = {c.get("url") for c in citations if isinstance(c, dict)}

                print(f"\n--- [{position}] digest_id={digest_id} event_id={row['event_id']}"
                      f" delivered={row['delivered']} articles={row['article_count']}")
                print(f"    feedback={row['feedback']} taste={row['taste_score']}")

                if previous is not None:
                    sim = (
                        await s.execute(
                            text(
                                """
                                SELECT 1 - (a.centroid <=> b.centroid)
                                FROM events a, events b
                                WHERE a.id = :left AND b.id = :right
                                """
                            ),
                            {"left": previous["event_id"], "right": row["event_id"]},
                        )
                    ).scalar()
                    previous_urls = {
                        c.get("url")
                        for c in (previous["citations"] or [])
                        if isinstance(c, dict)
                    }
                    reuse = len(urls & previous_urls) / len(urls) if urls else 0.0
                    previous_text = full_text(previous)
                    current_text = full_text(row)
                    print(
                        f"    vs parent: centroid_sim={float(sim or 0):.3f}"
                        f" text_jaccard={jaccard(tokens(previous_text), tokens(current_text)):.3f}"
                        f" article_reuse={reuse:.2f}"
                    )
                    new_numbers = numbers(current_text) - numbers(previous_text)
                    new_caps = capitalised(current_text) - capitalised(previous_text)
                    print(f"    new_numbers={sorted(new_numbers)[:8]}")
                    print(f"    new_caps={sorted(new_caps)[:8]}")

                print(f"    HEADLINE: {row['headline']}")
                print(f"    SUMMARY: {row['summary']}")
                print(f"    WHY: {row['why_it_matters']}")
                previous = row

        print("\n" + "=" * 78)
        print("AGGREGATE over consecutive pairs in threads")
        print("=" * 78)
        sims: list[float] = []
        jaccards: list[float] = []
        reuses: list[float] = []
        for thread in threads:
            for left_id, right_id in zip(thread, thread[1:], strict=False):
                left, right = by_id[left_id], by_id[right_id]
                sim = (
                    await s.execute(
                        text(
                            """
                            SELECT 1 - (a.centroid <=> b.centroid)
                            FROM events a, events b
                            WHERE a.id = :left AND b.id = :right
                            """
                        ),
                        {"left": left["event_id"], "right": right["event_id"]},
                    )
                ).scalar()
                sims.append(float(sim or 0))
                jaccards.append(jaccard(tokens(full_text(left)), tokens(full_text(right))))
                left_urls = {
                    c.get("url") for c in (left["citations"] or []) if isinstance(c, dict)
                }
                right_urls = {
                    c.get("url") for c in (right["citations"] or []) if isinstance(c, dict)
                }
                reuses.append(len(right_urls & left_urls) / len(right_urls) if right_urls else 0.0)

        def describe(label: str, values: list[float]) -> None:
            if not values:
                return
            ordered = sorted(values)
            median = ordered[len(ordered) // 2]
            print(
                f"  {label:<14} n={len(values)} min={ordered[0]:.3f}"
                f" median={median:.3f} max={ordered[-1]:.3f}"
            )

        describe("centroid_sim", sims)
        describe("text_jaccard", jaccards)
        describe("article_reuse", reuses)


asyncio.run(main())
