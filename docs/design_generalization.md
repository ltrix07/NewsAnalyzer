# Design — Generalizing from one user to the Ukrainian-expat cohort

Decision taken 2026-07-19, revised the same day after scoping. The MVP targets **Ukrainian citizens
living in Europe**. Citizenship is therefore fixed; only country of residence varies. This document is
the architecture and the sequencing. Individual Codex briefs follow per stage.

## Why the fixed cohort makes this much cheaper

The earlier framing ("support any country") required parameterizing the whole selection prompt. Fixing
citizenship to UA collapses most of that, because the corpus splits into two tiers with very different
requirements:

| tier | content | who it serves | status |
| --- | --- | --- | --- |
| **UA tier** | Ukraine government/economy, mobilization, EU integration, temporary protection, war | **every** user in the cohort, regardless of residence | already covered (2 feeds), already tuned |
| **residence tier** | local migration regime, central bank, taxes, attitudes toward Ukrainians, bilateral relations with UA | one country's users only | exists for **PL only** (4 feeds) |

A Ukrainian in Portugal is therefore not an empty product — they get the entire UA tier and nothing
local. That is shippable, provided onboarding says so out loud instead of implying full coverage.

**Consequence for the prompt.** `relevance_v3.j2`'s categories map onto the tiers:

| block | tier | disposition |
| --- | --- | --- |
| A — Ukraine (citizenship) | UA | **constant**, keep verbatim |
| E — EU temporary protection / conscription | UA | **constant**, keep verbatim |
| B — "Poland as it affects foreign residents" | residence | parameterize: PL specifics (karta pobytu, NBP, KNF) are instances of generic slots — residence-permit regime, central bank / currency, financial regulator |
| C — UA-PL bilateral | residence | parameterize to UA-⟨residence⟩ |
| D — profession (retail algo trading) | neither | drop as a fixed category (see open question 3) |
| STEP 2 significance filter | UA | constant — it filters war coverage, which is UA-tier by definition |
| STEP 3 rejects | mixed | keep the generic clauses; generate the geography clause from residence country |
| ANTI-PATTERNS | UA + residence | **carries over** — see below |
| GROUNDING RULES | generic | keep verbatim |

Correction to the earlier draft of this document: it claimed the anti-patterns block encodes one
person's taste and must not be shipped to other users. With citizenship fixed, that is mostly wrong.
Anti-patterns 1–4 assert that Ukraine topics, migration policy, bilateral friction and EU-integration
news are relevant *by default* — those are properties of being a Ukrainian expat in the EU, not of
being this particular user. The block ports over. Revisit it if and when the cohort widens past
Ukrainian citizens; that is the point where it becomes one cohort's taste imposed on another's.

What remains genuinely per-user is the taste tuning that lives in `interests` / `not_interested` and
the deferred kNN gate — not this prompt.

## The questionnaire

Buttons for the 5 largest Ukrainian-diaspora countries in Europe plus "другая" → free text.

Recommended five, by post-2022 Ukrainian population: **Poland, Germany, Czechia, Italy, Spain**
(UK and Netherlands are the near runners-up — swap on cohort evidence, not on instinct). Note that of
these five, **only Poland has local feeds today**; the other four are UA-tier-only until Stage 4
curation. The buttons must not imply otherwise.

Changes to the flow:

- **Add a gate question first: "Вы гражданин Украины?"** Non-UA users get an honest "пока не для вас"
  message and are not onboarded. This is what makes the fixed-cohort assumption safe — without it the
  prompt silently treats everyone as Ukrainian.
- **Drop the free citizenship question** (`_BUTTON_QUESTIONS[1]`) — it is answered by the gate. One
  fewer step in a funnel whose drop-off is a tracked metric.
- **"Другая" opens a free-text follow-up.** It must never persist the literal string `"other"`, which
  is what happens today (`onboarding.py:271-272` copies the button value verbatim, so the prompt
  renders `Location: other`). Store the typed country; also store it as demand data — the free-text
  answers are the input for choosing which country to curate next.
- **An unsupported country gets an explicit expectation-setting message** before the questionnaire
  continues: Ukraine coverage yes, local coverage not yet. Do not silently onboard.
- **The legal-status question** (`_BUTTON_QUESTIONS[5]`: karta pobytu / work permit) becomes
  conditional on residence country, with a per-country option set, and is skipped entirely for
  unsupported countries.

## The cost argument that sets the order

`engine/cli/score.py:31-68`: the LLM relevance call runs for every event that passed the keyword
filter, per user. `synthesize_profile` always sets `keyword_rules` empty (`onboarding.py:275`, and the
synthesis prompt mandates it), so **onboarded users have no pre-filter at all** — the entire 72h corpus
reaches the LLM for each of them.

Affordable at 8 feeds. Not affordable at 5 countries × 3-5 feeds: the event set grows several-fold and
every user pays relevance on all of it, including four countries' domestic news they will never want.
Measured cost is ~$2–3/month/profile today (`docs/commercial.md`); this scales it with the number of
supported countries while adding nothing.

