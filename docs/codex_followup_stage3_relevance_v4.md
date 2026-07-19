# Codex follow-up — Stage 3 review fixes

Review of `relevance_v4.j2`. The structure is right: v3 is untouched, the flag defaults to false, the
`None`/`ZZ` path correctly omits categories B and C while keeping the full Ukraine tier, STEP 3 is
rewritten so those users are not rejected wholesale, and `compare-relevance` persists nothing. Suite is
212 passed / 0 skipped, verified independently.

Three defects in the prompt itself, all found by rendering it rather than by running the tests. Fix
before the flag is turned on.

## 1. The user's location is lost

v3 rendered `- Location: {{ profile.location }}`. v4 renders `residence.name` from
`config/countries.yaml`, which is a different and lossier value.

- For the existing profile, `location: "PL (Warsaw)"` becomes `Location: Poland`. The city is gone.
  Locality demonstrably matters to this user's taste (the Kharkiv pattern in the feedback analyses), so
  this is a quality regression on the only profile with a feedback history.
- For a `None` or `ZZ` profile there is **no Location line at all** — confirmed by rendering. The
  free-text country collected in Stage 1 lands in `profile.location` and is then discarded at the one
  place it could do any work. A Ukrainian in Portugal is presented to the model as someone with no
  place of residence.

Fix: restore `- Location: {{ profile.location }}` in the USER PROFILE block for **every** profile, and
use `residence.name` only inside the generated categories, where a canonical country name is what is
actually wanted. The two values answer different questions and both belong in the prompt.

Test: a `ZZ` profile whose `location` is `"Portugal"` renders that string in USER PROFILE while
rendering no B or C category — the current implementation gets the second half right and the first half
wrong, so assert both together.

## 2. Partially filled country slots raise at render time

`{% if residence.slots %}` is an all-or-nothing test, but the block it guards dereferences five specific
keys (`residence_and_work_terms`, `migration_policy_terms`, `central_bank`, `currency`,
`financial_regulator`). A country with some slots filled raises:

```
jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'migration_policy_terms'
```

This is not cosmetic — it fails at render time, so it breaks the relevance stage for every event for
every user in that country.

It is also not hypothetical: Stage 4 adds countries incrementally, and filling two slots out of five is
the natural intermediate state of that work. A YAML edit must not be able to break the pipeline.

Fix: validate the slots where the config is loaded, not where it is rendered — a country's
`residence_prompt_slots` must be either **empty** (generic category, already handled) or **complete**
(all required keys present and non-empty). Raise a clear error naming the country and the missing keys.
The template then keeps its simple all-or-nothing branch, which is correct once the invariant holds.

Test — the important one: iterate over **every** country in `config/countries.yaml`, render v4 for a
profile in it, and assert no exception. That turns a future bad edit into a test failure instead of a
production outage, and it costs nothing to keep.

## 3. Anti-pattern 5 still hardcodes Polish

```
5. Do NOT reject because the article is in Polish or Ukrainian or Russian —
   language is not a filter.
```

For a Spain-resident user this names a language they may not read and omits the ones they do. Build the
list from `profile.languages`.

This one is the brief's fault, not the implementation's: the table named anti-pattern 2 explicitly and
said nothing about 5, and 2 and 3 were correctly generalized. Flagged here for completeness.

## Optional, only if cheap

- `_country_registry()` in `engine/stages/relevance.py` is a second `lru_cache`d loader of
  `config/countries.yaml`, alongside `_countries()` in `delivery/onboarding.py`. Two loaders of one file
  will drift in validation — and item 2 above adds validation to exactly one of them. Sharing a single
  loader would be the natural place to put that validation.
- `engine/stages/base.py` changed `version: ClassVar[str]` to `version: str`, loosening the contract for
  every stage so that one stage can set it per instance. It works; it is simply a wider change than the
  need justifies.

## Definition of done

- `make lint` and `make test` pass; report executed and skipped counts separately.
- `relevance_v3.j2` still unmodified — verify with `git diff`.
- The PR description includes the rendered v4 prompt for a PL profile and for a `ZZ` profile with a
  free-text location. This was asked for in the original brief and not provided; the prompt is a product
  artifact and should not land unread.
