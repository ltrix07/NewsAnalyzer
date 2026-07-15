# Codex brief — Batched delivery ("N digests are ready" gate)

## Why

Today `deliver_pending` pushes every ready digest to Telegram as its own message. Two problems for
the beta:

1. **Spam.** A run can produce 5–12 digests; the user gets that many messages back to back whether
   or not they were ready to read.
2. **No engagement signal.** We cannot tell whether the bot solves anyone's problem, because we have
   no measure of who actually *reads* the digests versus who just receives them.

The fix is a reveal gate. Instead of pushing digests, the bot sends **one** notification — "N digests
are ready, tap to view" — and only sends the actual digests after the user taps **Показать**. This
de-spams delivery and, more importantly, turns "did the user open it" into a first-class metric.
This is the metric the beta lives or dies on: if few users ever tap Показать, the product does not
work, and we need to know that number.

## The core insight (do not add state you don't need)

The digest lifecycle already encodes reveal state:

- `digests.delivered_at IS NULL` — ready but **not yet shown** (this is what `_pending_digests_query`
  already selects).
- `digests.delivered_at IS NOT NULL` + `telegram_message_id` — **shown**; an `Impression` exists and
  the digest can act as a thread parent (`_find_thread_parent` already requires exactly this).

So "reveal" is precisely what the current inner loop of `deliver_pending` already does per digest
(send message → set `delivered_at` + `telegram_message_id` → write `Impression`). We are not changing
what a reveal *is*; we are changing **when** it fires — from the cron to the user's tap — and doing it
N at a time. **`Impression` and `delivered_at` move to reveal time.** Do not write an `Impression` at
notification time; that would re-break the very metric we are adding.

## Feature flag (mandatory)

```python
batched_delivery_enabled: bool = False
```

When **false**, delivery behaves byte-for-byte as it does today (direct push of every pending
digest). The operator currently uses the bot daily in direct mode; batch mode must not switch on
until it is tested. Env: `BATCHED_DELIVERY_ENABLED`.

Everything below is gated on this flag.

## Decisions already made (build to these)

- **Accumulate into one batch.** If a previous batch is still unopened when the next run produces new
  digests, add them to the **same** open batch and **edit** the existing notification message with the
  new count. No second push on the normal daily path.
- **Re-engagement nudge.** If an open batch has been unopened for `batch_nudge_after_days` (default
  **3**) days, send **one** deliberate push to re-engage ("You have N unread digests"), and do not
  nudge again for another `batch_nudge_after_days`. This is the *only* case that pushes a second
  notification.
- **Reveal top-N, then "Показать ещё".** On Показать, reveal the top `batch_reveal_page_size`
  (default **5**) unrevealed digests by score; if more remain, offer **Показать ещё N**.
- **Threads go direct.** Thread updates (silent same-story replies) are NOT gated. They keep sending
  immediately as today. Only new top-level digests are batched.

## Config

```python
batched_delivery_enabled: bool = False
batch_reveal_page_size: int = 5
batch_nudge_after_days: int = 3
```

Env vars mirror these. Add to `.env.example` with comments.

## Data model

One new table. Chain the migration off head `f4a5b6c7d8e9` (`add_ui_events`).

### `delivery_batches`

| column                    | type                        | notes                                            |
| ------------------------- | --------------------------- | ------------------------------------------------ |
| `id`                      | BigInteger PK               |                                                  |
| `chat_id`                 | BigInteger, not null        | recipient                                        |
| `notification_message_id` | BigInteger, nullable        | the "N ready" message we edit; null until first sent |
| `created_at`              | timestamptz, not null       | `now()` — when the batch opened                  |
| `notified_at`             | timestamptz, nullable       | when the first notification was sent             |
| `last_nudge_at`           | timestamptz, nullable       | nudge dedup                                      |
| `opened_at`               | timestamptz, nullable       | first Показать tap — **the open-rate metric**    |
| `closed_at`               | timestamptz, nullable       | set when every digest in the batch is revealed   |

Index: `("chat_id")` filtered to open batches is fine as a plain `("chat_id", closed_at)` index.

Add `batch_id` (BigInteger, nullable, FK `delivery_batches.id`) to `digests`. A digest is attached to
the batch it was notified in. Nullable because: (a) direct-mode digests never get one, (b) thread
updates never get one.

