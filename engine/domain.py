"""Pydantic DTOs that mirror persisted database entities."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from engine.llm.schemas import Citation, RelevanceVerdict, VerificationReport


class Source(BaseModel):
    """DTO for a configured content source."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: Literal["rss", "telegram", "html", "api"]
    url: str | None
    enabled: bool
    poll_interval_seconds: int
    config: dict[str, Any] | None
    created_at: datetime


class Article(BaseModel):
    """DTO for a persisted normalized article."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    url: str
    url_hash: bytes
    content_hash: bytes | None
    title: str | None
    raw_text: str | None
    lang: str | None
    published_at: datetime | None
    fetched_at: datetime


class Embedding(BaseModel):
    """DTO for a persisted article embedding."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    article_id: int
    vector: list[float]
    model: str
    created_at: datetime


class Event(BaseModel):
    """DTO for a persisted clustered event."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    article_count: int
    status: str


class EventMember(BaseModel):
    """DTO for one article assigned to one event."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: int
    article_id: int
    similarity_to_centroid: float
    added_at: datetime


class ScoredEvent(BaseModel):
    """DTO for an event annotated with a relevance verdict."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event: Event
    verdict: RelevanceVerdict


class VerifiedEvent(BaseModel):
    """DTO for an event annotated with relevance and verification output."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event: Event
    verdict: RelevanceVerdict
    report: VerificationReport


class Digest(BaseModel):
    """DTO for one persisted digest row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: int
    profile_name: str
    headline: str
    summary: str
    why_it_matters: str
    confidence_level: Literal["high", "medium", "low"]
    caveats: list[str]
    citations: list[Citation]
    stage_version: str
    created_at: datetime
    delivered_at: datetime | None


class DigestFeedback(BaseModel):
    """DTO for one append-only digest feedback row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    digest_id: int
    chat_id: int
    feedback: Literal["like", "dislike"]
    created_at: datetime


class Impression(BaseModel):
    """DTO for one outbound digest impression."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    digest_id: int
    event_id: int
    profile_name: str
    chat_id: int
    shown_at: datetime
    context: dict[str, Any] | None


class Decision(BaseModel):
    """DTO for an append-only stage decision record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: UUID
    stage_name: str
    stage_version: str
    target_type: Literal["article", "event", "digest", "source", "discussion", "research"]
    target_id: int
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    decision_json: dict[str, Any]
    created_at: datetime
