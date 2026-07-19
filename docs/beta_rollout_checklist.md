# Beta rollout checklist

Operator runbook for shipping the 9-feature stack to the server and going live with ~30 beta users.
Run top to bottom. Each phase has a **verify** step — do not proceed until it passes.

Server: project at `/root/NewsAnalyzer/`. Collection and delivery use separate cron entries described
in Phase 4. Listener is systemd `newsanalyzer-listener.service`
(long-running — **must be restarted** to pick up new code/env).

Migration head after this stack: **`1a2b3c4d5e6f`**.

Everything new is behind a flag or gated on user state; with flags off and no invited users, behavior
is identical to today. Turn features on **one at a time**.

---

## Phase 0 — Safety net (before touching anything)

- [ ] **Back up the database.** Neon: take a branch/snapshot of the production DB so you can restore.
      Do not skip — several migrations are additive but `onboarding` makes `users.profile` nullable and
      its downgrade will fail if invited rows exist.
- [ ] Note the current migration revision: `cd /root/NewsAnalyzer && uv run alembic current`. Write it
      down — that is your rollback target.
- [ ] Confirm you are deploying the single branch that bundles all 9 features, tests green locally.

## Phase 1 — Deploy code (flags still off)

- [ ] Pull/checkout the bundled branch on the server.
- [ ] Install deps: `cd /root/NewsAnalyzer && uv sync`.
- [ ] **Do not restart the listener yet** and **do not run the cron** until migrations are applied
      (new code against old schema will error). Confirm the server clock configuration with
      `timedatectl`; the cron below declares UTC explicitly and therefore does not depend on it.

## Phase 2 — Migrations

- [ ] `cd /root/NewsAnalyzer && uv run alembic upgrade head`
- [ ] **Verify:** `uv run alembic current` shows `1a2b3c4d5e6f`.
- [ ] **Verify** the new tables exist (no error): the stack adds `digest_links`, `link_clicks`,
      `ui_events`, `delivery_batches`, `onboarding_state`, `users`, plus columns on `sources`,
      `digests`, `decisions`. A quick check: `uv run python -m engine users list` should run without a
      schema error (it will be empty until Phase 3).

## Phase 3 — Seed your own account (behavior-preserving)

The pipeline/delivery now read the profile from the `users` table, not YAML. Seed yourself so nothing
changes for you.

- [ ] `cd /root/NewsAnalyzer && uv run python -m engine users seed-self`
      (reads `config/profiles/volodymyr.yaml` + `TELEGRAM_CHAT_ID` + `UI_LANGUAGE`, idempotent).
- [ ] **Verify:** `uv run python -m engine users show volodymyr` prints your profile;
      `uv run python -m engine users list` shows one enabled user with your chat_id.

## Phase 4 — Restart listener on new code (still single-user, flags off)

- [ ] Split `news-pipeline.sh`: it must run `uv run python -m engine run` only. Remove its old
      unconditional `delivery send`; scheduled delivery is now the separate hourly command below.
- [ ] Restore and uncomment both cron jobs, declaring their timezone explicitly:
      ```cron
      CRON_TZ=UTC
      30 3 * * * cd /root/NewsAnalyzer && ./news-pipeline.sh
      0 * * * * cd /root/NewsAnalyzer && uv run python -m delivery send-due
      ```
      The server timezone was previously undocumented, so `CRON_TZ=UTC` makes the schedule
      unambiguous. The supported cohort reaches UTC+3. Budgeting two hours for a 03:30 UTC collection
      gives a 05:30 UTC finish; the earliest 09:00 local slot is 06:00 UTC at UTC+3, leaving a
      30-minute margin (`09:00 - 03:00 = 06:00 >= 05:30`).

- [ ] `systemctl restart newsanalyzer-listener.service && systemctl is-active newsanalyzer-listener.service`
- [ ] **Verify:** send yourself a like/dislike on an existing digest — feedback still acks. Check the
      service didn't crash: `systemctl status newsanalyzer-listener.service --no-pager`.
- [ ] **Verify** a normal pipeline+delivery still works for you: run one cycle manually
      (`uv run python -m engine run` then `uv run python -m delivery send --limit 3`) and confirm you
      receive digests as before. See the **security note** on `delivery send` below.

> At this point the whole multi-user data layer is live but you are the only user and every optional
> flag is off. This is the safe baseline. Everything below is opt-in.

## Phase 5 — Source metadata sanity (no flag; safe)

- [ ] Sync sources with their new metadata: `uv run python -m engine sources sync`.
- [ ] **Validate feeds actually fetch:** `uv run python -m engine sources validate`
      (fetches each enabled feed cache-bypassed; exits non-zero on any dead feed). Fix or disable any
      `FAIL` before onboarding real users — a dead feed silently starves a topic.
- [ ] `uv run python -m engine sources list` — eyeball lang/country/topics coverage.

## Phase 6 — Turn on batched delivery (optional, independent)

Gate new top-level digests behind one "N ready" notification. No external infra needed.

