"""Helpers for loading event member article context for downstream stages."""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.models import Article, EventMember, Source


class EventArticle(BaseModel):
    """Compact article context passed into event-level stages."""

    source_name: str
    title: str | None
    url: str
    excerpt: str


async def load_event_articles(session: AsyncSession, event_id: int) -> list[EventArticle]:
    """Load member articles of an event with source names and short excerpts."""

    rows = (
        await session.execute(
            select(Source.name, Article.title, Article.url, Article.raw_text)
            .select_from(EventMember)
            .join(Article, Article.id == EventMember.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(EventMember.event_id == event_id)
            .order_by(EventMember.id)
        )
    ).all()

    return [
        EventArticle(
            source_name=source_name,
            title=title,
            url=url,
            excerpt=(raw_text or "")[:600],
        )
        for source_name, title, url, raw_text in rows
    ]
