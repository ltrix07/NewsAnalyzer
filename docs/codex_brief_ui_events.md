# Codex brief — `ui_events`: uniform button-usage logging

## Why

We are about to open the bot to ~30 beta testers. Two of the questions the beta must answer are
"which features do people actually use?" and "which ones does nobody touch?" — and right now we
cannot answer either.

What exists today is fragmented and semantic, not behavioural:

- `digest_feedback` records like/dislike **verdicts** (and their drill-down reason),
- `link_clicks` records source clicks,
- `discussion_pending` / `research_pending` record *intent in flight*, and are deleted once the
  interaction completes.

So "Обсудить" and "Уточнить в сети" leave no durable trace at all. Nobody can count how many times
they were pressed.

`ui_events` is a single append-only table that records **every button press, uniformly**, regardless
of what the press semantically meant. It does not replace `digest_feedback` — that table keeps its
own meaning (the current verdict, with a UNIQUE-ish latest-wins read pattern). `ui_events` is the
usage layer underneath. Some redundancy between the two is intentional; do not try to unify them.

## The one design rule

**Instrument the choke point, not the branches.**

`_handle_callback_query` in `delivery/listener/handlers.py` calls `parse_callback_data(data)` once
and then fans out into `if payload.action in {"like", "dislike"} / == "dislike_reason" / ==
"research"` branches. Log the event **once, immediately after the payload parses**, before the fan-out
— not inside each branch.

This is what makes the table survive us: every button we add later is logged automatically, with no
chance of someone forgetting the logging line in a new branch.

## Data model

New table, following the conventions in `engine/models.py`.

### `ui_events`

| column       | type                           | notes                                                     |
| ------------ | ------------------------------ | --------------------------------------------------------- |
| `id`         | BigInteger PK                  |                                                           |
| `chat_id`    | BigInteger, not null           | who pressed                                               |
| `action`     | String, not null               | see the vocabulary below                                  |
| `digest_id`  | BigInteger, **nullable**       | FK `digests.id`; nullable — future actions need not target a digest |
| `context`    | JSONB, nullable                | small action-specific payload                             |
| `created_at` | timestamptz, not null          | `now()`                                                   |

Indexes: `("chat_id", created_at DESC)` and `("action", created_at DESC)` — the two shapes every
analytics query will use.

`digest_id` is deliberately nullable rather than a hard FK requirement: the onboarding questionnaire
and the batched-delivery gate (both coming next) will emit `ui_events` rows that have no digest.

Alembic migration chains off the current head `e3f4a5b6c7d8` (`add_link_tracking`).

### `action` vocabulary

Reuse the existing `KeyboardAction` values verbatim so the column joins cleanly against the code:

- `like`, `dislike`, `dislike_reason`, `discussion`, `research`

Plus two that do not come from a keyboard:

- `discussion_question` — the user actually sent their free-text question after pressing "Обсудить".
  Logged from `_handle_message` when the message resolves against a `discussion_pending` row.
  `context` = `{"question_length": <int>}`. **Do not store the question text** — we already have the
  digest, and the text is the user's own writing.
- `unknown_callback` — `parse_callback_data` returned `None`. `context` = `{"data": <raw
  callback_data>}` (it is capped at 64 bytes by `_MAX_CALLBACK_BYTES`, so it is safe to store whole).
  This is the tell-tale of a stale keyboard left over from an older deploy, and it costs nothing to
  capture.

For `dislike_reason`, set `context` = `{"reason": "off_topic" | "weak_analysis"}`.

Define the vocabulary as a `Literal` type or module constant next to `KeyboardAction` — not as bare
strings scattered through the handler.

## Behaviour

Logging is **best-effort and must never affect the user-visible interaction.** The file already has
this idiom (`_best_effort_answer_callback`, `_best_effort_edit_reply_markup`,
`_best_effort_send_message`). Add `_best_effort_log_ui_event` in the same shape:

- It opens its **own** `session_scope()` — never the caller's session. A failure to write an
  analytics row must not roll back or poison the transaction that is recording the user's actual
  feedback.
- The whole thing (insert *and* commit) is wrapped in `try/except Exception`, logging
  `ui_event_logging_failed` on error.
- It is awaited before the branch logic, but its failure is swallowed.

(This is the same conclusion we reached for click logging in `web/app.py` and for token minting in
`delivery/dispatcher.py`: a side concern gets its own transaction.)

## Analytics deliverable

Add `audit/ui_usage.sql` with two queries, commented:

1. **Feature usage per week** — count of each `action`, grouped by ISO week and `action`. This is the
   query that answers "what does nobody use".
2. **Dislike drill-down completion rate** — of the `dislike` presses, what share were followed by a
   `dislike_reason` press from the same `chat_id` within 10 minutes. If this is low, the drill-down
   UI is too much friction and we need to know before we build more on top of it.

Match the style of whatever is already in `audit/`.

## Tests

Extend the listener tests:

- Every button press writes exactly one `ui_events` row with the right `action`, `chat_id` and
  `digest_id` — cover `like`, `dislike`, `dislike_reason` (with `context.reason`), `discussion`,
  `research`.
- A `dislike` press writes **both** a `digest_feedback` row and a `ui_events` row (the two layers
  coexist).
- Unparseable `callback_data` writes a `ui_events` row with `action="unknown_callback"` and the raw
  data in `context`, and still does not crash the handler.
- Answering a pending discussion writes `action="discussion_question"` with `context.question_length`
  and **not** the question text.
- A failing `ui_events` write (patch it to raise) does not change the user-visible outcome: the
  feedback is still recorded, the callback is still answered, and the handler returns normally.

## Definition of done

- `make lint` and `make test` pass.
- Migration applies and downgrades cleanly from head `e3f4a5b6c7d8`.
- No behavioural change to any existing interaction — this brief adds observation only.
