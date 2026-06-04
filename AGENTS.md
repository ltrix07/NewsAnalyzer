# AGENTS.md

Single source of truth for any AI coding agent (Codex, Claude, etc.) working in this repository. Read it fully at the start of every task. Re-read it when in doubt. Update it when a principle, abstraction, or convention changes.

## 1. Project

**Personal news aggregator with a verification cascade.**

A single-user-first system that:

1. Pulls news from RSS feeds, Telegram channels, and official sources (Ukrainian, Polish, and financial).
2. Clusters articles into events using embeddings.
3. Filters events through a cost-tiered LLM cascade for personal relevance.
4. Verifies surviving events: cross-source corroboration, speaker authority, hype score.
5. Produces digests through the user's profile lens, with citations to primary sources.

The engine is a CLI-driven Python application. Delivery (Telegram bot, scheduling, dashboards) is a **separate concern and not part of the engine**. Do not add delivery code to `engine/`. `engine run` produces digests; `python -m delivery send` is the operational complement that ships pending digests to Telegram.

## 2. Core principles (do not violate)

**Cascade discipline.** Every article passes through stages of increasing cost. Cheap stages (regex, embeddings, `gpt-4o-mini`) must aggressively cut volume before expensive stages (`gpt-4o`) run. If you find yourself sending hundreds of items to the expensive model, the cascade is broken upstream - fix it there, not by throwing money at the symptom.

**Decision log over silent filtering.** Every filtering or transformation decision MUST write a row to the `decisions` table with stage name, stage version, model, input tokens, output tokens, USD cost, and a structured JSON payload describing what was decided and why. If a decision is not logged, it didn't happen. This is how postmortems are possible when the system misses something important or surfaces noise.

**Structured output everywhere except the final summary.** LLM stages that classify, score, or analyze must return Pydantic-validated objects via OpenAI Structured Outputs with `strict: true`. Free-text output is allowed only in the `Digest.summary` field that is delivered to the user.

**Idempotency.** Running the pipeline twice over the same time window must not duplicate work or output. Use content hashes, dedupe tables, and `decisions` lookups to short-circuit already-processed items.

**Recall over precision in cheap stages.** Cheap stages over-include rather than drop. False positives are paid for by the next stage; false negatives are lost forever.

**Reproducibility.** Any digest the user receives must be traceable: which articles, which cluster, which prompt version, which model, which decision rationale. The `decisions` table plus content-addressed storage make this possible. Do not compromise it.

## 3. Tech stack (locked)

Do not introduce alternatives without explicit user approval.

- Language: **Python 3.11+**
- Package manager: **uv**
- Concurrency: **asyncio** (no Celery, Prefect, Airflow, or Temporal)
- HTTP: **httpx** (async). Never `requests`.
- RSS: **feedparser**
- HTML extraction: **trafilatura**
- Telegram ingestion: **Telethon** (MTProto). Bot API is for delivery only, not for reading channels.
- Database: **PostgreSQL 16** with the **pgvector** extension
- ORM: **SQLAlchemy 2.0** (async)
- Migrations: **Alembic**
- Validation / DTOs: **Pydantic v2**
- Settings: **pydantic-settings**, reading from `.env`
- Prompt templates: **Jinja2**
- LLM SDKs: **openai** (official) - used for both embeddings and chat completions
- Logging: **structlog** (JSON to stdout)
- CLI: **typer**
- Hashing: **xxhash**
- Tests: **pytest**, **pytest-asyncio**, **vcrpy** for HTTP fixtures, hand-rolled fixtures for LLM responses
- Local infra: **docker compose** (`app + postgres`)

If a task seems to require something outside this list, stop and ask. Do not silently add `pandas`, `flask`, `requests`, or `langchain`.

## 4. Directory layout

