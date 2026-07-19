# Codex brief — Stage 1: structured profile and cohort-honest questionnaire

Context: `docs/design_generalization.md`. The MVP cohort is **Ukrainian citizens living in Europe**;
citizenship is fixed, residence country varies. This stage builds the profile structure and the
questionnaire that Stage 2 (source routing) and Stage 3 (parameterized relevance prompt) depend on.
It changes onboarding only — no selection or delivery behaviour changes here.

This brief **supersedes** `docs/codex_brief_onboarding_multiselect_ack.md`; that brief's fix is folded
in below (section 4) because both rewrite the same state machine.

## 1. The state machine cannot stay index-keyed

`delivery/onboarding.py` maps questions to a bare integer `step` through three parallel dicts
(`_BUTTON_QUESTIONS`, `_TEXT_KEYS`, `_ANSWER_KEYS`) and hardcodes the flow in `_advance`:

```python
if state.step <= 5:      # button question
elif state.step <= 8:    # free-text question
else:                    # synthesize
```

This stage introduces **conditional questions** — the legal-status question depends on residence
country, and "другая страна" opens a free-text follow-up. A static step→question map cannot express
that, and three dicts that must be kept in sync by hand is already the kind of structure where a
skipped step silently reads the wrong answer key.

Replace it with one ordered list of question descriptors, each carrying: its answer key, its kind
(single-select / multi-select / free text), its options or prompt string key, and an **applicability
predicate** over the answers collected so far. `_advance` moves to the next *applicable* question and
synthesizes when none remain. Keep `OnboardingState.step` as the index into that list — no migration
needed, and skipping is just advancing past.

Keep the existing `onb:{step}:{value}` callback format and the `int(parts[1]) != state.step` guard;
they still do their job.

**Deploy hazard:** in-flight `onboarding_state` rows carry step indices under the *old* numbering and
will point at different questions after deploy. The cohort is tiny — delete in-flight rows as part of
the rollout and say so in the deploy notes. Do not attempt to migrate them.

## 2. Questionnaire changes

New order (all strings go through `t()` in `delivery/strings.py`, ru + en, following the existing
table):

1. **Citizenship gate — "Вы гражданин Украины?"** New, first. A "нет" answer ends onboarding with an
   honest "пока не для вас" message. Do **not** populate the profile, do **not** enable the user, and
   do **not** delete their `users` row — an operator created it. Record a `UIEvent` so the funnel audit
   can count this outcome distinctly from an abandonment.
2. **Residence country** — five buttons plus "другая": **Poland, Germany, Czechia, Italy, Spain**
   (chosen by post-2022 Ukrainian diaspora size). "другая" opens a free-text follow-up question.
3. ~~Citizenship~~ — **removed**, answered by the gate. One fewer step in a funnel whose drop-off is a
   tracked metric.
4. Languages (multi-select) — unchanged except for the fixes in section 4.
5. Output language — unchanged.
6. Occupation — unchanged.
7. **Legal status** — now conditional: shown only for countries with a defined option set, skipped
   entirely otherwise. Options are per country (Poland keeps today's karta pobytu / work permit /
   studies / citizen).
8. wanted / unwanted / context free text — unchanged.

### Expectation setting for unsupported countries

Local coverage exists **only for Poland** today; the other four buttons are Ukraine-tier only. After a
residence answer whose country has no local sources, send a short message stating plainly what the user
will and will not receive (Ukrainian news yes, local news not yet), then continue the questionnaire.
Onboard them — the Ukraine tier is genuinely useful on its own — but never let the button set imply
coverage that does not exist.

### The `"other"` bug this fixes

Today picking "Другое" persists the literal string `"other"`: `_advance` writes the raw button value
and `synthesize_profile` copies it verbatim (`onboarding.py:271-272`), so the relevance prompt renders
`Location: other`. That is worse than not asking. The free-text follow-up replaces it; the typed value
is also the demand signal for choosing which country to curate next, so store it, do not normalize it
away.

## 3. Profile model

`engine/profile.py` — add `residence_country`: an **ISO-3166-2 code**, not a display string. Stage 2
routes by matching it against `sources.country` (`engine/models.py:61`), and free strings cannot match.
Keep `location` as the human-readable string for prompts. For a free-text country, resolve to a code if
it is recognizable and otherwise store a sentinel — decide the representation, document it, and make
sure Stage 2 can tell "no local tier" from "not yet asked".

