"""Tests for source configuration, raw article validation, and source registry loading."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from engine.cli.sources import app as sources_app
from engine.models import Source as SourceModel
from engine.sources import registry
from engine.sources.base import RawArticle, Source, SourceConfig
from engine.sources.registry import (
    UnknownSourceKindError,
    build_source,
    load_sources_config,
    register_source,
)

SOURCE_METADATA = {
    "description": "Example financial news source.",
    "topics": ["economy_markets"],
    "lang": "pl",
    "country": "PL",
}


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, type[Source]]]:
    """Isolate the source registry for each test and assert no registrations leak out."""

    test_registry: dict[str, type[Source]] = {}
    monkeypatch.setattr(registry, "_REGISTRY", test_registry)
    yield test_registry
    test_registry.clear()
    assert registry._REGISTRY == {}


def test_source_config_rejects_unknown_fields() -> None:
    """Unknown fields should fail validation because source config is strict."""

    with pytest.raises(ValidationError):
        SourceConfig(name="bankier_rss", kind="rss", unexpected=True, **SOURCE_METADATA)


def test_source_config_rejects_invalid_kind() -> None:
    """Kinds outside the supported source literal set should fail validation."""

    with pytest.raises(ValidationError):
        SourceConfig(name="bankier_rss", kind="atom", **SOURCE_METADATA)


def test_source_config_rejects_unknown_topic_with_allowed_set() -> None:
    """Unknown source topics should identify the bad value and controlled vocabulary."""

    with pytest.raises(ValidationError) as exc_info:
        SourceConfig(
            name="bankier_rss",
            kind="rss",
            description="Example source.",
            topics=["made_up_topic"],
            lang="pl",
            country="PL",
        )

    error = str(exc_info.value)
    assert "made_up_topic" in error
    assert "economy_markets" in error


@pytest.mark.parametrize(("description", "topics"), [("", ["tech"]), ("   ", ["tech"]), ("x", [])])
def test_source_config_rejects_empty_metadata(description: str, topics: list[str]) -> None:
    """Descriptions and topic lists are required and must be non-empty."""

    with pytest.raises(ValidationError):
        SourceConfig(
            name="example",
            kind="rss",
            description=description,
            topics=topics,
            lang="en",
        )


def test_raw_article_rejects_missing_html_and_text() -> None:
    """A raw article must carry either HTML or plain text content."""

    with pytest.raises(ValidationError):
        RawArticle(source_name="bankier_rss", url="https://example.com/story")


def test_raw_article_accepts_text_only() -> None:
    """RawArticle should validate when only plain text is present."""

    article = RawArticle(
        source_name="bankier_rss",
        url="https://example.com/story",
        raw_text="plain text",
        published_at=datetime.now(UTC),
    )

    assert article.raw_text == "plain text"
    assert article.raw_html is None


def test_raw_article_accepts_html_only() -> None:
    """RawArticle should validate when only HTML is present."""

    article = RawArticle(
        source_name="bankier_rss",
        url="https://example.com/story",
        raw_html="<p>markup</p>",
    )

    assert article.raw_html == "<p>markup</p>"
    assert article.raw_text is None


def test_build_source_raises_for_unregistered_kind() -> None:
    """A valid but unimplemented source kind should fail at factory time."""

    with pytest.raises(UnknownSourceKindError):
        build_source(SourceConfig(name="future_api", kind="api", **SOURCE_METADATA))


def test_register_source_builds_instance_from_config() -> None:
    """A registered source implementation should round-trip its config."""

    @register_source("rss")
    class DummySource(Source):
        async def fetch(
            self,
            since: datetime | None = None,
            *,
            use_cache: bool = True,
        ) -> AsyncIterator[RawArticle]:
            del use_cache
            if False:
                yield RawArticle(
                    source_name=self.name,
                    url="https://example.com/story",
                    raw_text="unused",
                )

    config = SourceConfig(
        name="bankier_rss", kind="rss", url="https://example.com/feed.xml", **SOURCE_METADATA
    )
    source = build_source(config)

    assert isinstance(source, DummySource)
    assert source.name == "bankier_rss"
    assert source.config == config


def test_load_sources_config_reads_bundled_template() -> None:
    """The bundled sources config should parse into well-formed, uniquely named sources."""

    config_path = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"
    configs = load_sources_config(config_path)

    assert configs, "bundled sources.yaml should not be empty"
    names = [config.name for config in configs]
    assert len(names) == len(set(names)), "source names must be unique"
    assert all(config.kind in {"rss", "telegram", "html", "api"} for config in configs)
    assert all(config.poll_interval_seconds > 0 for config in configs)
    assert all(isinstance(config.enabled, bool) for config in configs)
    assert len(configs) == 8
    assert all(config.description and config.description.strip() for config in configs)
    assert all(config.topics for config in configs)
    assert all(config.lang for config in configs)
    assert "bankier_rss" in names


@pytest.mark.asyncio
async def test_sync_sources_round_trips_and_updates_metadata(db_session: AsyncSession) -> None:
    """Source metadata should be inserted and updated through the sole sync path."""

    config = SourceConfig(
        name="metadata_sync",
        kind="rss",
        url="https://example.com/feed.xml",
        description="Initial description.",
        topics=["tech"],
        lang="en",
        country=None,
    )
    await registry.sync_sources_to_db([config], db_session)
    row = await db_session.scalar(select(SourceModel).where(SourceModel.name == config.name))
    assert row is not None
    assert (row.description, row.topics, row.lang, row.country) == (
        "Initial description.",
        ["tech"],
        "en",
        None,
    )

    updated = config.model_copy(
        update={"description": "Updated description.", "topics": ["tech", "economy_markets"]}
    )
    await registry.sync_sources_to_db([updated], db_session)
    await db_session.refresh(row)
    assert row.description == "Updated description."
    assert row.topics == ["tech", "economy_markets"]


def test_validate_command_reports_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Validation should use real source instances and exit non-zero on any fetch failure."""

    @register_source("rss")
    class FakeSource(Source):
        async def fetch(
            self,
            since: datetime | None = None,
            *,
            use_cache: bool = True,
        ) -> AsyncIterator[RawArticle]:
            del since
            if self.name == "broken":
                raise RuntimeError("feed unavailable")
            if use_cache:
                return
            yield RawArticle(
                source_name=self.name,
                url="https://example.com/story",
                raw_text="article",
            )

    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        """sources:
  - name: working
    kind: rss
    description: Working source.
    topics: [tech]
    lang: en
  - name: broken
    kind: rss
    description: Broken source.
    topics: [tech]
    lang: en
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("engine.cli.sources._sources_config_path", lambda: config_path)

    result = CliRunner().invoke(sources_app, ["validate"])

    assert result.exit_code == 1
    assert "OK working (1 items)" in result.output
    assert "FAIL broken: feed unavailable" in result.output