```text
engine/
├── __main__.py            # CLI entrypoint (typer)
├── config.py              # settings, sources registry loader, profile loader
├── db.py                  # async engine, session factory
├── models.py              # SQLAlchemy ORM
├── domain.py              # Pydantic domain DTOs
├── sources/
│   ├── base.py            # Source ABC + RawArticle
│   ├── rss.py
│   ├── telegram.py
│   └── html.py
├── stages/
│   ├── base.py            # Stage ABC, StageResult, Context
│   ├── ingest.py          # extract + normalize + dedupe
│   ├── embed.py
│   ├── cluster.py
│   ├── filter_rules.py    # keyword / regex prefilter, no LLM
│   ├── relevance.py       # gpt-4o-mini
│   ├── verify.py          # gpt-4o
│   └── summarize.py       # gpt-4o
├── llm/
│   ├── client.py          # OpenAI wrapper: retries, structured outputs, cost accounting
│   ├── schemas.py         # Pydantic schemas for structured outputs
│   └── prompts/           # *.j2 files; version is part of the filename
├── observability.py       # decision writer, metrics, cost rollups
├── pipeline.py            # run_once(profile)
└── cli/                   # typer subcommands
delivery/
├── __init__.py            # Delivery-only package; may import engine config/db/models/profile but never engine.cli/stages/pipeline/sources
├── __main__.py            # Typer CLI: python -m delivery {send|test|list}
├── client.py              # Telegram Bot API wrapper using httpx
├── formatter.py           # Digest -> Telegram HTML message
└── dispatcher.py          # Pull undelivered digests, send, mark delivered
config/
├── sources.yaml           # source registry
└── profiles/
    └── <name>.yaml        # user profile
migrations/                # Alembic
tests/
└── fixtures/              # recorded RSS/HTML and mocked LLM responses
docker-compose.yml
.env.example
AGENTS.md                  # this file
```

A new cascade feature adds files only under `stages/`, `llm/prompts/`, and `llm/schemas.py`. Do not create parallel hierarchies.

## 5. Data flow

Strict left-to-right type progression. A later type never appears before its predecessor.

```text
RawArticle        # returned by Source.fetch(); not persisted
  ↓ stages/ingest.py
Article           # persisted; deduped by url_hash + content_hash (xxhash64)
  ↓ stages/embed.py
Embedded          # Article + vector in pgvector
  ↓ stages/cluster.py
Event             # cluster of 1..N Articles within a time window
  ↓ stages/filter_rules.py
Event (passed)    # cheap deterministic prefilter
  ↓ stages/relevance.py     [gpt-4o-mini, structured output]
ScoredEvent       # + RelevanceVerdict
  ↓ stages/verify.py        [gpt-4o, structured output]
VerifiedEvent     # + VerificationReport
  ↓ stages/summarize.py     [gpt-4o, mixed: structured wrapper + free-text summary]
Digest            # final user-facing object; references Event and citations
```

Each arrow is exactly one Stage. Each Stage writes one `decisions` row per input it sees, whether the input passes, is filtered, or errors out.

## 6. Key abstractions

**Source** (`sources/base.py`):

```python
class Source(ABC):
    name: str
    kind: Literal["rss", "telegram", "html", "api"]

    @abstractmethod
    async def fetch(self, since: datetime) -> AsyncIterator[RawArticle]:
        ...
```

**Stage** (`stages/base.py`):

```python
class Stage(ABC, Generic[In, Out]):
    name: str
    version: str   # bump on prompt change or logic change

    @abstractmethod
    async def process(self, item: In, ctx: Context) -> StageResult[Out]:
        ...
```

**StageResult**:

```python
@dataclass
class StageResult(Generic[T]):
    output: T | None        # None means filtered out
    decision: Decision      # always present, always logged
    cost_usd: float
```

**Decision** is the row written to `decisions`. Stage authors fill `decision_json` with stage-specific context: the verdict, the rationale, references to input/output rows. The schema of `decision_json` is owned by the stage and is part of what bumps `stage.version`.

## 7. LLM rules

**Model assignment** is fixed per stage and lives in configuration, not stage code:

- `filter_rules`: no LLM.
- `relevance`: **gpt-4o-mini**. Structured output via OpenAI Structured Outputs.
- `verify`: **gpt-4o**. Structured output via OpenAI Structured Outputs.
- `summarize`: **gpt-4o**. Mixed: structured wrapper schema with a free-text `summary` field inside.