**Breaking-change hazard, handle explicitly:** `Profile` is `extra="forbid"` and `resolve_profile`
(`engine/users.py:33-43`) validates stored JSONB on every pipeline run. A new **required** field makes
every existing profile fail validation and breaks the pipeline for the current user. Either make it
optional with a documented default, or backfill `users.profile` and `config/profiles/*.yaml` in the
same change. State which you chose and why in the PR description.

Also add the country data as **data, not code** — a config file mapping country code → display label,
legal-status options, and (left empty for now, filled in Stage 3) the residence-tier prompt slots. The
goal is that adding a country is a data change plus feed curation, never a code change.

`synthesize_profile` must keep copying hard facts verbatim and keep `keyword_rules` empty; the
synthesis prompt's contract does not change here.

## 4. Folded-in fix: acknowledge every callback

Live testing found the language question appears to freeze. `onboarding.py:175-194`, the multi-select
branch, returns before reaching the `answer_callback_query` on line 198, so Telegram spins on the
button for ~30s; and the keyboard is never repainted, so nothing marks what is selected. Both feedback
channels are silent at once, which is what reads as "бот завис".

Required in the rewritten machine:

- **No return path may skip the acknowledgement.** Prefer a structure where this is guaranteed (answer
  once at a single exit, or in a `finally`) over adding call sites a later edit can bypass again.
- **Multi-select repaints its keyboard** on each toggle, marking chosen options with a `✅ ` prefix —
  the idiom already used by `build_digest_keyboard` (`delivery/keyboards.py:131-134`). Extend
  `build_onboarding_keyboard` with a `selected` argument defaulting to empty so single-select call
  sites are unchanged. The message id comes from `callback["message"]["message_id"]`; guard its absence.
- **A failed repaint must never swallow the acknowledgement.** `delivery/client.py:114-143` raises on
  any non-`ok` response, and a fast double-tap legitimately produces `400 message is not modified`. A
  stale checkmark is cosmetic; an unanswered callback is the bug.
- **Stale-keyboard taps** — old questions stay in the chat with live buttons and scrolling back to tap
  one is normal user behaviour. Today that path returns silently (`onboarding.py:165-172`).
  Acknowledge it with a short "этот шаг уже пройден" string; do not advance or rewind the state.
- While here: the "Готово" row builds `callback_data` without `_validate_callback_data`
  (`keyboards.py:232`), unlike every other button in that module. Route it through the helper.

## Tests

- **Every callback in the full-funnel test is answered exactly once.** The fake client
  (`tests/test_onboarding.py:32`) currently accepts `answer_callback_query(*_, **__)` and discards it,
  which is why this defect shipped — make it record calls and assert on them. This is the regression
  guard for the whole class, not just one branch.
- A language toggle answers its callback and repaints the keyboard with that language marked; a second
  tap unmarks it. Must fail on current code.
- A failing `edit_message_reply_markup` still answers the callback.
- A stale-step tap is answered and leaves `state.step` unchanged.
- Gate: answering "нет" leaves `user.profile` NULL and `user.enabled` false, and records the UIEvent.
- Conditional flow: a Poland answer reaches the legal-status question; a Spain answer skips it and
  lands on the next applicable question with the correct answer key — i.e. assert the resulting
  profile, not just the step number, so a skipped step cannot silently shift the key mapping.
- "другая" stores the typed country, never the literal `"other"`.
- An unsupported country produces the expectation-setting message and still completes onboarding.
- Existing stored profiles (without `residence_country`) still validate through `resolve_profile` —
  this is the test that proves the pipeline does not break for the current user.

## Definition of done

- `make lint` and `make test` pass. Report the **executed** test count, not the collected count: a run
  reporting many skips means the DB fixture is skipping (`tests/conftest.py:89`) and proves nothing.
- The named must-fail-first tests fail before the change and pass after.
- Deploy notes state whether a `users.profile` backfill is required and that in-flight
  `onboarding_state` rows must be deleted.

## Out of scope

- Source routing and any change to which events reach the LLM — Stage 2.
- `relevance_v4.j2` and prompt parameterization — Stage 3. The country config file gets its slots then;
  leave them empty here.
- Curating feeds for the four uncovered countries — Stage 4.
- The profile view/edit card, and the silent LLM fallback in `synthesize_profile`
  (`onboarding.py:289-303`) — both tracked separately.
