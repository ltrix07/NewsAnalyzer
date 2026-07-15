# Codex brief — Click-tracked source links (redirect service)

## Why

Today the digest footer renders raw source URLs as `<a href="...">` links. Telegram does **not**
report clicks on links (nor on `InlineKeyboardButton(url=...)`), so we have zero signal on whether
users actually open sources.

We are about to run a ~30-person beta. Source-click rate is one of the core metrics: the hypothesis
is that as trust in the bot grows, users click through to sources **less** while still opening
digests just as often. Without click data that hypothesis is unmeasurable.

Solution: mint an opaque tracking token per (digest, citation, recipient) at delivery time, render
the citation link as `https://<redirect_base_url>/r/<token>`, and stand up a small HTTP service that
logs the click and 302-redirects to the real URL.

This also gives us the HTTP component we will later reuse for the Telegram webhook and the web
onboarding form, so build it as a real (small) app, not a one-off script.

## Scope

**In scope:**
- New `web/` ASGI app with a `GET /r/{token}` redirect endpoint and `GET /healthz`.
- Two new tables: `digest_links`, `link_clicks` (+ Alembic migration).
- Link minting in `delivery/dispatcher.py` before formatting.
- `format_digest` accepts an optional citation-index → tracked-URL map.
- Config: `link_tracking_enabled`, `redirect_base_url`.
- Tests.

**Out of scope (do not touch):**
- Telegram webhook mode (still long-polling).
- Any change to the pipeline, ranking, threading, or feedback logic.
- Auth / user accounts.
- Deployment (systemd unit, TLS, DNS) — that is done by the operator, but document what the app
  expects (see "Ops contract").

## Data model

Two new tables. Follow the existing conventions in `engine/models.py` (BigInteger PKs,
`server_default=text("now()")` timestamps, explicit indexes).

### `digest_links`

| column           | type                       | notes                                            |
| ---------------- | -------------------------- | ------------------------------------------------ |
| `id`             | BigInteger PK              |                                                  |
| `token`          | String(32), not null       | unique; `secrets.token_urlsafe(16)`              |
| `digest_id`      | BigInteger FK digests.id   | not null                                         |
| `chat_id`        | BigInteger, not null       | recipient the link was minted for                |
| `citation_index` | Integer, not null          | position in `digests.citations`                  |
| `url`            | Text, not null             | the real target URL                              |
| `source`         | String, nullable           | citation source name, denormalized for analytics |
| `created_at`     | timestamptz, not null      | `now()`                                          |

Constraints/indexes:
- `UniqueConstraint("token")` — the lookup key.
- `UniqueConstraint("digest_id", "chat_id", "citation_index")` — minting must be idempotent; a
  re-delivery of the same digest to the same chat must reuse the existing token, not mint a second
  one.

### `link_clicks`

Append-only.

| column       | type                            | notes                     |
| ------------ | ------------------------------- | ------------------------- |
| `id`         | BigInteger PK                   |                           |
| `link_id`    | BigInteger FK digest_links.id   | not null                  |
| `clicked_at` | timestamptz, not null           | `now()`                   |
| `user_agent` | Text, nullable                  | truncate to 512 chars     |

Index: `("link_id", clicked_at DESC)`.

**Privacy: do not store IP addresses.** User-Agent only.

Alembic migration chains off the current head (`d2e3f4a5b6c7`, `add_digests_telegram_message_id`).

## Config (`engine/config.py`)

```python
link_tracking_enabled: bool = False
redirect_base_url: str | None = None  # e.g. "https://l.example.com" — no trailing slash
```

Add a `require_redirect_base_url()` accessor mirroring the existing `require_telegram_chat_id()`
style: raise a clear RuntimeError if `link_tracking_enabled` is true and `redirect_base_url` is
unset. Delivery must fail loudly on misconfiguration rather than silently sending untracked links.

Env vars: `LINK_TRACKING_ENABLED`, `REDIRECT_BASE_URL`.

## Delivery changes

### `delivery/formatter.py`

`format_digest` is currently pure and has no DB access — **keep it that way**. Change the signature
to:

```python
def format_digest(digest: Digest, link_urls: dict[int, str] | None = None) -> str:
```

`link_urls` maps citation index → tracked URL. When it is `None` or a given index is missing, fall
back to `citation.url` exactly as today. All existing call sites and tests must keep working with
the one-argument form.

