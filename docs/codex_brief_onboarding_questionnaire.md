# Codex brief — Onboarding questionnaire → synthesized profile

## Why

For the beta we cannot hand-write a profile YAML for 30 people. A user should press `/start`, answer
a short questionnaire in Telegram, and have their `users.profile` synthesized from the answers. This
is the feature that makes the beta scalable and, more importantly, the one that determines selection
quality — the bottleneck in every feedback analysis so far.

## Decisions already made (build to these)

- **Operator invite, not open registration.** The listener ignores unknown chats (5c). A person can
  only onboard if the operator has invited their `chat_id` first. No open `/start`.
- **Profile only.** The questionnaire synthesizes `interests` / `not_interested` (and the hard facts).
  **Per-user source subscription is a separate later brief** — do not build `user_sources`, topic→source
  mapping, or any fetch/selection change here. Every onboarded user still sees the shared event set,
  scoped by their profile via the existing relevance stage.
- **~10 questions**, buttons for hard facts, free text for taste.

## User lifecycle (three states, expressed with existing fields)

`users` already has `profile` (made **nullable** by this brief) and `enabled`. Encode state without a
new enum:

| state      | `profile`  | `enabled` | listener behavior              | delivery (`list_enabled_users`) |
| ---------- | ---------- | --------- | ------------------------------ | ------------------------------- |
| invited    | NULL       | false     | onboarding flow only           | excluded (enabled=false)        |
| active     | NOT NULL   | true      | normal feedback/discussion     | included                        |
| disabled   | NOT NULL   | false     | ignored                        | excluded                        |

