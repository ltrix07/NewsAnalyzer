# Codex follow-up — Language should not influence selection

Product decision taken 2026-07-19: the value of the product is that the bot reads the **primary
source**, in whatever language it was published, and reports back in the one language the user chose.
Language is therefore not a selection criterion at all, and the only thing worth asking a user is which
language to write their digests in.

This changes `relevance_v4.j2`, the questionnaire, and some button labels. **`relevance_v3.j2` must
remain untouched** — it is still the default and the rollback.

## 1. Remove language from the selection prompt entirely

Two changes to `engine/llm/prompts/relevance_v4.j2`, which work together:

- **Delete the `- Languages they read: ...` line** from the USER PROFILE block.
- **Make anti-pattern 5 unconditional** — no list of languages, neither the user's nor the corpus's:
  do not reject an article because of the language it is written in, whatever that language is.

The reasoning for doing both: the profile line tells the model which languages the user reads, and
anti-pattern 5 then forbids the model from acting on exactly that information. The anti-patterns were
written as patches for observed misbehaviour, and this particular misbehaviour is plausibly *caused* by
the line above it. Removing the cause is better than maintaining the antidote.

A list-free anti-pattern also cannot drift: it needs no update when feeds in a new language are added
in Stage 4.

Note this supersedes the previous follow-up item about mapping language codes to names in that
sentence. There is no longer a list to render, so the `uk` / `UK` ambiguity disappears from the prompt
along with it.

## 2. Drop the reading-languages question from onboarding

With the above, `Profile.languages` drives nothing. The question is a funnel step that buys nothing —
and it is the step that visibly broke during live testing.

- Remove the `languages` `Question` descriptor from `QUESTIONS` in `delivery/onboarding.py`.
- Keep the **output language** question. Rephrase its prompt string so it clearly asks what it now
  actually means: which language to write the digests in.
- Update `onboarding_summary` in `delivery/strings.py` (ru + en) — it currently prints
  `Языки чтения: {languages}`, which will have nothing to show.

**Keep the `Profile.languages` field.** Removing it means touching stored JSONB for no benefit.
Populate it for new profiles with `[output_language]` — `synthesize_profile` currently does
`languages=list(answers["languages"])` and will raise `KeyError` once the question is gone, so this must
be handled in both the LLM path and the fallback path.

Consequence to note in the PR, not to act on: the languages question is the only `multi` question, so
that branch of the state machine will have no live user. Keep the kind and its tests — the account menu
is likely to need it — but be aware it is now exercised only by tests.

## 3. Language button labels

The output-language options are currently `[("ru", "RU"), ("uk", "UK"), ("pl", "PL"), ("en", "EN")]`.
`uk` is ISO 639-1 for Ukrainian, but a button reading **UK** next to one reading **EN** is read as
"United Kingdom" or "English" by roughly everyone. The same code was also labelled `UA` in the
reading-languages question, so the questionnaire labelled one language two different ways.

Label the buttons with language names in the language itself — `Русский`, `Українська`, `Polski`,
`English` — and keep the ISO codes as the callback values. Endonyms avoid needing a translation per
`ui_language`.

## Tests

- `relevance_v4.j2` renders no `Languages they read` line and no language list in anti-pattern 5, for
  every configured country and for `None` / `ZZ`.
- Anti-pattern 5 is still present — deleting the list must not delete the protection.
- `relevance_v3.j2` renders unchanged; the existing v3 tests pass untouched.
- Onboarding completes without a languages question and produces a profile whose `languages` equals
  `[output_language]`, in both the synthesis path and the fallback path.
- The existing full-funnel test's callback sequence and its "every callback answered exactly once"
  assertion survive the question removal.

## Definition of done

- `make lint` and `make test` pass; report executed and skipped counts separately.
- `git diff` shows `relevance_v3.j2` unmodified.
- PR description includes the rendered v4 prompt for a PL profile, so the removals can be read in
  context.

## Validation this enables (operator step, not code)

Once this lands, `compare-relevance` measures exactly the question at issue: v3 carries the profile
language line plus the hardcoded anti-pattern, v4 carries neither. The disagreements between them are
the behavioural effect of this decision, and they should be read before `relevance_v4_enabled` is
turned on.