### `delivery/dispatcher.py`

Before formatting each digest:

1. If `settings.link_tracking_enabled` is false → `link_urls = None`, behave exactly as today.
2. Otherwise mint/fetch tokens for all citations of that digest for the target `chat_id` (one
   `INSERT ... ON CONFLICT (digest_id, chat_id, citation_index) DO NOTHING` + `SELECT`, or an
   upsert returning the rows — do it in **one** round trip per digest, not one per citation).
3. Build `link_urls = {i: f"{base}/r/{token}"}` and pass it into `format_digest`.

**Minting must be best-effort in the same spirit as threading** (see the existing try/except around
`_find_thread_parent`): if minting raises, log a warning (`link_minting_failed`, with `digest_id`)
and fall back to raw URLs. A tracking failure must never block a digest from being delivered.

Note the thread-update path formats and sends the same digest as a reply — it goes through the same
minting call, and the unique constraint makes that a no-op reuse of the existing tokens.

## The web service (`web/`)

New package `web/` with `web/app.py` (FastAPI) and `web/__main__.py` (uvicorn entrypoint). Reuse the
existing async SQLAlchemy session factory from `engine/db.py` and the existing structlog setup from
`engine/observability.py` — do not build a second DB or logging stack.

### `GET /r/{token}`

1. Look up `digest_links` by token. Not found → `404` plain-text ("Link not found"), no redirect.
2. Log a `link_clicks` row. **This must be best-effort**: wrap in try/except, log a warning on
   failure, and redirect anyway. A logging failure must never break the user's click.
3. Skip the click-log row (but still redirect) when the `User-Agent` looks like a crawler —
   case-insensitive match on any of `telegrambot`, `bot`, `crawler`, `spider`, `preview`,
   `facebookexternalhit`. (`disable_web_page_preview=True` already means Telegram should not
   prefetch, so this is defense in depth.)
4. Respond `302` with `Location: <url>` and **`Cache-Control: no-store`**. This header is not
   optional — without it a browser or proxy may cache the redirect and subsequent clicks never reach
   us, silently zeroing the metric.

### `GET /healthz`

Returns `200 {"status": "ok"}`. No DB access (so it stays up even if Neon is briefly unreachable).

### Hardening

- No authentication; security rests on the token being unguessable (128 bits). Do not add an
  endpoint that enumerates or lists links.
- Only ever redirect to the URL stored in our own row — never to anything derived from the request
  (no `?next=` parameter, ever). We must not become an open redirector.

## Ops contract (for the operator, not for you to execute)

The app listens on `127.0.0.1:8080` by default (overridable via `--host` / `--port`), behind a TLS
reverse proxy on a real domain. `REDIRECT_BASE_URL` must match that public origin. Add the FastAPI +
uvicorn dependencies to `pyproject.toml`.

## Tests

Add `tests/test_link_tracking.py` (and extend `tests/test_delivery.py` / `tests/test_formatter.py`
as needed):

- `format_digest` with no `link_urls` → renders raw URLs (existing behavior unchanged).
- `format_digest` with `link_urls` → renders tracked URLs; a missing index falls back to the raw URL.
- Dispatcher with `link_tracking_enabled=False` → no `digest_links` rows, raw URLs in the message.
- Dispatcher with tracking on → one `digest_links` row per citation; message contains
  `{base}/r/{token}`.
- Minting is idempotent: delivering the same digest to the same chat twice reuses the same tokens
  (no unique-constraint crash, no duplicate rows).
- Minting failure (patch the mint call to raise) → digest is still delivered, with raw URLs, and
  `report.sent == 1`.
- `GET /r/{token}` → 302 to the stored URL, `Cache-Control: no-store` present, one `link_clicks` row.
- `GET /r/{unknown}` → 404, no rows written.
- Crawler User-Agent → still 302, but **no** `link_clicks` row.
- Click-log failure (patch insert to raise) → still 302.
- `GET /healthz` → 200.

## Definition of done

- `make lint` and `make test` pass (mypy scope must be extended to cover `web/`).
- Migration applies cleanly on a database at head `d2e3f4a5b6c7`, and `downgrade` works.
- With `LINK_TRACKING_ENABLED=false` (the default) behavior is byte-for-byte identical to today.
