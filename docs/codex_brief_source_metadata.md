# Codex brief — Source metadata + validation (foundation for auto-selection)

## Why

The source list (`config/sources.yaml`, 8 feeds) was hand-picked for one person's interests. For the
beta we need two things:

1. Each source **described** in structured, queryable terms — topic tags, language, country, a
   human-readable description — so that a later questionnaire pipeline can auto-select which sources a
   new user gets, instead of us hand-tuning a profile per person.
2. A way to **expand and trust** the list: adding feeds must be safe, because a dead or wrong RSS URL
   silently starves a whole topic.

This brief builds the **metadata schema, the controlled topic vocabulary, and a validation command**.
It does **not** build per-user source subscriptions (that is the multi-user brief) and it does **not**
ask you to invent feed URLs (see the hard constraint below).

## Hard constraint — do not invent feed URLs

**You must not generate, guess, or "recall" RSS/feed URLs.** A hallucinated or rotted URL is worse
than a missing one: it looks configured but delivers nothing, starving a topic invisibly.

- Populate metadata **only** for the sources that already exist in `config/sources.yaml`.
- Do **not** add new source entries with URLs you produced yourself.
- New feeds are added later by a human supplying verified URLs; the `validate` command below is the
  gate that proves a URL is real before it is trusted.

If the brief seems to call for more feeds, stop at the schema + validation + the existing 8. Adding
feeds is explicitly out of scope for you.

## Controlled topic vocabulary

Define these as a module-level constant (e.g. `engine/sources/taxonomy.py`), the single source of
truth that both `SourceConfig` validation and later questionnaire mapping import:

```
ua_war          # Russia's war on Ukraine: front line, strikes, mobilization
ua_domestic     # Ukraine politics, economy, society
pl_legal        # Poland: residence, karta pobytu, work permits, migration law
pl_domestic     # Poland politics, economy, society
eu_policy       # EU policy, geopolitics, temporary protection directive
economy_markets # economy, finance, FX, markets, business
tech            # technology, IT, startups
sport           # sport
crypto          # crypto, digital assets
local           # city / regional local news
```

Keep it a flat, closed set. A source is tagged with **one or more** of these. The questionnaire (a
later brief) will map user answers → a subset of topics → the sources carrying them.

## Schema changes

### `SourceConfig` (`engine/sources/base.py`)

Add four fields. Keep `extra="forbid"`.

```python
description: str                       # one human sentence: what this outlet actually covers
topics: list[str] = Field(min_length=1)  # each must be in the controlled vocabulary
lang: str                              # ISO 639-1, e.g. "uk", "pl", "ru", "en"
country: str | None = None             # ISO 3166-1 alpha-2, e.g. "UA", "PL", or null for supranational
```

Add a validator that rejects any `topics` entry not in the vocabulary, with a message naming the bad
tag and listing the allowed set. `description` must be non-empty. These are **required** for every
source (existing sources are updated below), so their absence should fail config load loudly — a
source with no metadata cannot be auto-selected and must not slip through.

### `Source` model (`engine/models.py`)

Add matching columns:

```python
description: Mapped[str | None] = mapped_column(Text, nullable=True)
topics: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
country: Mapped[str | None] = mapped_column(String(2), nullable=True)
```

Nullable at the DB level (existing rows predate them; the migration backfills nothing), but the
**config layer requires them**, so any synced source will have them populated. Add a GIN index on
`topics` — the questionnaire will filter `WHERE topics ?| array[...]`.

Migration chains off head `a5b6c7d8e9f0` (`add_delivery_batches`).

### `sync_sources_to_db` (`engine/sources/registry.py`)

Extend both the insert and the update branches to carry `description`, `topics`, `lang`, `country`.
This is the one place metadata reaches the DB — do not add a second sync path.

## Populate existing sources

Fill in `description`, `topics`, `lang`, `country` for all 8 sources currently in
`config/sources.yaml`. Base the topic tags and language on each outlet's **known, stable editorial
identity** — do not fetch or guess beyond what the existing `name`/`url` already tell you. Example
shape (fill real values for each):

```yaml
  - name: bankier_rss
    kind: rss
    url: https://www.bankier.pl/rss/wiadomosci.xml
    enabled: true
    poll_interval_seconds: 1800
    lang: pl
    country: PL
    topics: [economy_markets, pl_domestic]
    description: Polish business and finance news portal covering markets, banking and the economy.
```

If you are not confident about a source's topic set, tag it conservatively (fewer, safer tags) rather
than inventing coverage — the same discipline as the URL constraint.

## The `validate` command

Add `engine sources validate` (Typer, alongside the existing `sync` in `engine/cli/sources.py`).

For every **enabled** source in the config it must:

1. Actually fetch the source (reuse the real `Source` implementation / fetch path — not a bespoke
   HTTP call), with a per-source timeout.
2. Assert it yields **at least one parseable article**.
3. Report a per-source line: `OK <name> (<n> items)` or `FAIL <name>: <reason>`.
4. Exit non-zero if any enabled source failed.

This is the gate that makes future URL additions safe: a human adds a feed, runs `validate`, and a
dead or wrong URL fails immediately instead of silently starving a topic. Network calls mean this is
not part of `make test`; it is an operator command.

Also extend the existing `sources list` output to show `lang` / `country` / `topics` so the operator
can eyeball coverage gaps per topic.

## Tests

- `SourceConfig` rejects a `topics` entry outside the vocabulary (error names the bad tag).
- `SourceConfig` rejects empty `topics` and empty `description`.
- A fully specified config round-trips through `sync_sources_to_db` and the row carries
  `description` / `topics` / `lang` / `country`.
- Re-syncing an existing source updates changed metadata (e.g. an added topic) in place.
- All 8 populated `config/sources.yaml` entries load and validate (a test that just calls
  `load_sources_config` on the real file and asserts every entry has non-empty metadata — this keeps
  future edits honest).
- `validate` is covered with a fake source (one returning items → OK, one raising → FAIL, non-zero
  exit). Do not hit the network in tests.

## Definition of done

- `make lint` and `make test` pass.
- Migration up/down clean from head `a5b6c7d8e9f0`.
- `config/sources.yaml` has full metadata for all 8 existing sources and still loads.
- No feed URL in the repo was authored by you — only the 8 that already existed.
- `engine sources validate` exists and fails non-zero on a broken feed.
