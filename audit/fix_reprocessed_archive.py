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
