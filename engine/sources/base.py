"""Source abstractions for producing raw articles.

`RawArticle` is the output of `Source.fetch()` and is not persisted directly.
The ingest stage is responsible for normalizing it into a persisted `Article`.

Sources are pure producers of `RawArticle` and must not touch the database.
Retries, rate limiting, and other transport-specific behavior belong in
concrete source implementations, not in the base abstraction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceConfig(BaseModel):
    """Validated configuration for a single source instance."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: Literal["rss", "telegram", "html", "api"]
    url: str | None = None
    enabled: bool = True
    poll_interval_seconds: int = 1800
    config: dict[str, Any] = Field(default_factory=dict)


class RawArticle(BaseModel):
    """Raw source output prior to ingest normalization."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    external_id: str | None = None
    url: str
    title: str | None = None
    raw_html: str | None = None
    raw_text: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_body_content(self) -> RawArticle:
        """Require at least one non-empty content representation."""

        has_html = bool(self.raw_html and self.raw_html.strip())
        has_text = bool(self.raw_text and self.raw_text.strip())
        if not has_html and not has_text:
            msg = "RawArticle requires at least one non-empty value for raw_html or raw_text."
            raise ValueError(msg)
        return self


class Source(ABC):
    """Pure producer of `RawArticle` values for one configured source."""

    kind: ClassVar[Literal["rss", "telegram", "html", "api"]]

    def __init__(self, config: SourceConfig) -> None:
        if config.kind != self.kind:
            msg = f"Source kind mismatch: expected {self.kind!r}, got {config.kind!r}."
            raise ValueError(msg)
        self.config = config
        self.name = config.name

    @abstractmethod
    async def fetch(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[RawArticle]:
        """Yield raw articles published since the optional cutoff."""

        if False:
            yield RawArticle(
                source_name=self.name,
                url="https://example.invalid",
                raw_text="unreachable",
            )
