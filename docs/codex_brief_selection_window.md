# Codex brief — Selection window + decision backfill migration (incident fix)

## Context: what went wrong in production

Migration `d8e9f0a1b2c3` (5b) added `decisions.profile_name` as nullable but **did not backfill
historical rows**. The per-user stages select candidates with
`~exists(decision WHERE ... profile_name = <username>)`, so every pre-migration decision
(`profile_name IS NULL`) became invisible and the entire archive looked unprocessed. The first cron
run after deploy re-scored **5766 events** through gpt-4o-mini ($2.07 in one run vs the usual $0.11)
and started re-summarizing June news into fresh digests, which were delivered to the user in July.

The data was repaired in production with `audit/fix_reprocessed_archive.py` (backfill +
duplicate/stale digest purge). This brief makes the fix permanent in code, and closes the deeper hole
the incident exposed.

## Two changes

### 1. Backfill migration (make the data fix permanent)

The production DB was repaired by hand. Any other environment (dev DBs, a restored snapshot, a future
clone) still has the same landmine. Add a migration that performs the same backfill, chained off head
`e9f0a1b2c3d4`:

```sql
UPDATE decisions
SET profile_name = :default_profile
WHERE profile_name IS NULL
  AND stage_name IN ('keyword_filter', 'relevance', 'verify', 'summarize');
```

- The value is the single pre-multi-user profile — read `settings.profile_name` (default
  `"volodymyr"`) rather than hardcoding it. At the time these rows were written the system was
  single-user by construction, so attributing them to that profile is correct.
- Shared stages (`ingest`, `embed`, `cluster`, `consolidate`, `discussion`, `research`) must keep
  `profile_name IS NULL` — do **not** touch them.
- It must be idempotent: on production (already repaired) it updates 0 rows and must not fail.
- `downgrade` is a no-op (we cannot know which rows were NULL before; document that in the docstring).

### 2. Selection window for the per-user stages (the real fix)

**The hole:** `filter`, `score`, `verify`, `summarize` have no recency bound. They will happily
process an event of any age. This was masked while the archive was invisible; the moment it became
visible, months-old events flowed into selection and produced digests about June news in July. The
same failure returns after any pause: stop the pipeline for a week, restart it, and the user gets a
week of stale news delivered as if fresh.

**The change:** add a config setting

```python
selection_window_hours: int = 72
```

(env `SELECTION_WINDOW_HOURS`; document in `.env.example`.)

In **each** of `filter`, `score`, `verify`, `summarize`, add to the candidate query:

```python
EventModel.last_seen_at >= datetime.now(UTC) - timedelta(hours=settings.selection_window_hours)
```

Notes:

- Use `last_seen_at` (not `created_at`): a long-running story whose cluster keeps absorbing fresh
  articles stays in the window, which is the intended behavior.
- Apply it in the SQL candidate query, not by filtering in Python after the fetch — the point is to
  never load or pay for stale events.
- **Do not** apply a window to the shared stages (`fetch`/`ingest`/`embed`/`cluster`/`consolidate`).
  `consolidate` already has its own `consolidate_window_hours`; leave it alone.
- 72h is deliberately wider than a daily cron: a run that fails for a day or two still catches up,
  but a month-old archive can never re-enter selection. This is an accepted trade-off — after a long
  outage, the missed window is **skipped, not backfilled**. State that in the setting's comment so
  nobody "fixes" it later by widening it to infinity.

## Tests

- An event with `last_seen_at` inside the window is a candidate for each of the four stages; an event
  outside the window is not — one test per stage, so a missed `.where(...)` is caught.
- The window is configurable: with `selection_window_hours` large, an old event becomes a candidate
  again (proves the bound comes from config, not a hardcoded constant).
- Shared stages are unaffected by `selection_window_hours`.
- **Regression test for the incident:** seed decisions with `profile_name IS NULL` for an event,
  run the backfill migration path (or a helper that performs the same update), and assert the event is
  *not* re-selected by `filter`/`score` afterwards. This is the test that would have caught the
  original bug — it requires pre-existing history, which is exactly what the empty test DBs lacked.
- Backfill is idempotent: running it twice updates 0 rows the second time and leaves shared-stage
  decisions NULL.

## Definition of done

- `make lint` and `make test` pass.
- Migration up/down clean from head `e9f0a1b2c3d4`; re-running the backfill is a no-op.
- With `SELECTION_WINDOW_HOURS=72` (default), a run on a DB containing a months-old archive selects
  **only** fresh events — no stale digests are produced.
- Shared stages and `consolidate_window_hours` behavior unchanged.