- [ ] Set the flag (short idempotent one-liner — the terminal breaks long pasted lines; never print
      `.env`):
      `cd /root/NewsAnalyzer && sed -i '/^BATCHED_DELIVERY_ENABLED=/d' .env && echo 'BATCHED_DELIVERY_ENABLED=true' >> .env && grep -n '^BATCHED_DELIVERY_ENABLED=' .env`
- [ ] Optionally tune `BATCH_REVEAL_PAGE_SIZE` (default 5) and `BATCH_NUDGE_AFTER_DAYS` (default 3) the
      same way.
- [ ] Restart listener (reveal buttons run in it):
      `systemctl restart newsanalyzer-listener.service`
- [ ] **Verify live** (this is the part unit tests cannot cover):
  - run a delivery cycle → you get **one** "N digests ready" message, not N messages;
  - tap **Показать** → up to 5 digests arrive with feedback buttons; a **Показать ещё** button if more;
  - tap it → the rest arrive; the notification flips to the "all shown" terminal text;
  - run another cycle with new digests before opening → the existing notification's **count is edited**,
    no second push.

## Phase 7 — Link tracking (optional; needs the web service + domain)

Only do this once the redirect service is reachable over HTTPS. If you have not set up the domain +
Caddy yet, **leave `LINK_TRACKING_ENABLED=false`** and come back — delivery works fine without it.

### 7a — Web redirect service

- [ ] Buy a domain, point `l.<domain>` (A record) at the server; open ports 80/443
      (`ufw allow 80/tcp && ufw allow 443/tcp`).
- [ ] Install Caddy; `/etc/caddy/Caddyfile`:
      ```
      l.<domain> {
          reverse_proxy 127.0.0.1:8080
      }
      ```
      `systemctl reload caddy`.
- [ ] Run the redirect app as a service: `uv run python -m web` (binds `127.0.0.1:8080`). Wrap it in a
      systemd unit (e.g. `newsanalyzer-web.service`) so it survives reboots, mirroring the listener unit.
- [ ] **Verify:** `curl -i https://l.<domain>/healthz` → `200 {"status":"ok"}`.

### 7b — Enable the flag

- [ ] `cd /root/NewsAnalyzer && sed -i '/^LINK_TRACKING_ENABLED=/d;/^REDIRECT_BASE_URL=/d' .env && printf 'LINK_TRACKING_ENABLED=true\nREDIRECT_BASE_URL=https://l.<domain>\n' >> .env && grep -nE '^(LINK_TRACKING_ENABLED|REDIRECT_BASE_URL)=' .env`
- [ ] Restart listener; the next `delivery send` mints tracked links.
- [ ] **Verify:** a delivered digest's source links point at `l.<domain>/r/…`; clicking one lands on the
      real article and writes a `link_clicks` row.

## Phase 8 — Onboard the first real beta user

- [ ] Get the person's Telegram `chat_id` (they can message the bot; the listener logs the ignored
      unknown chat — read the id from the log, or use @userinfobot).
- [ ] `uv run python -m engine users invite --username <slug> --chat-id <id> [--ui-language ru|en]`
      (creates an **invited** row: no profile, disabled).
- [ ] Tell them to open the bot and press **/start**.
- [ ] **Verify the full funnel yourself first** using a second Telegram account as the guinea pig:
  invite it, `/start`, answer all ~10 questions, confirm the summary → `users list` shows it as
  **enabled** with a synthesized profile; next pipeline run selects for it and delivers to *their* chat.
- [ ] Only after that clean run, invite the real cohort in small waves (e.g. 5 at a time) so a surprise
      doesn't hit all 30 at once.

## Phase 9 — Watch the funnel

- [ ] After a day or two: run `audit/onboarding_funnel.sql` — invited → started → per-step → completed.
      The earliest big drop-off is your first signal.
- [ ] `audit/ui_usage.sql` — which buttons get used / ignored.
- [ ] `audit/batch_open_rate.sql` — **the kill metric.** Open rate < 30% in week 2 = the delivery
      hypothesis is not validated. Decide that threshold now, not after.

---

## Security (do before inviting real people)

- [ ] **Rotate the Telegram bot token** via @BotFather if the current one ever appeared on camera or in
      shared logs. Update `.env`, restart the listener.
- [ ] Verify delivery logs no longer contain Telegram request URLs. The application now raises the
      `httpx` and `httpcore` loggers to WARNING; rotate the bot token if older logs exposed it.
- [ ] `.env` holds live secrets — never `cat`/print it; the one-liners above only append and grep by
      key.

## Rollback

- Feature misbehaves → flip its flag back to `false` and restart the listener; no code revert needed.
- Schema problem → restore the Neon snapshot from Phase 0 (cleanest), or
  `uv run alembic downgrade <revision from Phase 0>`. Note: downgrading past the onboarding migration
  fails if any invited user has a NULL profile — delete invited rows first, or just restore the
  snapshot.

## Deferred (not blocking beta)

- Profile view/edit card (task 3) — separate brief.
- Per-user source subscriptions (task 2, the `user_sources` half) — separate brief. Until then every
  user sees the shared event set scoped by their synthesized profile.
