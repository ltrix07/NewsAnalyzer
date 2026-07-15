# Codex brief — Fix 5b: scope by username, not profile.name

Follow-up to `docs/codex_brief_multiuser_5b_per_user_selection.md` (merged). The per-user scoping is
structurally correct, but it keys on the wrong field. **No schema change, no migration.**

## The defect

Every per-user stage derives its scoping key as `profile_name = resolved_profile.name`
(`engine/cli/filter.py`, `score.py`, `verify.py`, `summarize.py`), and the summarize **stage** writes
`Digest.profile_name = self.profile.name` (`engine/stages/summarize.py:74`).

`Profile.name` is a free-text human field inside the profile YAML/JSON. The actual user identifier is
`User.username` (the slug, unique, what `resolve_profile` looks up). They are only equal today because
the sole profile happens to have `name: volodymyr`. The two-user test hides the bug because it also
sets `"name": username` for its fixtures (`tests/test_multiuser_selection.py:74`) — code and test
share the same wrong assumption, so both agree.

This breaks the moment `profile.name != username`, which is exactly what the questionnaire (next
brief) produces — it will set `name` to a real display name while `username` is a generated slug. Two
concrete failures:

1. **Delivery crashes.** `Digest.profile_name` becomes the display name; `format_digest` →
   `resolve_profile(digest.profile_name)` looks up `User.username == <display name>` → `LookupError`
   → that user's delivery fails.
2. **Cross-user decision collision.** Two users who both typed the display name "Alex" (different
   usernames) get their decisions scoped under the same "Alex" → the exact cross-user leakage 5b
   exists to prevent.

`profile.name` must never be used as an identifier. The identifier is `username`.

## The fix

The username is already in hand in every per-user command: `profile or settings.profile_name` is the
slug that `resolve_profile` resolves against `User.username`. Use **that** as the scoping key, not
`resolved_profile.name`.

### In each of `filter.py`, `score.py`, `verify.py`, `summarize.py`

Replace

```python
resolved_profile = await resolve_profile(profile or settings.profile_name, session)
profile_name = resolved_profile.name
```

with

```python
username = profile or settings.profile_name
resolved_profile = await resolve_profile(username, session)
profile_name = username
```

Everything else (the `Decision.profile_name == profile_name` sub-query scoping, `Context(...,
profile_name=profile_name)`) stays as written — it just now carries the username.

### In the summarize stage (`engine/stages/summarize.py:74`)

`Digest.profile_name` must be the username, not `self.profile.name`. The stage has `ctx` available and
`ctx.profile_name` now carries the username, so write:

```python
profile_name=ctx.profile_name,
```

(Confirm `ctx` is in scope where the `Digest` is constructed; it is passed to `process`. Do not add a
new constructor field to the stage if `ctx.profile_name` is reachable.)

For the `volodymyr` user this is byte-for-byte identical (name == username), so single-user output is
unchanged.

## Guard against regression: make the test distinguish the two fields

The two-user test currently sets `"name": username`, which cannot catch this class of bug. Change the
fixtures so **`profile.name` differs from `username`** — e.g. `username="alice"` with
`profile["name"]="Alice Display"`, `username="bob"` with `profile["name"]="Bob Display"` (and, ideally,
one case where two users share the same display `name` but differ by username, to lock in the
collision guard).

Then assert:

- decisions are scoped by **username** (`Decision.profile_name in {"alice","bob"}`, never the display
  name);
- each user still gets complete, independent selection over the shared events;
- `Digest.profile_name` equals the **username** for each user's digests;
- `resolve_profile(digest.profile_name, session)` succeeds for every produced digest (this is the
  assertion that would have caught the delivery crash).

## Definition of done

- `make lint` and `make test` pass.
- No migration, no schema change.
- Single-user (`volodymyr`) output is identical to today.
- The two-user test uses `profile.name != username` and passes, proving decisions and
  `Digest.profile_name` key on username; `resolve_profile` round-trips for every produced digest.
