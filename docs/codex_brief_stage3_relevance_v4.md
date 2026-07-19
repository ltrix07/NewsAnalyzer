# Codex brief — Stage 3: parameterized relevance prompt

Context: `docs/design_generalization.md`. The MVP cohort is Ukrainian citizens living in Europe, so
citizenship is fixed and only residence country varies. This stage makes the selection prompt read the
profile instead of hardcoding one person's life.

Stage 1 (structured profile, `residence_country`) has landed. Stage 2 (source routing) has not, and
this stage does not depend on it.

## Why this is the blocking piece

`engine/llm/prompts/relevance_v3.j2` interpolates only `location`, `citizenship`, `languages`,
`interests`, `not_interested`. Everything that actually decides is literal text — including category D,
"USER'S PROFESSION (retail algorithmic trading)", and a STEP 3 clause rejecting "generic news from
other EU member states". A Ukrainian in Germany who completes the new questionnaire still gets
selection tuned for a Polish-resident trader. Until this changes, onboarding a beta user produces
digests unrelated to their answers.

## What is constant and what varies

Do not rewrite the prompt from scratch. There is a lot of tuning in v3 and most of it is cohort-wide,
not personal. Split it exactly as follows:

| v3 block | disposition in v4 |
| --- | --- |
| A — Ukraine (citizenship) | **constant**, copy verbatim |
| E — EU temporary protection / conscription | **constant**, copy verbatim |
| STEP 2 significance filter (war/combat) | **constant**, copy verbatim — it filters UA-tier content by definition |
| GROUNDING RULES | **constant**, copy verbatim |
| B — "Poland as it affects foreign residents" | **generate** from the residence country's slots |
| C — UA-PL bilateral | **generate** as UA-⟨residence⟩ bilateral |
| STEP 3 reject criteria | generic clauses constant; the **geography clause** generated from {Ukraine, residence country} |
| ANTI-PATTERNS | **port over**, with anti-pattern 2's "Polish migration / cudzoziemcy" made residence-generic |
| D — profession | **drop** as a fixed category |

On the anti-patterns: they assert that Ukraine topics, migration policy, bilateral friction and
EU-integration news are relevant by default. Those are properties of being a Ukrainian expat in the EU,
not of this particular user, so they belong in v4. They would need revisiting only if the cohort ever
widens past Ukrainian citizens.

On dropping category D: occupation is still asked in onboarding and still flows into `interests`
through profile synthesis, which is where a profession-specific interest belongs. A fixed category
exists in v3 only because the single user is a trader; for most fields it would match nothing.

## Country content lives in data

`config/countries.yaml` already carries an empty `residence_prompt_slots` per country. Fill it for
**PL only** — the other four are Stage 4 curation work. Define the slot names so that adding a country
stays a data change; suggested shape, adjust if the prompt reads better otherwise:

- residence-permit and work-authorization terms (PL: `karta pobytu`, `work permit`)
- migration-policy vocabulary (PL: `cudzoziemcy`, `szybka ścieżka`)
- central bank and currency (PL: `NBP`, `PLN`)
- financial regulator (PL: `KNF`)
- bilateral topics with Ukraine (PL: border/transit, aid, UPA/Wołyń disputes)

**Empty slots must degrade to a generic category, not to an empty bullet list.** A category header
followed by nothing is worse than "news about your country of residence insofar as it affects foreign
residents" — the model will either ignore the empty block or invent content for it.

## Users with no residence tier

`residence_country` can be `null` (legacy profile) or `"ZZ"` (unrecognized free-text country) — both
documented in `docs/stage1_profile_questionnaire_deploy.md`. For these:

- Omit categories B and C entirely. Never render a category that refers to a country the profile cannot
  name.
- The STEP 3 geography clause must then be built from Ukraine alone, and must **not** be phrased so
  that it rejects everything outside Ukraine — such a user still gets the whole UA tier, which is the
  entire point of onboarding them.

This is the case that silently produces an empty digest if it is got wrong, so it deserves its own
test rather than a shared one.

## Selecting v3 vs v4

The design document said to keep the existing user on v3 "side by side" with v4. **Implement it as a
single global flag plus an offline comparison instead** — per-user prompt divergence means two live code
paths, two sets of feedback data that cannot be compared, and a long-lived branch in the stage.

- Add `relevance_v4_enabled` to `Settings`, default **false**. When false, behaviour is byte-identical
  to today: `relevance_v3.j2`, `version = "v3"`.
- When true, all users get `relevance_v4.j2` and `version = "v4"`.
- Leave `relevance_v3.j2` untouched on disk. It is the rollback.

Note that `stage_version` does not participate in candidate selection — only the existence of a
`Decision` row for `(stage_name, target_type, target_id, profile_name)` does. Flipping the flag
therefore cannot reprocess the archive, and cannot re-score events that already have a relevance
decision.

## The comparison command

"Proven before flipping" needs a way to see v4's verdicts without delivering them. Add a read-only
command that scores recent events with **both** prompts for a given profile and prints every
disagreement with each verdict's `why`, plus a summary count.

**It must not write any `Decision` rows.** Writing one with `stage_name = "relevance"` would mark those
events as scored and permanently exclude them from the real pipeline. This is the one way this command
can cause damage, so it should be structurally impossible, not merely avoided — print only.

Bound the cost with a required `--limit`; each event costs two LLM calls.

## Tests

- A PL profile renders the residence categories with that country's slot content.
- A profile with `residence_country = None` and one with `"ZZ"` render **no** B/C categories, contain no
  country name other than Ukraine, and still contain the full UA tier.
- A country whose `residence_prompt_slots` are empty renders the generic residence category, not an
  empty one.
- No rendering of v4 contains the string `retail algorithmic trading`, nor the literal `ZZ`, nor an
  empty category header.
- The STEP 2 significance filter, GROUNDING RULES and anti-patterns appear in v4 for every profile —
  these are the blocks whose accidental loss would be invisible in tests that only check the new parts.
- With `relevance_v4_enabled = false`, the stage renders `relevance_v3.j2` and reports `version = "v3"`;
  the existing v3 tests must pass unchanged.
- The comparison command writes no `Decision` rows — assert on the table, not on the code path.

## Definition of done

- `make lint` and `make test` pass; report **executed** and **skipped** counts separately.
- `relevance_v3.j2` is unmodified — verify with `git diff`.
- With the flag off, no observable behaviour changes.
- The PR description shows the rendered v4 prompt for a PL profile and for a `ZZ` profile, so the two
  can be read and judged. This prompt is a product decision as much as a code one; it should not land
  unread.

## Out of scope

- Source routing and per-user corpora — Stage 2.
- Curating feeds or filling `residence_prompt_slots` for DE / CZ / IT / ES — Stage 4.
- Any change to `verify`, `summarize`, or taste ranking.
- Re-scoring events that already have a relevance decision.
