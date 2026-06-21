"""ORM models for persisted entities.

The ``decisions`` table is append-only and intentionally has no foreign keys.
Decision rows must outlive the rows they reference so historical traces remain
valid across later schema changes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from engine.db import Base


class Source(Base):
    """Configured source from which raw content is fetched."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("name", name="uq_sources_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=text("true"))
    poll_interval_seconds: Mapped[int] = mapped_column(
        nullable=False,
        default=1800,
        server_default=text("1800"),
    )
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    """Persisted normalized article content from a single source."""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_id", "url_hash", name="uq_articles_source_id_url_hash"),
        Index("ix_articles_content_hash", "content_hash"),
        Index("ix_articles_source_id_fetched_at_desc", "source_id", text("fetched_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    url_hash: Mapped[bytes] = mapped_column(LargeBinary(8), nullable=False)
    content_hash: Mapped[bytes | None] = mapped_column(LargeBinary(8), nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    source: Mapped[Source] = relationship(back_populates="articles")
    embedding: Mapped[Embedding | None] = relationship(back_populates="article", uselist=False)
    event_member: Mapped[EventMember | None] = relationship(
        back_populates="article",
        uselist=False,
    )


class Embedding(Base):
    """Stored embedding vector for one article."""

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("articles.id"),
        nullable=False,
        unique=True,
    )
    vector: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    article: Mapped[Article] = relationship(back_populates="embedding")


class Event(Base):
    """Cluster of one or more related articles."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_last_seen_at", "last_seen_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    centroid: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    article_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="open",
        server_default=text("'open'"),
    )

    members: Mapped[list[EventMember]] = relationship(back_populates="event")
    digests: Mapped[list[Digest]] = relationship(back_populates="event")


class EventMember(Base):
    """Assignment of one article to one clustered event."""

    __tablename__ = "event_members"
    __table_args__ = (Index("ix_event_members_event_id", "event_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id"), nullable=False)
    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("articles.id"),
        nullable=False,
        unique=True,
    )
    similarity_to_centroid: Mapped[float] = mapped_column(Float, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    event: Mapped[Event] = relationship(back_populates="members")
    article: Mapped[Article] = relationship(back_populates="event_member")


class Digest(Base):
    """Persisted user-facing digest row for one event/profile/stage version."""

    __tablename__ = "digests"
    __table_args__ = (
        Index("ix_digests_event_id_created_at", "event_id", "created_at"),
        Index("ix_digests_created_at_desc", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id"), nullable=False)
    profile_name: Mapped[str] = mapped_column(String, nullable=False)
    headline: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_level: Mapped[str] = mapped_column(String, nullable=False)
    caveats: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    stage_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    event: Mapped[Event] = relationship(back_populates="digests")


class DigestFeedback(Base):
    """Append-only explicit feedback on one delivered digest."""

    __tablename__ = "digest_feedback"
    __table_args__ = (
        CheckConstraint("feedback in ('like', 'dislike')", name="ck_digest_feedback_feedback"),
        Index(
            "ix_digest_feedback_digest_chat_created",
            "digest_id",
            "chat_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("digests.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    feedback: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class Impression(Base):
    """Append-only record that a digest was shown to the configured chat."""

    __tablename__ = "impressions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("digests.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id"), nullable=False)
    profile_name: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shown_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class DiscussionPending(Base):
    """Current chat-to-digest discussion target awaiting the user's question."""

    __tablename__ = "discussion_pending"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class ResearchPending(Base):
    """Current chat-to-digest research request awaiting button confirmation."""

    __tablename__ = "research_pending"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class TelegramCursor(Base):
    """Single-row cursor for Telegram long-poll update acknowledgement."""

    __tablename__ = "telegram_cursor"
    __table_args__ = (CheckConstraint("id = 1", name="ck_telegram_cursor_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Decision(Base):
    """Append-only decision row recorded for every pipeline stage input."""

    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_run_id_stage_name", "run_id", "stage_name"),
        Index("ix_decisions_target_type_target_id", "target_type", "target_id"),
        Index("ix_decisions_created_at_desc", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    stage_name: Mapped[str] = mapped_column(String, nullable=False)
    stage_version: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
