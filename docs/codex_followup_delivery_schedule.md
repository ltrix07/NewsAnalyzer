# Codex follow-up — Delivery schedule review fixes

Review of the `send-due` implementation. The feature is correct in shape — slot arithmetic, DST
handling, the missed-tick self-heal, the slot check constraint and the httpx silencing are all right,
and the suite genuinely runs 197 passed / 0 skipped. Three defects to fix before this ships. The first
two are blocking.

## 1. Blocking — partial failure re-sends already-delivered digests

`deliver_due` (`delivery/dispatcher.py:594-635`) calls `_deliver_for_user` with
`commit_incrementally=False` and then rolls back the user's whole transaction if any digest failed:

```python
if report.failed != failed_before:
    await session.rollback()
    continue
```

`reveal_digest` sets `digest_model.delivered_at` and adds the `Impression`
(`delivery/dispatcher.py:316-319`). With incremental commits disabled, neither is committed until the
end, so the rollback discards the record of **every** digest already sent in that pass — while the
Telegram messages are, of course, already in the user's chat.

Failure scenario: five pending digests; #1–#3 send; #4 raises (timeout, HTTP 429, any
`RuntimeError` from `_post`); the inner `except` at `:517` records the failure and continues; #5 sends.
After the loop `report.failed` has changed, so the transaction is rolled back. Four messages are
delivered, `delivered_at` is unset for all of them, and `last_delivery_date` is never written — so the
user stays due and the next hourly tick delivers the same four **again**. This repeats every hour for
the rest of the local day.

This is the duplicate-delivery failure mode the fragmentation work has been fighting, reintroduced
from the other end, and it only appears under partial failure — which is why the tests pass.

**The brief's wording caused this and is being corrected here.** "`last_delivery_date` must be written
in the same transaction as the send" meant *do not write the guard in a separate later transaction
where a crash between the two loses it*. It did not mean making N sends atomic — Telegram delivery is
not transactional, so a message that has left the process cannot be rolled back, and any design that
pretends otherwise will re-send.

Required:

- Restore per-digest commits for the scheduled path — drop the `commit_incrementally` parameter and the
  two conditionals it guards. The per-digest commit **is** the duplicate guard: it makes an already-sent
  message durably recorded before the next send is attempted.
- Write `last_delivery_date` after the loop, committed, **only when the pass had no failures**.
- On a pass with failures, leave `last_delivery_date` unset so the next tick retries. That retry is now
  safe precisely because successful sends were committed: those digests are no longer pending and
  cannot be re-sent. Only the genuinely failed ones are retried, which is the desired behaviour.

Add a test for exactly this: a user with several pending digests where one send raises, asserting that
the successfully delivered digests have `delivered_at` set after the call, and that a second
`deliver_due` on the same day sends **only** the previously failed digest. This test must fail on the
current implementation.

## 2. Blocking — one malformed user row stops delivery for everyone after it

`deliver_due:604` evaluates the due check outside the `try`:

```python
for user in users:
    if user.chat_id is None or not is_user_due(user, delivery_time):
```

`is_user_due` raises `ValueError` for an unrecognized slot and `ZoneInfo(user.timezone)` raises
`ZoneInfoNotFoundError` for a bad timezone string. Neither is caught, so the exception escapes the loop
and the entire tick dies. `list_enabled_users` orders by username, so the symptom is that everyone
alphabetically after the bad row silently receives nothing — every hour, until someone notices.

`delivery_slot` is protected by a check constraint, but `timezone` has no constraint and no validation
anywhere. The pending account menu will let users change it, which turns this from a hypothetical into
a reachable state.

Required:

- Move the due evaluation inside the per-user `try`, so a malformed row costs that one user their
  delivery and nothing more. The existing `except` already logs with `username`, which makes the bad
  row identifiable.
- Validate the timezone where it is written (reject anything `ZoneInfo` cannot construct) rather than
  only where it is read.
- Test: a user with an invalid timezone does not prevent a later-sorting user from being delivered to.

## 3. Migration must not read settings

`migrations/versions/2026_07_19_1a2b3c4d5e6f_add_user_delivery_schedule.py:21` computes the column's
`server_default` from `get_settings().default_timezone`. Three consequences:

- The resulting schema depends on whichever `.env` was present when the migration ran, so two
  environments can end up with different defaults from the same revision. A migration should produce
  the same schema everywhere.
- `engine/models.py:90` hardcodes `server_default=text("'Europe/Warsaw'")`. If `DEFAULT_TIMEZONE` is
  ever set to anything else, the model and the database disagree and autogenerate starts emitting
  phantom diffs.
- It couples schema migration to config validity: an `.env` that fails `Settings` validation — which now
  includes the merge-window invariant — makes the migration fail with a confusing configuration error
  instead of a database one.

Hardcode the literal default in the migration to match the model. `DEFAULT_TIMEZONE` keeps its role for
newly created users at the application layer, which is where a configurable default belongs.

## Definition of done

- `make lint` and `make test` pass; report the **executed** count.
- The two new tests (partial-failure retry, malformed row isolation) fail before the change and pass
  after.
- Migration round-trip still clean.
