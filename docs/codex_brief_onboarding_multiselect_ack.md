# Codex brief — Acknowledge onboarding callbacks and show multi-select state

## The bug

During a live onboarding run the language question (step 2, "На каких языках вы читаете новости?")
appears to **freeze**: tapping a language does nothing visible and the button spins for ~30 seconds.

`delivery/onboarding.py:175-194` — the multi-select branch:

```python
if state.step == 2:
    selected = list(state.answers.get("languages", []))
    if value == "done":
        ...
        await _advance(session, settings, client, llm, user, state, selected)
    elif value in allowed:
        selected = (...)
        state.answers = {**state.answers, "languages": selected}
        state.updated_at = datetime.now(UTC)
        await session.flush()
    return          # <-- exits before the answer_callback_query on line 198
```

Two consequences, both user-visible:

1. **The callback is never answered.** Every other button in the app answers
   (`onboarding.py:198`, `:160`, `:180`), so Telegram resolves them instantly. Here it does not, and
   the Telegram client shows a loading spinner on the button until it times out. This affects the
   toggle taps *and* the "Готово" tap — the latter does send the next question, so it looks merely
   sluggish rather than broken.
2. **The keyboard is never re-rendered.** Nothing marks which languages are currently selected, so
   the user has no way to tell a registered tap from an ignored one. This is what turns a spinner
   into "бот завис": the only two feedback channels are both silent.

The state machine itself is correct — the selections are stored and the resulting profile is right.
Only the acknowledgement is missing.

### Why tests did not catch it

`tests/test_onboarding.py:93-95` drives exactly this sequence (`onb:2:uk`, `onb:2:en`, `onb:2:done`)
and asserts only the final `user.profile["languages"]`. The fake client at `tests/test_onboarding.py:32`
accepts `answer_callback_query(self, *_: Any, **__: Any)` and discards the call, so an unanswered
callback and an answered one are indistinguishable to the suite. Any fix here must also close that
blind spot, otherwise the same class of defect ships again.

## Changes

### 1. Render selection state in the onboarding keyboard

`delivery/keyboards.py:221` — add an optional `selected` argument and mark chosen options, mirroring
the existing idiom in `build_digest_keyboard` (`✅ ` prefix, `keyboards.py:131-134`):

```python
def build_onboarding_keyboard(
    step: int,
    options: list[tuple[str, str]],
    *,
    lang: str = "ru",
    done: bool = False,
    selected: Sequence[str] = (),
) -> dict[str, list[list[dict[str, str]]]]:
```

Each row's label becomes `f"✅ {label}"` when its `value` is in `selected`. Default `()` keeps every
existing call site (single-select steps) byte-identical.

While here: the "Готово" row at `keyboards.py:232` builds its `callback_data` without passing through
`_validate_callback_data`, unlike every other button in the module. Route it through the helper for
consistency — it cannot exceed the limit today, but the asymmetry is an invitation to a future bug.

### 2. Answer every callback branch, and repaint the keyboard on toggle

In the `state.step == 2` branch:

- **On toggle:** after persisting the new selection, edit the existing message's markup to the
  re-rendered keyboard, then answer the callback. The message id comes from
  `callback["message"]["message_id"]` — guard for its absence rather than assuming it.
- **On "Готово" with a non-empty selection:** answer the callback after `_advance`, matching line 198.
- **On "Готово" with an empty selection:** already answers with `onboarding_invalid` — leave as is.

The markup edit must never be able to swallow the acknowledgement. `delivery/client.py:114-143` raises
on any non-`ok` Telegram response, and a fast double-tap can legitimately produce
`400 message is not modified`. Wrap the edit so a failed repaint is logged and the callback is still
answered — a stale checkmark is a cosmetic defect, an unanswered callback is the bug we are fixing.

Prefer a shape where **no return path can skip the acknowledgement** over adding two more call sites
that a future edit can bypass again — e.g. answer once in a `finally`, or compute the response text
and answer at a single exit. Keep it terse and in the style of the surrounding code; do not restructure
the state machine.

### 3. Also worth a look while in this file

`_handle_callback` returns silently at `onboarding.py:147-148` (non-`onb:` data) and `:165-172`
(step mismatch — i.e. a tap on a *stale* keyboard from an earlier question). The stale-keyboard case
is reachable in normal use: the old messages stay in the chat with live buttons, so a user scrolling
back and tapping gets the same silent spinner. Answer that case too, with a short "этот шаг уже
пройден"-style string added to `delivery/strings.py` (ru + en, following the existing `t()` table).
Do not attempt to advance or rewind the state machine on a stale tap — acknowledge and ignore.

## Tests

- Make the fake Telegram client in `tests/test_onboarding.py` **record** `answer_callback_query`
  calls (id + text) instead of discarding them, and record `edit_message_reply_markup` calls.
- Assert that a language toggle answers its callback and repaints the keyboard with the tapped
  language marked; assert a second tap on the same language unmarks it.
- Assert "Готово" with a selection answers its callback and sends the next question.
- Assert every callback in the existing full-funnel test is answered exactly once — this is the
  regression guard for the whole class of defect, not just this branch.
- Assert a tap on a stale-step keyboard is answered and leaves `state.step` unchanged.
- Assert a failing `edit_message_reply_markup` (fake raises) still answers the callback.

The toggle-repaint and the "Готово"-answer tests must fail on current code.

## Definition of done

- `make lint` and `make test` pass. Report the **executed** count, not the collected count: a run
  reporting large numbers of skips means the DB fixture is skipping (`tests/conftest.py:89`) and the
  result is not evidence of anything.
- The two named tests fail before the change and pass after.
- No migration — this is presentation and acknowledgement only.

## Out of scope

- The wording or ordering of the onboarding questions.
- The profile-synthesis step and its silent fallback on LLM failure
  (`onboarding.py:289-303`) — separate concern, tracked separately.
