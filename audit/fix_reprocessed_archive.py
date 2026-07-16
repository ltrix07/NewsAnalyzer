"""Diagnose and repair the 5b archive-reprocessing incident.

Migration d8e9f0a1b2c3 added ``decisions.profile_name`` but did not backfill
historical rows. The per-user stages select candidates with
``~exists(decision WHERE profile_name = <username>)``, so every pre-migration
decision (profile_name IS NULL) is invisible to them and the whole archive looks
unprocessed — it gets re-scored, re-verified, re-summarized, and the resulting
duplicate digests get delivered.

Run with no arguments for a read-only report. Pass --apply to repair:

  1. backfill ``profile_name`` on historical per-user decisions, so the archive
     stops looking unprocessed;
  2. delete undelivered duplicate digests (an undelivered digest whose event
     already has a delivered one) so tomorrow's run does not send them.

Genuinely new digests (events with no delivered digest yet) are never touched.
"""

import asyncio
import sys

from sqlalchemy import text

from engine.config import get_settings
from engine.db import session_scope

PER_USER_STAGES = ("keyword_filter", "relevance", "verify", "summarize")


async def report(session, username: str) -> None:
    print("=== decisions by stage x profile_name ===")
    rows = (
        await session.execute(
            text(
                """
                SELECT stage_name, profile_name, count(*)
                FROM decisions
                GROUP BY 1, 2 ORDER BY 1, 2
                """
            )
        )
    ).all()
    for stage, profile, n in rows:
        mark = "  <-- invisible to new code" if profile is None and stage in PER_USER_STAGES else ""
        print(f"  {stage:<16} profile={str(profile):<12} n={n}{mark}")

    orphaned = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM decisions
                WHERE profile_name IS NULL AND stage_name = ANY(:stages)
                """
            ),
            {"stages": list(PER_USER_STAGES)},
        )
    ).scalar()
    print(f"\nhistorical per-user decisions needing backfill -> {orphaned}")

    print("\n=== digests ===")
    rows = (
        await session.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE delivered_at IS NULL) AS undelivered,
                       count(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                       count(*) AS total
                FROM digests
                """
            )
        )
    ).all()
    for undelivered, delivered, total in rows:
        print(f"  total={total} delivered={delivered} undelivered={undelivered}")

    dupes = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM digests d
                WHERE d.delivered_at IS NULL
                  AND EXISTS (
                    SELECT 1 FROM digests o
                    WHERE o.event_id = d.event_id
                      AND o.id <> d.id
                      AND o.delivered_at IS NOT NULL)
                """
            )
        )
    ).scalar()
    print(f"  undelivered DUPLICATES (event already had a delivered digest) -> {dupes}")

    fresh = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM digests d
                WHERE d.delivered_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM digests o
                    WHERE o.event_id = d.event_id
                      AND o.id <> d.id
                      AND o.delivered_at IS NOT NULL)
                """
            )
        )
    ).scalar()
    print(f"  undelivered genuinely new (kept) -> {fresh}")

    print("\n=== undelivered digests, by age of their underlying event ===")
    rows = (
        await session.execute(
            text(
                """
                SELECT d.id,
                       date(e.last_seen_at AT TIME ZONE 'Europe/Warsaw') AS event_day,
                       (now() - e.last_seen_at) > interval '3 days' AS stale,
                       left(d.headline, 55) AS headline
                FROM digests d JOIN events e ON e.id = d.event_id
                WHERE d.delivered_at IS NULL
                ORDER BY e.last_seen_at
                """
            )
        )
    ).all()
    for digest_id, event_day, stale, headline in rows:
        flag = "STALE (archive)" if stale else "fresh"
        print(f"  id={digest_id} event_day={event_day} [{flag}] {headline}")

    print("\n=== pipeline queue after backfill (what tomorrow's cron would chew) ===")
    verify_q = (
        await session.execute(
            text(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (target_id) target_id,
                         decision_json->>'action' AS action
                  FROM decisions
                  WHERE stage_name = 'relevance' AND target_type = 'event'
                    AND profile_name = :username
                  ORDER BY target_id, created_at DESC, id DESC)
                SELECT count(*) FROM latest l
                WHERE l.action = 'relevant'
                  AND NOT EXISTS (
                    SELECT 1 FROM decisions d
                    WHERE d.stage_name = 'verify' AND d.target_type = 'event'
                      AND d.target_id = l.target_id AND d.profile_name = :username)
                """
            ),
            {"username": username},
        )
    ).scalar()
    print(f"  events awaiting verify   -> {verify_q}   (cron does 20/run)")

    summarize_q = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM decisions v
                WHERE v.stage_name = 'verify' AND v.target_type = 'event'
                  AND v.profile_name = :username
                  AND NOT EXISTS (
                    SELECT 1 FROM decisions s
                    WHERE s.stage_name = 'summarize'
                      AND s.profile_name = :username
                      AND s.decision_json->>'event_id' = v.target_id::text)
                """
            ),
            {"username": username},
        )
    ).scalar()
    print(f"  events awaiting summarize -> {summarize_q}   (cron does 15/run)")

    print(f"\nbackfill username would be: {username!r}")


async def apply(session, username: str) -> None:
    result = await session.execute(
        text(
            """
            UPDATE decisions SET profile_name = :username
            WHERE profile_name IS NULL AND stage_name = ANY(:stages)
            """
        ),
        {"username": username, "stages": list(PER_USER_STAGES)},
    )
    print(f"backfilled decisions -> {result.rowcount}")

    result = await session.execute(
        text(
            """
            DELETE FROM digests d
            WHERE d.delivered_at IS NULL
              AND EXISTS (
                SELECT 1 FROM digests o
                WHERE o.event_id = d.event_id
                  AND o.id <> d.id
                  AND o.delivered_at IS NOT NULL)
            """
        )
    )
    print(f"deleted undelivered duplicate digests -> {result.rowcount}")

    result = await session.execute(
        text(
            """
            DELETE FROM digests d
            USING events e
            WHERE e.id = d.event_id
              AND d.delivered_at IS NULL
              AND (now() - e.last_seen_at) > interval '3 days'
            """
        )
    )
    print(f"deleted undelivered digests about stale (archive) events -> {result.rowcount}")


async def main() -> None:
    do_apply = "--apply" in sys.argv
    username = get_settings().profile_name
    async with session_scope() as session:
        await report(session, username)
        if not do_apply:
            print("\n(read-only. re-run with --apply to repair)")
            return
        print("\n=== APPLYING ===")
        await apply(session, username)
        print("done. re-running report:\n")
        await report(session, username)


asyncio.run(main())
