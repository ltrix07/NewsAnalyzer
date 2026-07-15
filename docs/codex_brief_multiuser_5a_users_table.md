# Codex brief — Multi-user 5a: users table + profile from DB (data layer only)

## Context and why this is split

Multi-user support touches the pipeline, delivery, and the listener at once. To keep it reviewable it
is split in two:

- **5a (this brief):** introduce a `users` table, move the profile out of YAML into the database, add
  an operator CLI to manage users, and seed the current profile. Rewire the code that *reads* a
  profile to read it from the DB. **This brief must not change runtime behavior for the existing
  single user** — same digests, same delivery, same everything. It is a data-layer swap.
- **5b (next brief, do not build here):** run selection per user and loop delivery + the listener over
  all enabled users; move taste ranking and `ui_language` to per-user.

Do **not** build the per-user pipeline loop, per-user delivery, per-user taste, or source
subscriptions here. Those are 5b and later. Staying in scope is the whole point of the split.

## The key idea (minimal-ripple migration)

Today the profile identifier is a string (`settings.profile_name`, default `"volodymyr"`) used to
load `config/profiles/<name>.yaml`. We keep that string as the identifier but change its **source**:
it becomes `users.username`, and the profile is loaded from the DB row instead of the YAML file.
Nothing downstream needs to learn a new identifier type — only the loader changes.

## Data model

New table `users`. Chain the migration off head `b6c7d8e9f0a1` (`add_source_metadata`).

| column         | type                    | notes                                                        |
| -------------- | ----------------------- | ------------------------------------------------------------ |
| `id`           | BigInteger PK           |                                                              |
| `username`     | String, not null        | unique; the profile slug (`^[a-z0-9_]+$`), replaces the YAML filename |
| `chat_id`      | BigInteger, nullable    | unique when set; the Telegram delivery target                |
| `profile`      | JSONB, not null         | conforms to the existing `Profile` pydantic schema           |
| `ui_language`  | String, not null        | default `'ru'`; per-user button language (consumed in 5b)    |
| `enabled`      | bool, not null          | default true                                                 |
| `created_at`   | timestamptz, not null   | `now()`                                                      |
| `updated_at`   | timestamptz, not null   | `now()`, updated on profile edits                            |

Constraints: `UniqueConstraint("username")`, `UniqueConstraint("chat_id")`. `chat_id` is nullable
because a user can exist before their Telegram chat is known (relevant later); for the beta seed it is
set.

**Reuse the existing `Profile` schema — do not redefine the profile fields as columns.** Storing the
profile as validated JSONB means the questionnaire and the profile-edit card (later briefs) just
rewrite one column, and validation stays in one place (`engine/profile.py`).

## Profile resolution

Add a small repository module (e.g. `engine/users.py`) with:

- `async def resolve_profile(username: str, session) -> Profile` — load the row, return
  `Profile.model_validate(row.profile)`. Raise a clear error if no such user.
- `async def get_user_by_username(username, session) -> User | None`
- `async def get_user_by_chat_id(chat_id, session) -> User | None`
- `async def list_enabled_users(session) -> list[User]`

These last three are used by 5b; add them now so 5b is purely call-site changes.

### Rewire the read sites (behavior-preserving)

Replace YAML profile loading with `resolve_profile(...)` at exactly these four sites:

- `engine/cli/filter.py` (`load_profile(profile or settings.profile_name, settings.profile_root)`)
- `engine/cli/score.py` (same)
- `engine/cli/summarize.py` (same)
- `delivery/formatter.py` (`_load_labels` → currently `load_profile(profile_name, _profile_root())`)

Each currently resolves a name then reads YAML; now it resolves the same name against the DB. The
name still defaults to `settings.profile_name` where it did before. For the single seeded user the
resulting `Profile` is identical, so output is unchanged.

`engine/profile.py`'s `load_profile` (YAML reader) is **retained** — but only for the operator import
path below, not for runtime. Do not delete it.

## Operator CLI

Add a `users` Typer app (e.g. `engine/cli/users.py`, wired into the root CLI like `sources`):

- `users add --username <slug> --chat-id <int> --profile <path-to-yaml> [--ui-language ru]` — read the
  YAML with the existing `load_profile`, validate it, store it as the user's `profile` JSONB. Fail if
  the username or chat_id already exists.
- `users list` — table of username, chat_id, enabled, ui_language, output_language (from profile).
- `users show <username>` — pretty-print the stored profile.
- `users enable <username>` / `users disable <username>` — flip `enabled`.

Adding a beta user is thus: operator writes a profile YAML (same shape as today's) and runs
`users add`. The questionnaire (next brief) will later replace the hand-written YAML with generated
profiles, writing the same `users.profile` column.

## Seed the current user

Add an idempotent seed — a CLI command `users seed-self` (or a one-off script) that:

- Reads `config/profiles/volodymyr.yaml`, `settings.telegram_chat_id`, `settings.ui_language`.
- Upserts a `users` row with `username="volodymyr"` (or `settings.profile_name`), that chat_id, that
  ui_language, and the profile JSONB.
- Is safe to run twice (update-in-place, no duplicate).

After seeding, the existing pipeline and delivery — still driven by `settings.profile_name` /
`settings.telegram_chat_id` in 5a — read the profile from this row. The operator runs this once on the
server; document the command in the README.

## Tests

- Migration up/down clean from head `b6c7d8e9f0a1`.
- `resolve_profile` returns a `Profile` equal to what `load_profile` would return for the same YAML
  (round-trip: seed from `volodymyr.yaml`, resolve, assert field-equal). This is the behavior-preserving
  guarantee.
- `resolve_profile` on an unknown username raises a clear error.
- `users add` rejects a duplicate username and a duplicate chat_id.
- `users add` with an invalid profile YAML (bad topic-free profile, e.g. missing required field) fails
  validation and writes no row.
- `enable`/`disable` flips the flag; `list_enabled_users` excludes disabled users.
- `seed-self` is idempotent (running twice yields one row, updated not duplicated).
- The four rewired read sites resolve from the DB (a test that runs filter/score/summarize/formatter
  against a seeded user and gets the same result as the YAML-backed path).

## Definition of done

- `make lint` and `make test` pass.
- Migration up/down clean from head `b6c7d8e9f0a1`.
- With one seeded user, pipeline output and delivery are **identical to today** — no digest content,
  ordering, or delivery change. This brief is observable only through the new `users` table and CLI.
- No per-user loop, no delivery/listener changes, no taste/ui_language relocation — those are 5b.
