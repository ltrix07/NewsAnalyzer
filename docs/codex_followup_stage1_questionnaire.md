# Codex follow-up — Stage 1 review fixes

Review of the Stage 1 questionnaire rewrite (commit `c6f00cf`). The implementation is sound: the
`finally`-based acknowledgement in `_handle_callback` is the right structural answer rather than a patch
on two branches, the full-funnel test asserts every callback is answered exactly once, legacy profiles
without `residence_country` are covered by tests, and the deploy notes correctly require deleting
in-flight `onboarding_state` rows.

Two design defects to fix. Neither breaks anything today; both are the kind that break silently later,
and one of them will block the account menu.

## 1. Index-keyed special-casing defeats the descriptor list

`delivery/onboarding.py:306-318`:

```python
if answered_step == 0:
    answers[question.answer_key] = "Ukraine"
elif answered_step == 1:
    ...
elif answered_step == 2:
    ...
else:
    answers[question.answer_key] = value
```

plus `if state.step == 0 and value == "no"` (`:262`) and
`residence_was_completed = answered_step == 2 or (answered_step == 1 and ...)` (`:330`).

The descriptor list was introduced precisely so that inserting, removing or skipping a question could
not silently shift what an answer means. Three magic indices now bypass it. Inserting any question
above `wanted` re-points all three branches at the wrong questions, with no type error and no obvious
test failure — the same defect class as the old `_ANSWER_KEYS` dict, and less visible than it was,
because the coupling is now spread across an `if` chain instead of sitting in a table.

Two symptoms already visible in the current code, both of which should disappear with the fix:

- Two `Question` entries share `answer_key="location"` (`:88` and `:89-94`), so the answer key no longer
  identifies a question.
- The gate `Question` is keyed `"citizenship"` (`:82-87`) but writes the constant `"Ukraine"` regardless
  of which button was pressed, so its declared key describes neither its options nor its behaviour.

**This is what blocks the account menu.** "Re-ask only the residence country" needs the step-1 and
step-2 handling, which is addressed by position rather than by question identity. The menu would have
to duplicate that logic or force this refactor anyway — do it now, while the code is fresh.

Required: move per-question post-processing into the descriptor itself, so behaviour travels with the
question rather than with its index. An optional `store` callable on `Question` — taking the collected
answers and the submitted value, returning the updated answers — expresses all three special cases
(gate constant, country code + display label, free-text country resolution) without any positional
reference. The gate's "no" branch and the unsupported-country notice should likewise be expressed as
properties of their questions, not as index comparisons.

After the change, no function in this module should compare `step` or `answered_step` against a
literal. Add a test that inserts a question at the front of `QUESTIONS` and asserts the resulting
profile is unchanged — that is the property being bought, and without a test it will erode.

## 2. "Country unsupported" must not be inferred from empty legal-status options

`_has_legal_options` (`:77-78`) currently drives two unrelated decisions: whether to ask the
legal-status question, and whether to warn the user that no local coverage exists (`:333-336`).

It works today only because `config/countries.yaml` happens to give DE, CZ, IT and ES an empty
`legal_status_options`. The two facts are independent: adding legal-status options for Germany — an
innocuous content edit that a future contributor would make without a second thought — **silently
removes the warning that no German news exists**, while German feeds still do not exist. A product
promise about coverage must not be controlled by the emptiness of an unrelated content field.

Required: make the coverage signal explicit and independent of the legal-status configuration. Two
acceptable shapes, in order of preference:

1. Derive it from ground truth — whether any enabled `sources` row has `country` equal to the user's
   `residence_country`. This cannot drift, because it asks the actual question ("do we have feeds for
   this country?") rather than a proxy for it.
2. Failing that, an explicit `residence_tier_supported` field in `config/countries.yaml`, with the
   comment at the top of that file updated — it currently documents the proxy behaviour and would
   otherwise enshrine it.

Whichever is chosen, keep asking the legal-status question purely on the presence of options, so the
two concerns stay separable.

Test: a country configured **with** legal-status options but **without** sources still produces the
unsupported-country notice. That is the case that currently regresses silently.

## Definition of done

- `make lint` and `make test` pass; report the **executed** count.
- No literal step comparisons remain in `delivery/onboarding.py`.
- The question-insertion test and the coverage-notice test both fail on the current code and pass
  after.

## Notes

- `_countries()` is `lru_cache`d, so `config/countries.yaml` edits require a listener restart. That
  matches how settings are already cached — no change requested, but it belongs in the deploy notes.
- Everything from Stage 1 landed inside commit `c6f00cf`, whose message describes only the delivery
  schedule. No action required, but the history does not reflect the two independent pieces of work.
