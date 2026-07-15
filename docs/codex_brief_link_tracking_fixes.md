# Codex brief — Link tracking: durability and redirect-availability fixes

Follow-up to `docs/codex_brief_link_tracking.md` (already merged, Alembic head `e3f4a5b6c7d8`).
The feature works; these are three defects found in review. **No schema changes, no new migration.**

---

## Fix 1 — Tracked links must be committed before the Telegram message is sent

### The defect

`deliver_pending` (`delivery/dispatcher.py`) wraps the entire digest loop in a single
`session_scope`, and `session_scope` (`engine/db.py:40-51`) only commits when the context exits.
Inside the loop there is nothing but `flush()`. So the current order is:

1. `_mint_digest_links` inserts `digest_links` rows (inside a savepoint — not committed),
2. `send_message` delivers the message to Telegram **with the tracked URLs in it**,
3. …the transaction is committed only after every digest in the batch has been processed.

If that final commit fails — a dropped connection to Neon is an ordinary event for serverless
Postgres — the `digest_links` rows roll back while the Telegram messages have already been
delivered. Every tracked link in that batch then resolves to `404` **permanently**: the user cannot
reach any source, and the click metric the feature exists to collect is silently zeroed.

The rule: **a token must be durable in the database before the message containing it leaves the
process.**

### The change

In `deliver_pending`, commit immediately after minting and before formatting/sending:

```python
if settings.link_tracking_enabled:
    base_url = settings.require_redirect_base_url()
    try:
        tokens = await _mint_digest_links(session, digest, chat_id)
        # Tokens must be durable before the message that embeds them is sent —
        # otherwise a later rollback leaves permanently dead links in Telegram.
        await session.commit()
        link_urls = {index: f"{base_url}/r/{token}" for index, token in tokens.items()}
    except Exception:
        logger.warning("link_minting_failed", digest_id=digest_model.id)
        link_urls = None
```

Note the `except` branch must also reset `link_urls` to `None` so a failure after a partial mint
cannot leak half-tracked URLs into the message.

A mid-loop commit is safe here: the session factory sets `expire_on_commit=False`
(`engine/db.py:36`), so `digest_model` and the other loaded ORM objects are not expired and no lazy
reload (which would raise `MissingGreenlet` under asyncio) is triggered.

### Also: close the duplicate-send window

While you are in that loop, replace the `await session.flush()` at the end of each iteration (after
`delivered_at`, `telegram_message_id` and the `Impression` are set) with `await session.commit()`.

Today a failure of the final batch commit loses `delivered_at` for digests that were already sent,
so the next run re-sends them as duplicates. Committing per iteration makes delivery at-least-once
**per digest** instead of per batch, which is the intended semantics and removes the last window in
which a delivered message can have uncommitted state behind it.

Do not restructure the loop beyond these two commits.

---

## Fix 2 — A failed click write must never turn into a failed redirect

### The defect

In `web/app.py`, `redirect_link` builds its `RedirectResponse` **inside** `async with
session_scope()`. `_record_click` is wrapped in try/except, but the `commit()` in the context
manager's `__aexit__` is not — and that commit runs before the response is handed back to FastAPI.
If the commit fails, the exception propagates and the user gets a `500` instead of their article.

The brief was explicit: click logging is best-effort and must never break the click.

### The change

Split the read from the write:

1. In one `session_scope`, look up the link by token and copy out the two scalars you need
   (`link.id`, `link.url`). If no row → return `404` `PlainTextResponse("Link not found")` as today.
2. Exit that scope.
3. Record the click in its **own** `session_scope`, wrapped whole — insert *and* commit — in
   `try/except Exception`, logging `link_click_logging_failed` on error. Keep skipping the write for
   crawler user-agents.
4. Return the `302` with `Cache-Control: no-store`.

Once the write is in its own scope, the `begin_nested()` savepoint in `_record_click` is no longer
needed; a plain add + commit inside the try/except is enough.

The redirect must be reachable in every case where the token was found, whatever the click write
does.

---

## Fix 3 — Only ever redirect to an http(s) URL

`RedirectResponse(link.url, ...)` puts a value that originated in an RSS feed straight into a
`Location` header. Browsers ignore non-http schemes there, so the practical risk is negligible — but
the guard is one line and it is cheaper than the argument.

Before redirecting, require that the stored URL starts with `http://` or `https://` (case
insensitive). If it does not, log a warning and return the same `404` as an unknown token. Do not
attempt to sanitize or rewrite the URL.

---

## Tests

Extend `tests/test_link_tracking.py`:

- **Tokens survive a rollback of the outer delivery transaction.** Deliver one digest with tracking
  on, then force the outer `session_scope` to fail (or simply assert from a *separate* session that
  the `digest_links` rows are visible before the dispatcher's outer scope exits). The point to pin
  down: the rows are committed, not merely flushed, by the time `send_message` is called. Asserting
  from inside the fake Telegram client's `send_message` — that a fresh session can already read the
  token — is the most direct way to express this.
- **A failed mint leaves no tracked URLs in the message** (already covered) — extend it to assert
  `link_urls` was not partially applied: every citation in the message renders its raw URL.
- **`/r/{token}` still returns 302 when the click commit fails** (patch the click-recording session
  or commit to raise, not just the insert).
- **`/r/{token}` returns 404 for a link whose stored URL has a non-http scheme** (e.g.
  `javascript:alert(1)`), and writes no `link_clicks` row.

Keep the existing tests passing unchanged.

## Definition of done

- `make lint` and `make test` pass.
- No migration, no schema change, no change to behavior when `LINK_TRACKING_ENABLED=false`.