The tier split gives the fix directly: a user needs **UA tier + their own residence tier**, never
anyone else's. Route on source metadata before the LLM call and per-user volume stays roughly flat as
countries are added. **This must land before the corpus grows**, or the bill arrives precisely during
the window when the beta is being judged on unit economics.

## Sequencing

### Stage 1 — Structured profile + honest questionnaire

Foundation; no behaviour change for the existing user.

- Add `residence_country` (ISO-3166-2) to `Profile`, alongside the human-readable `location`. The code
  is what Stage 2 routes on; free strings cannot match `sources.country`.
- Citizenship gate, dropped citizenship question, free-text "другая", conditional legal-status
  question, expectation-setting for unsupported countries — per the section above.
- `users.profile` is JSONB so this is additive, but existing rows lack the new field: either backfill
  or make it optional with a derivation from the existing string.

### Stage 2 — Source routing as a cheap pre-filter

Must precede any corpus growth.

- Derive each user's allowed source set from the profile: **UA tier + international tier + their
  residence country**.
- Filter score candidates on source metadata **before** the LLM relevance call.
- Wrinkle: an event is a cluster of articles from potentially several sources. Route on "**any** member
  article matches" — routing on a single source silently drops multi-country stories, which are exactly
  the high-value ones (category C, UA-⟨residence⟩ bilateral, is precisely that shape).
- `sources.lang` / `country` / `topics` are already populated and stored (`engine/models.py:59-61`) but
  read by nothing outside `engine/cli/sources.py` display — this stage is what makes them load-bearing.
- Decide in the brief whether to derive the set from the profile or to materialize `user_sources`
  (deferred in the beta checklist).

### Stage 3 — Parameterized relevance prompt

- `relevance_v4.j2`: constant UA-tier blocks kept verbatim, residence-tier categories rendered from
  `residence_country`. Bump the stage version — note `stage_version` does **not** participate in
  candidate selection, so this cannot reprocess the archive.
- Keep `relevance_v3.j2` and keep the existing user on it until v4 is proven side by side. There is a
  lot of tuning in v3 and a regression is invisible until digests quietly get worse days later.
- Generic slots for the residence tier: residence-permit regime, work authorization, central bank /
  currency, financial regulator, tax changes affecting foreigners, public attitudes toward Ukrainians,
  bilateral relations with UA. Filling those per country is content, not code — keep it in a data file
  so adding a country stays a data change.

### Stage 4 — Expand the source pool

Curation, not code. Per supported country: 3-5 feeds with correct `lang`/`country`/`topics`, validated
with `engine sources validate`. Only after Stage 2 is live. Prioritize by where the beta cohort
actually lives and by the free-text "другая" answers from Stage 1.

## Open questions

1. **Does the existing single user stay on `relevance_v3`** until v4 is proven? Recommended yes — his
   is the only profile with a feedback history worth anything.
2. **Profession as a fixed category** — category D exists because the user is a trader. Generically most
   fields never intersect the news. Recommended: drop the fixed category, let profession flow into the
   free-text taste answers and hence into `interests`.
3. **Which country gets curated after Poland** — answer from Stage 1 demand data, not now.

## Account menu — decisions taken, brief pending

Agreed 2026-07-19; the brief waits until Stage 1 lands so it can reuse the question descriptors rather
than guess at their shape.

Contents: view the current profile; edit individual answers; change delivery slot and timezone;
**pause / resume**. Pause is not optional — without it the only way a user can stop the flow is to
block the bot, which converts a temporary lapse in interest into a permanent loss. `users.enabled`
already expresses it.

Editing an answer re-asks exactly one question. This is why Stage 1's descriptors must support
single-question re-ask, not just linear traversal.

**On edit, invalidate `relevance` decisions inside the selection window, limited to once per day.**
`score` gates on the existence of a `Decision` for `(stage, target, profile_name)` and `profile_name`
is the username — which does not change when profile *content* does. Without invalidation a user edits
their interests, sees an identical digest, and concludes the menu is broken. The daily limit is what
stops someone re-running paid LLM scoring in a loop by toggling settings.

Delivery slot mechanics live in `docs/codex_brief_delivery_schedule.md`, which is independent of
Stage 1 and can be built in parallel.

## Out of scope / independent

- Merge-window fragmentation fix — shipped (`docs/codex_brief_merge_window_fix.md`).
- Onboarding callback acknowledgement bug (`docs/codex_brief_onboarding_multiselect_ack.md`) — **folded
  into Stage 1** rather than shipped separately, since Stage 1 rewrites `_BUTTON_QUESTIONS` and
  renumbers the steps; two independent passes over `delivery/onboarding.py` would conflict. That brief
  is now an input to the Stage 1 brief, not a standalone task. Its test requirement — every callback
  path answered exactly once — must survive the merge; it is the only thing preventing the same defect
  from shipping again with the new questionnaire. Consequence: live onboarding testing stays broken
  until Stage 1 lands.
- The kNN taste gate — still sequenced after fragmentation work.