**Prompts** live in `engine/llm/prompts/<stage>_<version>.j2`, e.g., `relevance_v3.j2`. Never edit an existing version in place - create the next version, update the stage's `version` attribute, and let A/B comparisons be done via `decisions`.

**The user profile** is injected into prompts via Jinja, never hardcoded inside Python. The profile YAML at `config/profiles/<name>.yaml` carries at minimum: `location`, `citizenship`, `languages`, `interests` (list), `not_interested` (list), `output_language`.

**Structured output** is implemented via OpenAI Structured Outputs with `strict: true`, using a Pydantic model as the schema source. Never parse free-text JSON with regex. The single helper `llm.client.call(...)` returns the parsed model plus a `Usage` object that includes tokens and computed USD cost.

**Cost accounting**: every LLM call records prompt tokens, completion tokens, and USD cost in the `decisions` row. Aggregate cost per pipeline run must be a single SQL query away.

## 8. Database conventions

- Timestamps are UTC, stored as `TIMESTAMPTZ`. Application code uses timezone-aware `datetime`.
- IDs are `BIGINT` autoincrement, except for natural-key tables.
- Content hashes (`url_hash`, `content_hash`) are `BYTEA` from `xxhash.xxh64`.
- The `decisions` table is **append-only**. Never `UPDATE` or `DELETE` rows. Corrections are new rows that reference the prior decision.
- Migrations are generated with `alembic revision --autogenerate`, then **inspected by a human** before being committed. Autogenerated diffs are starting points, not gospel.

## 9. Coding conventions

- All I/O is `async`. Sync code is allowed only inside CPU-bound helpers (hashing, parsing).
- Type hints on everything public; `mypy --strict` passes on the `engine/` and `delivery/` packages.
- No `print()`. Use `structlog`.
- No module-level mutable state. Configuration and database sessions flow via the `Context` object passed to stages.
- Functions over classes when there is no state to keep. Classes are reserved for `Source`, `Stage`, and ORM models.
- Imports: stdlib, third-party, local - separated by blank lines, sorted by `ruff`.
- Line length 100. Format with `ruff format`. Lint with `ruff check`.
- Tests live under `tests/`, mirroring the package layout.

## 10. Antipatterns to avoid

- Sending raw article batches to `gpt-4o` without clustering and `gpt-4o-mini` filtering first.
- Editing an existing prompt file in place instead of creating a new version.
- Returning free-text from any cascade stage other than `summarize`.
- Hardcoding the user profile inside stage code or prompts.
- Introducing a second runtime (worker process, queue, scheduler) - the engine is one process.
- Storing secrets in code. Secrets live in `.env` only.
- Calling any synchronous HTTP library.
- Catching broad `Exception` without structured logging and re-raising or returning a typed failure.
- Writing code without an accompanying test for the new branch.
- Renaming or refactoring across the codebase opportunistically inside a feature task. Refactors are their own task.

## 11. Definition of done (per task)

A task is complete only when all of these hold:

- Code compiles, type-checks, and lints clean.
- New behavior has at least one test for the happy path and one for an edge case.
- A CLI command or `pytest` invocation demonstrates the new behavior end-to-end.
- New configuration knobs have defaults in code and an entry in `.env.example`.
- The `decisions` table contains rows for any stage exercised by the new code.
- This file (`AGENTS.md`) is updated if a principle, abstraction, convention, or stack item changed.

## 12. Glossary

- **Article**: one piece of content from one source.
- **Event**: a cluster of Articles describing the same real-world happening within a time window.
- **Cascade**: the ordered chain of stages, cheap to expensive.
- **Decision**: one logged row describing a stage's choice for one input.
- **Digest**: the final user-facing object - one Event seen through the user's profile lens, with citations.
- **Profile**: the YAML describing what the current user cares about.
- **Stage**: a unit of pipeline work; single input type to single output type; always writes a decision.

## 13. Delivery

- Delivery is outbound-only and lives under `delivery/`, never `engine/`.
- Delivery reads persisted `Digest` rows, sends them via the Telegram Bot API using `httpx`, and marks success with `digests.delivered_at`.
- `python -m delivery send` is idempotent at the digest row level: non-NULL `delivered_at` means "already sent".
