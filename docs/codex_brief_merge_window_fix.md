# Codex brief — Fix the merge windows so backlog articles can still be clustered

## The incident this fixes

On 2026-07-19 the user received **six digests about one event** (the appointment of a new Ukrainian
defence minister). Diagnosis from production data (`audit/why_not_merged.py`):

- All six events were created in the same run, `created_at = 2026-07-19 09:58:35`.
- All six articles were published within a 2.6-hour band on **2026-07-16** (18:00–20:40), so every
  event got `last_seen_at ≈ 62h old` at creation time.
- All six cluster decisions are `created_event`. **None** is `attached`.
- The consolidate stage ran in that same run and merged unrelated groups of 9 and 14 events
  successfully — but produced **no decision at all** for these six. It never loaded them.

Root cause, in one sentence: **`created_at` answers "when did we ingest this", `last_seen_at` answers
"when did the news happen" — and both merge stages ask the first question while filtering on the
second column.**

### Where

`engine/stages/cluster.py:59-70` — the nearest-event search:

```python
effective_time = item.published_at or item.fetched_at
window_cutoff = datetime.now(UTC) - timedelta(hours=self.window_hours)
...
.where(EventModel.last_seen_at >= window_cutoff)
```

`last_seen_at` is derived from `effective_time` (the article's publication time). So an event built
from an article published 62h ago is **already outside the 36h window at the moment it is created**.
The next article about the same story cannot see it. Every backlog article becomes a singleton event,
by construction — this is not specific to the cron outage, it happens to any article that arrives more
than `cluster_window_hours` after publication.

`engine/cli/consolidate.py:69-88` — `_load_candidates` has the identical defect:

```python
window_cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
select(Event).where(Event.last_seen_at >= window_cutoff, ...)
```

so the same events are invisible to the second-chance merge as well.

### The window inconsistency this exposed

| stage | window |
| --- | --- |
| `cluster` | `cluster_window_hours = 36` |
| `consolidate` | `consolidate_window_hours = 36` |
| selection (`filter`/`score`/`verify`/`summarize`) | `selection_window_hours = 72` |

Anything **between 36h and 72h old is deliverable but unmergeable**. The six digests sat at 62h,
squarely in that dead zone. Six paid relevance+verify+summarize passes instead of one.

## Changes

### 1. Cluster: make the window time-local, not wall-clock-local

The question the query should ask is "which events are near **this article** in time", not "which
events are near **now**". Replace the `now()`-based cutoff with a window centred on the article's
`effective_time`, testing for interval overlap:

```python
window = timedelta(hours=self.window_hours)
...
.where(
    EventModel.last_seen_at >= effective_time - window,
    EventModel.first_seen_at <= effective_time + window,
)
```

This makes clustering deterministic with respect to *when the news happened* rather than *when we
happened to run*, which is the property we actually want: re-running the pipeline a day later must
produce the same clusters.

Keep `limit(1)` + `similarity >= threshold` as they are — nearest-neighbour ordering is consistent, so
top-1 is sufficient.

### 2. Consolidate: select candidates by ingestion time

`_load_candidates` wants "events we created recently and have not yet filtered or delivered". Two of
those three conditions are already expressed correctly (`~exists(keyword_filter decision)`,
`~exists(digest)`). The time bound should use `created_at` — the ingestion clock — not `last_seen_at`:

```python
Event.created_at >= datetime.now(UTC) - timedelta(hours=window_hours)
```

Do **not** simply widen `consolidate_window_hours`; that would still mis-handle a backlog older than
whatever number is chosen. The column is the bug, not the constant.

### 3. Close the "cluster should have caught it" gap in the candidate band

`engine/cli/consolidate.py:119`:

```python
if min_similarity <= similarity < cluster_threshold:
```

The upper bound assumes any pair at or above `cluster_similarity_threshold` was already merged by
`cluster`. The production data disproves that assumption — events 6056 and 6147 sit at
**similarity 0.935** and were never merged, because the window (not the threshold) separated them.
Drop the upper bound:

```python
if similarity >= min_similarity:
```

Pairs already merged by `cluster` cannot appear here anyway — they are the same event, and
`_candidate_pairs` only pairs *distinct* candidate events. The upper bound therefore removes real
duplicates and protects nothing.

### 4. Enforce the window invariant

Add a validator on `Settings` asserting that the merge windows are at least as wide as the selection
window:

```python
min(cluster_window_hours, consolidate_window_hours) >= selection_window_hours
```

Raise at startup with a message naming the three settings if violated. Rationale: an event that can be
*selected* but never *merged* is exactly the dead zone that produced this incident, and the invariant
makes that configuration unrepresentable. Bump the two defaults to `72` so the shipped config
satisfies it.

## Tests

- **Regression for the incident:** ingest six articles about one story, all with `published_at` set
  72h in the past, in a single run. Assert they end up as **one** event (or, if similarity is below the
  cluster threshold, that `consolidate` merges them into one) — currently this produces six.
- Cluster is time-local: two articles published one hour apart cluster together whether the run
  happens immediately or three days later. This is the property that makes re-runs reproducible.
- Cluster still refuses to merge articles genuinely far apart in time (outside `cluster_window_hours`
  of each other) even when their embeddings are near — the window must still do its job.
- Consolidate loads a candidate whose `last_seen_at` is old but whose `created_at` is fresh; and does
  **not** load an event created long ago (proving the column swap, not just a widened bound).
- A pair at similarity above `cluster_similarity_threshold` is now adjudicated by consolidate rather
  than skipped.
- Settings validation: `selection_window_hours` wider than either merge window raises at startup.

## Definition of done

- `make lint` and `make test` pass.
- The regression test above fails on the current code and passes after the change.
- Re-running the pipeline over an existing backlog produces materially fewer events than articles for
  a story covered by several outlets.
- No migration required — this is query and config only.

## Out of scope

- Cross-day merging of events that already produced a delivered digest (story threading covers that
  presentation-side; true cross-run consolidation is a separate design).
- Any change to the selection/taste ranking. Note for later: every `taste_score` logged in
  `impressions.context` for this delivery was `None` despite `taste_ranking_enabled = True` — worth a
  separate investigation, not part of this brief.