Listener guard (extends 5c's `get_user_by_chat_id`):

- user is `None` → ignore (not invited).
- user with `profile IS NULL` (invited) → route **only** to the onboarding handler; ignore normal
  feedback callbacks.
- user `enabled` with profile → normal flow (as 5c).
- user not enabled **with** profile (disabled) → ignore.

### Migration

- `users.profile` → nullable. Chain off head `d8e9f0a1b2c3`. `resolve_profile` must raise a clear
  error if called on a NULL profile (it only ever runs for enabled users, so this is a guard, not a
  path).
- No other schema change to `users`.

## Onboarding state

New table `onboarding_state`, one row per in-progress questionnaire (mirrors `discussion_pending`):

| column         | type                   | notes                                   |
| -------------- | ---------------------- | --------------------------------------- |
| `chat_id`      | BigInteger PK          | the invited user                        |
| `step`         | Integer, not null      | current question index                  |
| `answers`      | JSONB, not null        | accumulated answers so far              |
| `created_at`   | timestamptz, not null  | `now()`                                 |
| `updated_at`   | timestamptz, not null  | `now()`, bumped each answer             |

Deleted when onboarding completes or is restarted. Same migration.

## Operator CLI

Add to the existing `users` app:

- `users invite --username <slug> --chat-id <int> [--ui-language ru]` — creates an **invited** row
  (`profile=NULL`, `enabled=false`). Fails if username/chat_id already exists. This is the only new
  entry point; `users add` (full profile, immediately active) stays for the operator's own account and
  testing.

The operator's flow: `users invite`, tell the person to open the bot and press `/start`.

## The questionnaire flow (in the listener)

A state machine driven by `onboarding_state`, handled **before** the normal feedback routing, only for
invited users.

1. `/start` from an invited user with no `onboarding_state` → create the row at step 0, send a one-line
   welcome + question 1. If an `onboarding_state` already exists, `/start` restarts it (reset to step 0)
   so a stuck user can recover.
2. Each answer (button callback or text message, depending on the question) is validated, written into
   `answers`, `step` advances, next question is sent.
3. After the last question → synthesize the profile (below), show the user a **plain-language summary**
   of what the bot understood (not raw JSON), with a single confirm button `t("onboarding_confirm")`.
4. On confirm → write `users.profile`, set `enabled=true`, delete `onboarding_state`, send a "you're
   all set, digests start with the next run" message. The user is now **active**.

Use inline keyboards for the button questions (new callbacks in `keyboards.py`, typed like the rest;
prefix e.g. `onb:`). Free-text answers arrive as messages and are matched to the pending
`onboarding_state` the same way `discussion_pending` matches a message.

Onboarding UI language: use the invited user's `ui_language` (set at invite time). All questionnaire
strings go through `t(...)` with ru + en, like every other user-facing string.

### The questions (~10)

Hard facts — **buttons** (deterministic, the LLM must not guess these):

1. Country of residence — Poland / Ukraine / other EU / other.
2. Citizenship — Ukraine / Poland / other.
3. Reading languages — multi-select — UA / RU / PL / EN (at least one).
4. Digest language — RU / UK / PL / EN → becomes `profile.output_language` **verbatim**, not
   LLM-decided.
5. Rough field / occupation — a small fixed set (IT / finance-trading / business-owner / student /
   other) — feeds `interests` synthesis.
6. Legal situation in PL (if resident) — work permit / karta pobytu / studies / citizen / n-a — sharpens
   `pl_legal` relevance. Skippable.

Taste — **free text** (this is where quality comes from):

7. "What do you most want to not miss?" → seeds `interests`.
8. "What annoys you / should never be sent?" → seeds `not_interested`. **Ask this as its own explicit
   question** — every feedback analysis shows `not_interested` carries the signal.
9. (optional) "Anything specific about your situation we should know?" → free context for synthesis.

`profile.name` is taken from the Telegram `first_name` on the `/start` message (no question spent on
it); fall back to the username if absent.

## Profile synthesis (LLM)

Add a synthesis step (new prompt `engine/llm/prompts/onboarding_profile.j2`, structured output like the
summarize stage) that takes the `answers` dict and returns a validated `Profile`:

- `location`, `citizenship`, `languages`, `output_language` — taken **directly** from the button
  answers, not synthesized. The LLM must not override deterministic facts.
- `interests`, `not_interested` — synthesized from the free-text answers into clean, deduplicated
  lists of short phrases. Preserve the user's meaning; do not invent interests they did not express.
- `keyword_rules` — **leave empty** (`keep_if_matches: []`, `drop_if_matches: []`). Do not
  auto-generate regex. An over-broad generated pattern silently drops relevant events with no signal to
  anyone; the recall-first `filter_rules` design deliberately prefers to let the relevance LLM decide.
  Regex rules are added later, deliberately, by the operator — not guessed from a questionnaire.
- The result is validated with `Profile.model_validate`; on validation failure, log it and ask the user
  a single retry / fall back to a minimal valid profile rather than crashing the flow.

The synthesis prompt must state plainly: hard facts are passed through as given; only the two taste
lists are the model's job; output must conform to the Profile schema.

## Funnel metrics

The first thing the beta must measure is **how many invited users finish onboarding** — the earliest
drop-off point. Log to `ui_events` (reuse the table; `digest_id` NULL) at:

- `onboarding_started` (on `/start`),
- `onboarding_step` with `context={"step": n}` on each answered question,
- `onboarding_completed` on confirm,

so an `audit/onboarding_funnel.sql` can show invited → started → per-step retention → completed. Add
that SQL file with a comment naming the key number: completion rate over invited.

## Strings

Add ru + en for every question, the button labels, the welcome, the summary template, the confirm
button, and the completion message. `t()` fallback behavior unchanged.

## Tests (no network — stub the LLM synthesis)

- `users invite` creates an invited row (profile NULL, enabled false); rejects duplicates.
- Listener ignores a non-invited chat; routes an invited chat's `/start` into onboarding; ignores a
  disabled user.
- Full happy path with a stubbed synthesizer: `/start` → answer each question (buttons + text) →
  summary → confirm → `users.profile` populated, `enabled=true`, `onboarding_state` gone, user now
  active. Assert `output_language`/`location`/`citizenship`/`languages` equal the button answers exactly
  and `keyword_rules` are empty.
- Invited user's normal feedback callback (e.g. a like) is NOT processed as feedback before onboarding
  completes.
- `/start` mid-onboarding restarts cleanly.
- Synthesis returning an invalid profile does not crash the flow (retry/fallback path).
- `onboarding_started` / `onboarding_step` / `onboarding_completed` rows land in `ui_events`.
- An onboarded (active) user goes through the normal 5c delivery path and receives digests.

## Definition of done

- `make lint` and `make test` pass.
- Migration up/down clean from head `d8e9f0a1b2c3` (`users.profile` nullable + `onboarding_state`).
- An operator can `users invite` a chat_id; that person completes `/start` onboarding and becomes an
  active user whose synthesized profile drives selection — with no hand-written YAML.
- Deterministic facts come from buttons, taste from free text, `keyword_rules` empty.
- No `user_sources`, no pipeline/fetch changes — profile synthesis only.
- Existing single-user and multi-user behavior unchanged for already-active users.
