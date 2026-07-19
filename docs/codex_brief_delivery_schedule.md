# Codex brief — Per-user delivery time

Context: `docs/design_generalization.md`. News collection stays on a single fixed schedule for
everybody; only **when the digests are handed to each user** becomes personal. This is deliberately the
cheap design: the expensive shared stages keep running once per day in one large batch, so `consolidate`
(which merges only *within a run*) keeps its batch size and the fragmentation work stays effective.
Running the pipeline hourly would quietly undo it.

Independent of the Stage 1 questionnaire work — can be built in parallel. The account menu that lets a
user *change* their slot is a separate brief that comes after Stage 1 lands.

## 1. Schema

Add to `users` (Alembic migration required; current head `f0a1b2c3d4e5`):

- `timezone` — **IANA identifier** (`Europe/Warsaw`), not a UTC offset. An offset silently shifts every
  user's slot by an hour twice a year, and it presents as an inexplicable seasonal bug.
- `delivery_slot` — which of the named slots below the user has chosen.
- `last_delivery_date` — the user's **local** date of the last delivery. This is the once-per-day guard;
  keep it explicit rather than deriving it from `digests`/`delivery_batches`, because the tick runs
  every hour and the guard is the only thing standing between that and repeated sends.

Backfill existing rows with the default timezone and the earliest slot, so current behaviour is
preserved.

Default timezone comes from a new setting for now. Once Stage 1's country config file exists, the
default should be derived from residence country — leave a comment marking that seam, but do not block
on Stage 1.

## 2. Slots, not 24 hours

Offer **3–4 named slots** (morning / day / evening), defined as data with local times. Rationale: fewer
buttons in a funnel whose drop-off is tracked, and — more importantly — every offered slot can be
guaranteed to fall *after* collection. A free choice of hour lets a user pick a time that structurally
cannot contain fresh news.

### The arithmetic that constrains this — verify before choosing times

The cohort spans roughly UTC+1 to UTC+3. A slot is only meaningful if, for the **easternmost** user,
its local time converts to a UTC moment *after* the pipeline has finished:

```
slot_local_time - max_utc_offset  >=  collection_finish_utc
```

With the pipeline cron at `30 7` and a run taking a while, an 09:00 local slot for a UTC+3 user is
06:00 UTC — **before collection**. That user would silently receive the previous day's batch, forever.

Fix this by **moving collection earlier**, not by pushing slots later (pushing them later turns
"morning" into 11:00). Recommend moving the pipeline cron to roughly `30 3`, then verify the inequality
holds for the earliest slot at the maximum supported offset. **Confirm the server's timezone first** —
the whole calculation depends on whether that cron expression is UTC or local, and this must be checked,
not assumed. State the verified numbers in the PR description.

Note the cron is currently commented out on the server; the deploy notes must say it has to be restored
at the new time, alongside the new hourly tick.

## 3. The delivery tick

A new invocation — either a flag on `delivery send` or a sibling command — selects users who are due
and sends only to them. Run it hourly from cron (`0 * * * *`).

**Due means "the slot has passed today and nothing was delivered today", not "the local hour equals the
slot hour."** Equality makes a single failed tick cost that user their entire day; the "has passed"
form self-heals on the next tick. Concretely: the user's local date differs from `last_delivery_date`
**and** their local time is at or past their slot.

`last_delivery_date` must be written in the same transaction as the send, and must reflect the user's
local date, not UTC — otherwise users east and west of UTC get an off-by-one day at the boundary.

Interaction with batched delivery (`BATCHED_DELIVERY_ENABLED`): the "N дайджестов готово" notification
is what should arrive at the slot. Nothing special to build — the notification is already the first
thing `delivery send` emits — but assert it in a test, because a per-user tick plus batching is exactly
where a double-notification would hide.

## 4. Silence the httpx logs in this change

`delivery send` currently logs `POST https://api.telegram.org/bot<TOKEN>/sendMessage` through httpx to
stdout and to `/root/NewsAnalyzer/logs/*.log`. Making delivery hourly multiplies that leak by 24 and
spreads it across every log file of the day.

Raise the `httpx` / `httpcore` loggers to WARNING in `engine/observability.py`. This is one line now and
a log-scrubbing exercise later. It is in scope for this brief specifically because this change is what
amplifies the exposure.

## Tests

- Due-selection: a user whose slot has passed and who has no delivery today is due; the same user after
  delivery is not due on the same local date; the same user on the next local date is due again.
- Self-healing: a user whose slot passed several hours ago (simulating a missed tick) is still due.
- Timezone correctness: two users with the same slot in different timezones become due at different UTC
  moments. Use real IANA zones.
- **DST**: a user in a zone crossing a DST boundary keeps the same local slot time across the
  transition. This is the test that justifies storing IANA identifiers and it must be explicit.
- Local-date boundary: `last_delivery_date` is the user's local date — assert with a user whose local
  date differs from the UTC date at the moment of sending.
- With batching enabled, one due tick produces exactly one notification, and a second tick in the same
  local day produces none.
- Backfilled existing users keep current behaviour.

## Definition of done

- `make lint` and `make test` pass. Report the **executed** test count, not the collected count — a run
  with many skips means the DB fixture is skipping (`tests/conftest.py:89`) and proves nothing.
- Migration applies and downgrades cleanly.
- Deploy notes state: the new pipeline cron time (with the verified arithmetic), the new hourly tick
  cron line, and that both must be uncommented.

## Out of scope

- The account menu where the user changes their slot — separate brief, after Stage 1. Until then the
  slot is set by backfill/default and is only changeable by an operator.
- Any change to what is collected or how it is selected.
- Per-user *collection* schedules — explicitly rejected: it would shrink `consolidate` batches and
  regress the fragmentation fix.
