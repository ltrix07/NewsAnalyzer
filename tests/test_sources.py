"""Tests for source configuration, raw article validation, and source registry loading."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.sources import registry
from engine.sources.base import RawArticle, Source, SourceConfig
from engine.sources.registry import (
    UnknownSourceKindError,
    build_source,
    load_sources_config,
    register_source,
)


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
        SourceConfig(name="bankier_rss", kind="rss", unexpected=True)


def test_source_config_rejects_invalid_kind() -> None:
    """Kinds outside the supported source literal set should fail validation."""

    with pytest.raises(ValidationError):
        SourceConfig(name="bankier_rss", kind="atom")


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
        build_source(SourceConfig(name="future_api", kind="api"))


def test_register_source_builds_instance_from_config() -> None:
    """A registered source implementation should round-trip its config."""

    @register_source("rss")
    class DummySource(Source):
        async def fetch(
            self,
            since: datetime | None = None,
        ) -> AsyncIterator[RawArticle]:
            if False:
                yield RawArticle(
                    source_name=self.name,
                    url="https://example.com/story",
                    raw_text="unused",
                )

    config = SourceConfig(name="bankier_rss", kind="rss", url="https://example.com/feed.xml")
    source = build_source(config)

    assert isinstance(source, DummySource)
    assert source.name == "bankier_rss"
    assert source.config == config


def test_load_sources_config_reads_bundled_template() -> None:
    """The bundled sources template should parse as two disabled sources."""

    config_path = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"
    configs = load_sources_config(config_path)

    assert len(configs) == 2
    assert [config.enabled for config in configs] == [False, False]
    assert [config.kind for config in configs] == ["rss", "telegram"]