An **open batch** for a chat = the row with `closed_at IS NULL` (there is at most one per chat; the
notify step must enforce this — reuse it, don't create a second).

## Delivery (notify) — refactor `deliver_pending`

When `batched_delivery_enabled` is true, the cron `delivery send` no longer sends new digests. It
does this, in one `session_scope`:

1. Load pending digests (`delivered_at IS NULL`), rank them with the existing `_rank_pending_digests`.
2. For each, run `_find_thread_parent` (unchanged). **If a parent is found → send the silent reply
   now and mark it revealed exactly as today** (this is the "threads go direct" path — keep the
   existing code path, including its best-effort try/except and link minting).
3. For each digest **without** a parent (a new top-level digest): do **not** send it. Ensure there is
   an open `delivery_batches` row for the chat (create if none), set `digest.batch_id` to it, and
   leave `delivered_at` NULL.
4. After processing all: if the open batch gained any new digests, compute the current unrevealed
   count and **upsert the notification** (see below).
5. Independently, run the **nudge check** (see below).

Direct mode (flag false) keeps the current loop untouched.

### The notification message

Content: **count only, no headlines.** Rendering a headline lets the user read the gist from the
message preview and never tap — which destroys the open metric. Use a string like
`t("batch_notification", lang)` formatted with the count, plus a single inline button
`t("btn_show_digests", lang)` with callback `build_reveal_callback(batch_id)`.

- First time (`notification_message_id IS NULL`): `send_message`, store the returned `message_id` in
  `notification_message_id`, set `notified_at`.
- Subsequent runs while unopened: `edit_message_text` on the stored `notification_message_id` with the
  new count. **No new send.** (The client already has `edit_message_reply_markup`; add an
  `edit_message_text` wrapper if absent.)
- Editing a message that the user already opened/deleted can 400; treat notification send/edit as
  best-effort and log `batch_notification_failed`, never crash the run.

### The nudge

In the same run, for the chat's open batch: if `opened_at IS NULL` and `notified_at` is older than
`batch_nudge_after_days`, and (`last_nudge_at IS NULL` or older than `batch_nudge_after_days`), send a
distinct `t("batch_nudge", lang)` push (a real new message, notification enabled) and set
`last_nudge_at`. Best-effort.

## Reveal — listener

Add to `delivery/keyboards.py`: `reveal` and `reveal_more` actions with `build_reveal_callback(batch_id)`
(`rev:<id>`) and `build_reveal_more_callback(batch_id)` (`revm:<id>`), plus their `parse_callback_data`
branches returning a payload carrying `batch_id`. (These target a batch, not a digest — extend the
payload or add a parallel one; keep it typed, not raw strings.) Add both to `KeyboardAction` so they
flow through the existing `ui_events` choke point and are logged for free.

In `delivery/listener/handlers.py`, handle `reveal` / `reveal_more`:

1. Load the batch; verify it belongs to `chat_id` (ignore otherwise).
2. If `opened_at IS NULL`, set it now (first open — the metric).
3. Select the batch's unrevealed digests (`batch_id = :id AND delivered_at IS NULL`), ranked by the
   same scoring used at notify time, take the next `batch_reveal_page_size`.
4. For each: **reveal it** — this is the existing per-digest send path factored out of
   `deliver_pending`: `send_message` with the digest text (+ tracked links if enabled) and the
   feedback keyboard, then set `delivered_at` + `telegram_message_id` and write the `Impression`.
   Reuse that code; do not duplicate the formatting/minting logic — extract a shared
   `reveal_digest(...)` helper that both the direct-mode loop and the reveal handler call.
5. After the page: count remaining unrevealed in the batch. If > 0, send/attach a **Показать ещё N**
   button (`reveal_more`). If 0, set `closed_at`, and edit the original notification to a terminal
   state (`t("batch_all_shown", lang)`, no button) — best-effort.

Post-commit safety: like the rest of the listener, actual Telegram sends belong to the post-commit
phase pattern already used (`HandlerResult`), so a send failure does not roll back the reveal
bookkeeping into an inconsistent state. Follow the existing structure — do not send inside the DB
transaction if the file's convention is to defer.

## Strings (`delivery/strings.py`)

Add ru + en for: `batch_notification` (takes count), `btn_show_digests`, `btn_show_more` (takes
count), `batch_nudge` (takes count), `batch_all_shown`. Keep `t()` fallback behavior.

## Analytics

Add `audit/batch_open_rate.sql`:

1. **Open rate** — of batches with `notified_at` not null, share with `opened_at` not null. **Comment
   the kill-criterion in the file: open rate < 30% in beta week 2 = the delivery hypothesis is not
   validated.**
2. **Time to open** — median/p90 of `opened_at - notified_at`, a proxy for perceived urgency.
3. **Reveal depth** — of opened batches, how many revealed all vs stopped after the first page (did
   Показать ещё get used).

## Tests

- Flag false → delivery is unchanged (existing delivery tests still pass verbatim).
- Flag true, run with new top-level digests → no digest messages sent; exactly one notification with
  the right count and a Показать button; digests remain `delivered_at IS NULL`, attached to one open
  batch; **no `Impression` rows yet.**
- Second run before open → same batch, notification **edited** (count grows), no second send,
  `notified_at` unchanged.
- Thread update under flag true → sent directly as a silent reply, `delivered_at` set, **not** added
  to the batch.
- Показать → top-N digests sent with feedback keyboards, `delivered_at`/`telegram_message_id`/
  `Impression` written for exactly those N, `opened_at` set; remaining digests still pending with a
  Показать ещё N button.
- Показать ещё → next page revealed; when the last is revealed, `closed_at` set and notification
  edited to the terminal string.
- Nudge fires once after `batch_nudge_after_days` on an unopened batch and not again within the
  window; never fires on an opened batch.
- A reveal `ui_events` row is written (choke point) for `reveal` / `reveal_more`.
- Notification edit failure (patch to raise) does not crash the run.

## Definition of done

- `make lint` and `make test` pass.
- Migration up/down clean from head `f4a5b6c7d8e9`.
- With `BATCHED_DELIVERY_ENABLED=false` (default) behavior is identical to today, including threading
  and link tracking.
- `reveal_digest(...)` is a single shared helper; the direct-mode loop and the reveal handler both
  call it (no copy-pasted send/mint/impression logic).
