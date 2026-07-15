# Codex brief — Fix: `sources validate` must not use (or mutate) conditional-GET state

Follow-up to `docs/codex_brief_source_metadata.md` (merged). The metadata/schema work is correct.
The `validate` command has a real defect. **No schema change, no migration.**

## The defect

`RSSSource.fetch()` does a conditional GET: it loads a stored `ETag` / `Last-Modified` from
`raw_storage_path` (`load_state`), sends them as `If-None-Match` / `If-Modified-Since`, and on a
`304 Not Modified` returns **zero** articles. On any non-304 response it calls `save_state()`,
overwriting the stored validators.

`validate_sources_command` treats zero articles as failure ("no parseable articles returned"). So:

1. **False FAIL.** On any environment that runs the pipeline regularly (i.e. the server), each healthy
   feed already has a stored validator. `validate` will get `304` → zero articles → report `FAIL` for
   a perfectly good feed. That is the exact opposite of the command's purpose, which is to prove a
   feed is real and live.

2. **Corruption of ingest state.** `fetch()` calls `save_state()`, so running `validate` overwrites the
   `ETag`/`Last-Modified` that the real pipeline relies on. A `validate` run right before a pipeline
   run can make the pipeline receive a `304` and silently skip articles it would otherwise ingest. A
   diagnostic command must never perturb ingestion.

## The fix

Give `fetch` an explicit way to bypass conditional-GET caching, and have `validate` use it.

### `Source.fetch` (`engine/sources/base.py`)

Add a keyword-only parameter to the abstract signature, defaulting to today's behavior:

```python
@abstractmethod
async def fetch(
    self,
    since: datetime | None = None,
    *,
    use_cache: bool = True,
) -> AsyncIterator[RawArticle]:
    ...
```

### `RSSSource.fetch` (`engine/sources/rss.py`)

When `use_cache` is `False`:

- Do **not** read stored state — send no `If-None-Match` / `If-Modified-Since` headers (so the server
  returns a full `200`, never a `304`).
- Do **not** call `save_state()` — leave the pipeline's stored validators untouched.

When `use_cache` is `True` (the default, used by the pipeline), behavior is exactly as today.

### `validate` (`engine/cli/sources.py`)

Call `source.fetch(use_cache=False)`. Everything else about the command stays: per-source
`asyncio.timeout`, count > 0 or `FAIL`, non-zero exit on any failure.

## Tests

- `RSSSource.fetch(use_cache=False)` sends no conditional headers and does not call `save_state`
  (assert the stored state file / validators are unchanged after the call). Use the existing RSS test
  fakes; do not hit the network.
- `RSSSource.fetch()` (default) still sends conditional headers and still saves state — i.e. the
  pipeline path is unchanged (the existing RSS tests should keep passing verbatim).
- `validate` reports `OK` for a feed that would return `304` under caching but returns items when
  caching is bypassed (a regression test for the false-FAIL bug). Simulate with a fake source, no
  network.

## Definition of done

- `make lint` and `make test` pass.
- No migration, no schema change.
- Pipeline fetch behavior (conditional GET + state save) is byte-for-byte unchanged; only `validate`
  bypasses the cache.
