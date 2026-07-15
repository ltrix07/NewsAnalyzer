# Codex brief — Multi-user 5c: per-user delivery + listener

## Context

5a added the `users` table; 5b made selection run per user (digests now carry `profile_name =
username`). 5c is the last multi-user piece: **deliver each user's digests to their own chat, and let
the listener serve every user** — not just the one hard-coded `settings.telegram_chat_id`.

With one seeded user, 5c must be behavior-identical to today. Its effect appears only with a second
enabled user.

## Decisions already made

- **Listener resolves the user by `chat_id`** via `get_user_by_chat_id`. An update from an unknown
  chat is ignored (as today, but the allow-list is now "any enabled user" instead of one env value).
- **Per-user failure isolation in delivery.** Delivery loops over enabled users; a failure for one
  user is logged and skipped, the others still get their digests. One broken profile must never block
  the batch.

## Change 1 — `deliver_pending` loops over enabled users

Today `deliver_pending` reads a single `settings.require_telegram_chat_id()` and delivers every
pending digest to it. Restructure:

```
users = await list_enabled_users(session)
for user in users:
    if user.chat_id is None:
        continue            # no delivery target yet; skip quietly
    try:
        await _deliver_for_user(session, user, ...)   # the current body, scoped to one user
    except Exception:
        logger.exception("user_delivery_failed", username=user.username)
        continue            # isolation: one user's failure does not touch others
```

Extract today's per-digest loop into `_deliver_for_user(session, user, client, adjudicator,
settings, report)`. Inside it, everything that currently reads a global becomes user-scoped:

- **Pending digests are the user's own:** `_pending_digests_query` gains a `profile_name` filter →
  `WHERE delivered_at IS NULL AND profile_name = :username`. (A user must never receive another
  user's digest.)
- **Target chat** is `user.chat_id`, not `settings.telegram_chat_id`.
- **`ui_language`** is `user.ui_language`, not `settings.ui_language`, everywhere in the per-user path
  (`t(...)`, keyboards, batch strings, thread header).
- **Taste vector** is scoped to this user — see Change 3.
- **Open batch** lookup is already keyed by `chat_id` (`DeliveryBatch.chat_id == user.chat_id`) — keep
  that, it is naturally per-user.

`report` (sent/failed/skipped) accumulates across users; add per-user counts to the log, not to the
return type.

### Single-user CLI override

Keep a way to deliver for one user only (mirrors the pipeline's explicit-profile path and today's
behavior). `deliver_pending` should accept an optional `profile_name` / username: when given, loop
over just that one enabled user; when `None`, loop all enabled users. The cron calls it with `None`.

`send_test_message` (operator connectivity probe) stays on `settings.require_telegram_chat_id()` —
it is a manual diagnostic, not user delivery. Leave it unchanged.

## Change 2 — listener resolves the user from `chat_id`

`handle_update` (`delivery/listener/handlers.py`) currently does
`expected_chat_id = settings.require_telegram_chat_id()` and rejects anything else. Replace with:

- Extract `chat_id` from the update (existing `extract_chat_id`).
- `user = await get_user_by_chat_id(chat_id, session)`. If `None` or `user.enabled` is false → log
  `listener_update_ignored_unknown_chat` and return `HandlerResult()` (same shape as today's foreign-chat
  ignore).
- Thread the resolved `user` (or at least `user.ui_language`) through `_handle_callback_query` and
  `_handle_message` so every ack, prompt, keyboard, and reveal uses **`user.ui_language`**, not
  `settings.ui_language`.

Everything else in the listener already keys on `chat_id` (feedback, discussion_pending,
research_pending, ui_events, delivery batches), so those are naturally per-user once the guard is
chat-based. Do not change those table interactions.

Note: the listener currently caches nothing per-user; resolving the user per update (one indexed
lookup on `users.chat_id`) is fine at beta scale. Do not build a cache.

## Change 3 — per-user taste vector

`build_taste_vector` (`engine/ranking/taste.py`) aggregates **all** `digest_feedback` globally
(its CTE has no `chat_id` filter). With multiple users their tastes would blend into one vector.

Add a required `chat_id: int` parameter and filter the feedback CTE to that chat
(`WHERE df.chat_id = :chat_id`, applied before the `row_number()` per-class ranking). Update the two
call sites:

- `_rank_pending_digests` in `dispatcher.py` — pass the current user's `chat_id`.
- Any test/other caller.

This keeps each user's ranking driven only by their own likes/dislikes — the whole point of taste
ranking. The `taste_ranking_enabled` flag and the cold-start `None` fallback behavior are unchanged.

## Change 4 — `_rank_pending_digests` takes the user

`_rank_pending_digests(session, digests, settings)` becomes
`_rank_pending_digests(session, digests, settings, chat_id)` (or takes the `user`) so it can build the
per-user taste vector. Signature-only change; the ranking math is unchanged.

## Out of scope

- No questionnaire / onboarding (next brief).
- No source subscriptions (later) — every user still sees digests from the shared event set as scoped
  by their profile in 5b.
- No new schema — `users`, `digests.profile_name`, `digest_feedback.chat_id`, `delivery_batches.chat_id`
  already exist. **This brief needs no migration.** If you find yourself writing one, stop and
  reconsider.

## Tests

- **Two users, isolated delivery:** two enabled users with distinct `chat_id` and their own pending
  digests (distinct `profile_name`). `deliver_pending()` sends each user's digests to **their** chat
  only; no cross-delivery. Assert the fake client's calls partition by chat_id correctly.
- **Failure isolation:** make delivery raise for user A (e.g. a client that throws for A's chat_id);
  assert user B still receives their digests and `report.sent` reflects B.
- **`user.chat_id is None` is skipped** without error.
- **ui_language per user:** user A `ru`, user B `en`; assert A's messages/keyboards use ru strings and
  B's use en (e.g. the batch notification or a feedback ack).
- **Listener resolves by chat_id:** an update from a known enabled user's chat is handled with that
  user's `ui_language`; an update from an unknown chat is ignored; an update from a disabled user is
  ignored.
- **Per-user taste:** user A has likes/dislikes, user B has none; assert A's ranking uses a taste
  vector and B falls back to taste-neutral (cold start) — i.e. A's feedback does not leak into B's
  ordering.
- **Single-user parity:** with one enabled user, `deliver_pending(profile_name=None)` reproduces
  today's delivery (same messages, same order, same batch behavior).
- Existing delivery tests still pass (adjust fixtures to seed a user with the expected chat_id where
  they relied on `settings.telegram_chat_id`).

## Definition of done

- `make lint` and `make test` pass.
- **No migration.**
- With one seeded user, delivery and listener behavior are identical to today.
- With two enabled users: each receives only their own digests, in their own `ui_language`, ranked by
  their own taste; a failure for one does not affect the other; the listener serves both.
- `send_test_message` unchanged; questionnaire and source subscriptions remain out of scope.
