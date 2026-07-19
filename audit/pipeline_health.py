"""Read-only health check: where does the chain break?

Walks the pipeline day by day — articles fetched, events touched, per-user
decisions written, digests created, digests delivered. The first column that
goes to zero is the stage that stopped.

Run on the SERVER (feedback/impressions/live delivery live only there):

    cd /root/NewsAnalyzer && uv run python audit/pipeline_health.py
"""

import asyncio

from sqlalchemy import text

from engine.config import get_settings
from engine.db import session_scope

DAYS = 14


async def main() -> None:
    settings = get_settings()
    username = settings.profile_name
    window = settings.selection_window_hours

    async with session_scope() as s:
        print(f"profile={username!r}  selection_window={window}h\n")

        print(f"=== daily chain, last {DAYS} days (Europe/Warsaw) ===")
        print("  day         articles  events_seen  decisions  digests  delivered")
        rows = (
            await s.execute(
                text(
                    """
                    WITH days AS (
                      SELECT generate_series(
                        current_date - make_interval(days => :days),
                        current_date, '1 day')::date AS day)
                    SELECT d.day,
                      (SELECT count(*) FROM articles a
                         WHERE date(a.fetched_at AT TIME ZONE 'Europe/Warsaw') = d.day),
                      (SELECT count(*) FROM events e
                         WHERE date(e.last_seen_at AT TIME ZONE 'Europe/Warsaw') = d.day),
                      (SELECT count(*) FROM decisions dec
                         WHERE dec.profile_name = :username
                           AND date(dec.created_at AT TIME ZONE 'Europe/Warsaw') = d.day),
                      (SELECT count(*) FROM digests g
                         WHERE date(g.created_at AT TIME ZONE 'Europe/Warsaw') = d.day),
                      (SELECT count(*) FROM digests g
                         WHERE date(g.delivered_at AT TIME ZONE 'Europe/Warsaw') = d.day)
                    FROM days d ORDER BY d.day
                    """
                ),
                {"days": DAYS, "username": username},
            )
        ).all()
        for day, articles, events, decisions, digests, delivered in rows:
            print(
                f"  {day}  {articles:>8}  {events:>11}  {decisions:>9}"
                f"  {digests:>7}  {delivered:>9}"
            )

        print("\n=== last activity per stage ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT stage_name, profile_name,
                           max(created_at AT TIME ZONE 'Europe/Warsaw') AS last_run,
                           count(*) AS n
                    FROM decisions
                    WHERE created_at > now() - interval '14 days'
                    GROUP BY 1, 2 ORDER BY 3 DESC NULLS LAST
                    """
                )
            )
        ).all()
        if not rows:
            print("  (no decisions at all in 14 days -> the pipeline never ran)")
        for stage, profile, last_run, n in rows:
            print(f"  {stage:<16} profile={str(profile):<12} last={last_run} n={n}")

        print("\n=== undelivered digests waiting right now ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT g.id, g.profile_name,
                           date(g.created_at AT TIME ZONE 'Europe/Warsaw') AS created_day,
                           date(e.last_seen_at AT TIME ZONE 'Europe/Warsaw') AS event_day,
                           g.batch_id,
                           left(g.headline, 50) AS headline
                    FROM digests g JOIN events e ON e.id = g.event_id
                    WHERE g.delivered_at IS NULL
                    ORDER BY g.created_at
                    """
                )
            )
        ).all()
        for gid, profile, created_day, event_day, batch_id, headline in rows:
            print(
                f"  id={gid} prof={profile} created={created_day} event={event_day}"
                f" batch={batch_id} | {headline}"
            )
        print(f"  -> {len(rows)} undelivered")

        print(f"\n=== fresh events in the {window}h window (what a run WOULD pick up) ===")
        row = (
            await s.execute(
                text(
                    """
                    SELECT count(*) FROM events e
                    WHERE (now() - e.last_seen_at) <= make_interval(hours => :window)
                    """
                ),
                {"window": window},
            )
        ).scalar()
        print(f"  events in window: {row}")

        print("\n=== users ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT username, enabled, chat_id IS NOT NULL AS has_chat,
                           profile IS NULL AS no_profile
                    FROM users ORDER BY username
                    """
                )
            )
        ).all()
        for username_, enabled, has_chat, no_profile in rows:
            print(
                f"  {username_!r} enabled={enabled} has_chat={has_chat}"
                f" no_profile={no_profile}"
            )

        print("\n=== delivery batches (if batched delivery is on) ===")
        rows = (
            await s.execute(
                text(
                    """
                    SELECT id, chat_id,
                           created_at AT TIME ZONE 'Europe/Warsaw' AS created,
                           notified_at IS NOT NULL AS notified,
                           opened_at IS NOT NULL AS opened,
                           closed_at IS NOT NULL AS closed
                    FROM delivery_batches
                    ORDER BY created_at DESC LIMIT 10
                    """
                )
            )
        ).all()
        if not rows:
            print("  (none — batched delivery never created a batch)")
        for bid, chat_id, created, notified, opened, closed in rows:
            print(
                f"  id={bid} chat={chat_id} created={created}"
                f" notified={notified} opened={opened} closed={closed}"
            )


asyncio.run(main())
