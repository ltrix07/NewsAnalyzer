# Codex brief — Multi-user 5b: per-user selection (pipeline loop)

## Context

5a landed the `users` table and DB-backed profiles with zero behavior change. 5b makes the
**selection half** of the pipeline run per user. Delivery and the listener are **still single-user**
in 5b — that is 5c. With one seeded user, 5b must be behavior-identical to today; its effect is only
visible when a second user exists (each enabled user independently gets full selection and their own
digests).

Split rationale: the delivery/listener fan-out (5c) is a separable concern and independently
testable. Keep it out of this brief.

## The core problem this brief solves

The four per-user stages — `filter`, `score`, `verify`, `summarize` — decide what to process by
checking for the **existence of a prior decision on the event**, with **no profile scoping**. For
example `filter` selects events where no `keyword_filter` decision exists
(`engine/cli/filter.py`), and `score` requires a `passed_keyword_filter` decision and no prior
`relevance` decision (`engine/cli/score.py`), both keyed only on `(stage_name, target_type='event',
target_id)`.

Consequence today if you merely loop users: user A's `filter` writes `keyword_filter` decisions for
the shared events, and user B's `filter` then sees those events as already processed and **skips them
entirely** — B silently gets zero digests. The gate must become per **(event, profile)**, not per
event.

## Change 1 — `Decision.profile_name`

Add a nullable column `profile_name: str | None` to the `Decision` model (`engine/models.py`).
Shared stages (`ingest`, `embed`, `cluster`, `consolidate`, and `fetch`) leave it NULL; the four
per-user stages write the acting user's username. Add an index
`("stage_name", "target_type", "target_id", "profile_name")` — this is the shape every candidate
sub-query below will probe.

`decisions` is append-only with no FKs (by design); a nullable string column is fully compatible.
Migration chains off head `c7d8e9f0a1b2` (`add_users`).

## Change 2 — thread the profile into decision persistence

- Add `profile_name: str | None = None` to `Context` (`engine/stages/base.py`).
- Add `profile_name: str | None = None` to `record_decision` (`engine/observability.py`) and write it
  onto the `Decision` row.
- In `Stage.run` / `Stage.run_batch`, pass `ctx.profile_name` into `record_decision`.

No change to `DecisionDraft` or any stage's `process()` — the profile rides on the context, not the
draft. The four per-user CLI commands set `Context(..., profile_name=<username>)`.

## Change 3 — profile-scope the candidate queries

In **each** of `filter`, `score`, `verify`, `summarize`, every `exists()` / `~exists()` sub-query
that gates candidate selection — both the stage's own "already processed" check **and** its upstream
dependency check — must additionally match `Decision.profile_name == <profile>`.

Concretely:

- `filter`: "no `keyword_filter` decision **for this profile**".
- `score`: "a `passed_keyword_filter` decision **for this profile** exists" AND "no `relevance`
  decision **for this profile**". (Both must be scoped — otherwise B's score rides on A's keyword
  pass.)
- `verify`: the `relevance`-exists and `verify`-exists checks, and the latest-`relevance` lookup, all
  scoped to the profile.
- `summarize`: same pattern — its upstream (`verify`/relevance) existence checks and its own
  already-summarized check scoped to the profile.

Miss any one of these and you reintroduce cross-user leakage. A test must cover two users getting
independent, complete selection over the same events (see Tests).

**Do not scope `consolidate`.** It runs once, before the per-user loop, and intentionally treats an
event as "already in selection" if *anyone* has filtered it. Its `~exists(keyword_filter)` /
`~exists(Digest)` candidate query stays global (unchanged). Leaving it global is correct, not an
oversight — call this out so it is not "helpfully" scoped.

## Change 4 — loop the per-user tail in `run_once`

`run_once` (`engine/pipeline.py`) currently threads a single `profile_name` into `_stage_calls`.
Restructure so:

- Shared stages (`fetch → consolidate`) run **once**.
- The per-user tail (`filter, score, verify, summarize`) runs **once per user**:
  - If `profile_name` is explicitly passed (CLI override) → run the tail for that one user only
    (preserves today's single-user CLI invocation).
  - If `profile_name` is `None` (the cron default) → load `list_enabled_users(session)` and run the
    tail for each, passing that user's username as the profile.

Decisions all share the run's `run_id`; `_apply_decision_rollups` aggregates across users, so the
`RunSummary` reports run totals (per-user cost can be derived from `decisions.profile_name` later —
out of scope here). Keep the per-stage `StageOutcome` buckets; a per-user stage's outcome is the sum
across users for that run.

If there are zero enabled users, the per-user stages are `skipped` (not an error).

## Change 5 — finish the 5a rewire in `verify`

`engine/cli/verify.py` still imports and calls `load_profile` (YAML) — a 5a leftover. Switch it to
`resolve_profile(profile or settings.profile_name, session)` like the other three stages. If the
resolved profile is genuinely unused by verify, drop the call rather than leave a dead YAML read.

## Out of scope (5c and later)

- No delivery changes: `deliver_pending` still uses `settings.require_telegram_chat_id()`.
- No listener changes.
- No per-user taste vector or per-user `ui_language` yet (those move in 5c).
- No source subscriptions (later).

## Tests

- **Two-user independence (the load-bearing test):** seed two enabled users with different profiles,
  cluster a set of shared events, run the per-user tail for both. Assert each user gets `filter` /
  `relevance` decisions for the **same** full event set (neither starves the other), and that digests
  are produced per user (`digests.profile_name` distinct). This is the test that would fail under the
  naive loop.
- `Decision.profile_name` is populated for per-user stages and NULL for `consolidate` / shared stages.
- Single explicit `profile_name` still runs exactly one user's tail (CLI path unchanged).
- `run_once` with `profile_name=None` and one enabled user reproduces today's output
  (behavior-preserving).
- `run_once` with zero enabled users marks the per-user stages `skipped`, no error.
- `consolidate` candidate selection is unchanged (a regression test asserting it still ignores
  profile — an event filtered by one profile is still not re-consolidated).

## Definition of done

- `make lint` and `make test` pass.
- Migration up/down clean from head `c7d8e9f0a1b2`.
- With one seeded user, pipeline output is identical to today.
- With two enabled users, both get complete, independent selection over the same events — proven by
  the two-user test.
- Delivery and the listener are untouched.
