# Codex brief — Event consolidation stage (story merge)

## Goal

One real-world event currently arrives as many near-duplicate digests. Example (real
production data, one morning): the same Kyiv mass-attack was delivered as 8 separate
digests ("Российская атака на Киев", "Повреждено 30 объектов в Киеве", "Атака на Киев
оставила 13 погибших", "Значительный атака на Киев: 25 жертв", …). Root cause: the
online clustering stage embeds title+lead and merges only at cosine similarity ≥ 0.82,
so different *facets* of one event (damage count vs death count vs "massive attack")
never join into one `Event`.

Add a new pipeline stage — **`consolidate`** — that runs after `cluster` and before
`filter`, and merges `Event` rows that describe the same underlying story into one
canonical event. Downstream (`filter → score → verify → summarize`) then sees a single
event, produces a single digest, and runs the expensive gpt-4o `verify`/`summarize`
once per story instead of once per fragment.

This is the highest-leverage fix: it removes the visual duplicate spam AND cleans the
feedback data (one event → one 👍/👎 label instead of 8 contradictory ones).

## Non-goals (do NOT do these here)

- Do NOT change the online `cluster` stage or its 0.82 threshold. Real-time clustering
  stays as-is; consolidation is a separate second pass.
- Do NOT touch ranking/selection/taste. That is a later, separate task.
- Do NOT add a DB migration. The design below is migration-free (see "Merge mechanics").
- Do NOT use gpt-4o here. Adjudication must use the cheap model (gpt-4o-mini) to keep
  COGS negligible.

## Where it plugs in

1. `engine/pipeline.py`: insert `"consolidate"` into `STAGE_ORDER` between `"cluster"`
   and `"filter"`. Add it to `_stage_calls` as
   `"consolidate": partial(consolidate_command, run_id=run_id)`.
   `DECISION_STAGE_TO_PIPELINE_STAGE` needs no entry (stage name == pipeline stage name).
2. New file `engine/cli/consolidate.py` with `async def consolidate_command(...)` and a
   `consolidate_command_sync_wrapper(...)`, mirroring `engine/cli/cluster.py` /
   `engine/cli/filter.py` structure (Typer options, `session_scope`, `Context`,
   per-decision logging, echo summary line).
3. Register the Typer command wherever the other `*_command_sync_wrapper`s are wired
   (check `engine/__main__.py` / `engine/cli/*` registration and match the existing
   pattern) so `uv run python -m engine consolidate` works standalone too.

## Candidate set

Mirror `filter_command`'s selection so consolidation sees exactly the events `filter`
is about to process, one step earlier. Candidates = `Event` rows that:

- have NO `keyword_filter` decision yet (same `~exists(...)` predicate as
  `engine/cli/filter.py:44`), AND
- have NO `Digest` row (`~exists(select(1).where(Digest.event_id == Event.id))`), AND
- `last_seen_at >= now() - consolidate_window_hours`.

This keeps consolidation to fresh, not-yet-processed, not-yet-delivered events (in a
daily run, effectively this run's new events). Never merge an event that already has a
digest — that would corrupt already-delivered history.

## Algorithm

1. **Load candidates** (above). If fewer than 2, exit early (echo `merged=0`).
2. **Generate candidate pairs by embedding proximity.** For each candidate event, find
   other candidate events whose centroid cosine similarity is
   `>= consolidate_candidate_min_similarity` (default 0.60) and `< cluster_similarity_threshold`
   (0.82 — anything ≥ that would already be one cluster). Use pgvector
   `Event.centroid.cosine_distance(other_centroid)`; `similarity = 1 - distance`. To bound
   LLM calls, keep at most `consolidate_max_neighbors` (default 5) nearest neighbors per
   event. Deduplicate unordered pairs (i<j).
3. **Adjudicate each pair with gpt-4o-mini.** Ask whether the two events are the same
   underlying news event (same incident, same day). Use structured output (see "LLM"
   below). Only pairs answered `same_event: true` become merge edges.
4. **Union-find over the `true` edges** to form merge groups. A group of 8 mutually
   similar Kyiv fragments collapses via transitive edges even if not every pair was
   adjudicated.
5. **Merge each group** (see mechanics) into one canonical event.
6. **Record one decision per merge** (see logging) and echo a summary line.

## Merge mechanics (migration-free)

For each group, pick the **canonical** event = highest `article_count` (tie → lowest
`id`). For every other (absorbed) event in the group:

- Reassign members: `UPDATE event_members SET event_id = <canonical> WHERE event_id = <absorbed>`.
  (`article_id` stays globally unique, so no conflict.)
- Update canonical aggregates:
  - `centroid` = article-count-weighted mean of canonical and absorbed centroids
    (weights = their `article_count` before this step). Keep it a plain `list[float]`
    of length 1536.
  - `article_count` = sum.
  - `first_seen_at` = min, `last_seen_at` = max.
- Delete the absorbed `Event` row. Safe because: absorbed events have no `Digest`
  (candidate filter guarantees it), their members are already reassigned, and the
  `decisions` table has no FK (stale decision rows referencing the deleted id are
  harmless and downstream stages select `FROM events`, so the row simply disappears).

Do all of this inside the existing `session_scope()` transaction.

## Config (`engine/config.py`)

Add, with sensible defaults, next to the cluster settings:

```python
consolidate_enabled: bool = True
consolidate_window_hours: int = 36
consolidate_candidate_min_similarity: float = 0.60
consolidate_max_neighbors: int = 5
openai_model_consolidate: str = "gpt-4o-mini"
```

When `consolidate_enabled` is False, `consolidate_command` must no-op cleanly (echo
`merged=0 (disabled)`) so it can be turned off without editing the pipeline.

## LLM adjudication

- Add a Pydantic schema in `engine/llm/schemas.py`, e.g.
  `class SameEventVerdict(BaseModel): same_event: bool; reason: str | None = None`.
- Add a Jinja template `engine/llm/prompts/consolidate_v1.j2`. Inputs: for each of the
  two events, a compact representation = its member titles (and a short lead snippet)
  built from `load_event_articles(session, event_id)` (cap to ~3 member articles per
  event, most-central first, to keep tokens small). Instruction: decide if BOTH clusters
  report the SAME underlying real-world event (same incident/attack/announcement, same
  day) — not merely the same topic or theme. "Two different cities struck" = NOT the
  same event. "Same Kyiv attack described by two outlets / with different casualty
  figures" = the same event. Output a single `SameEventVerdict`.
- Call via `make_llm_client(settings)` + `client.call_structured(model=settings.openai_model_consolidate,
  system=..., prompt=rendered, output_schema=SameEventVerdict, max_tokens=256)`, exactly
  like `engine/stages/summarize.py:62`. Sum `response.usage` tokens/cost into the
  decision rows so the run summary reflects consolidation cost.
- Make the adjudicator injectable so tests can supply a deterministic fake (accept an
  optional adjudicate callable/dependency, defaulting to the LLM-backed one). Do not hit
  the network in unit tests.

## Decision logging

For each performed merge, write a decision row (use `record_decision` /
`DecisionDraft`, `target_type="event"`, `target_id=<canonical>`):

```json
{"action": "merged", "canonical_event_id": <id>, "absorbed_event_ids": [...], "group_size": n}
```

Set `stage_name="consolidate"`, `stage_version="v1"`, and attach `model` + token/cost
from the mini calls (attribute the group's adjudication cost to its canonical decision;
splitting exactly is unnecessary). For candidates that were evaluated but NOT merged, no
decision is required (keep it lean). Echo:
`Run <id>: merged=<groups> absorbed=<events> pairs_judged=<n>; cost=$...; elapsed=...s`.

## Guarding summarize token growth

A merged event can now carry many member articles. Confirm `load_event_articles` +
`summarize_v3.j2` stay within a reasonable prompt size; if a merged event exceeds, cap
the member articles passed into the summarize prompt (e.g. top ~8 by recency/centrality)
in `engine/stages/summarize.py`'s article loading. Keep this change minimal and only if
needed — note it, don't over-engineer.

## Tests (`tests/`)

Follow existing stage-test style. Add:

1. Union-find grouping: given a fake adjudicator returning fixed verdicts, 8 mutually
   similar events collapse into 1 canonical; an unrelated event stays separate.
2. Merge mechanics: members reassigned to canonical, absorbed events deleted,
   `article_count` summed, `first/last_seen_at` min/max, centroid length still 1536.
3. Candidate exclusion: an event that already has a `Digest` is never merged/deleted.
4. `consolidate_enabled=False` → no-op, no events touched.
5. End-to-end-ish: after consolidate, `filter`'s candidate query returns only canonical
   events (absorbed ones are gone).

Use the deterministic injected adjudicator — no live OpenAI calls in tests.

## Acceptance criteria

- `uv run python -m engine consolidate` runs standalone and via `engine run`.
- On a dataset with N fragments of one story, exactly one canonical event survives with
  all members; downstream produces exactly one digest for it.
- `make lint` (ruff + mypy over engine+delivery) and `make test` pass.
- No Alembic migration added. `consolidate_enabled=False` fully bypasses the stage.

## Notes for the reviewer (me) — not for Codex to implement

- Alternative non-destructive design (status='merged' + `merged_into_event_id` column +
  exclude non-open events in 4 candidate queries) was rejected for v1 because it needs a
  migration and touches every downstream stage. Revisit only if we want merge provenance
  beyond the decision log.
- After this ships and data homogenizes, the kNN/example-based selection gate is the next
  task (separate brief).
