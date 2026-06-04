"""RSS source implementation built on feedparser and httpx."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import feedparser  # type: ignore[import-untyped]
import httpx
import structlog

from engine._retry import retry_async
from engine.config import get_settings
from engine.sources._state import SourceState, load_state, save_state
from engine.sources.base import RawArticle, Source, SourceConfig
from engine.sources.registry import register_source

logger = structlog.get_logger(__name__)


def _build_async_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Build the default async HTTP client for RSS fetches."""

    return httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; newsAnalyzerBot/1.0; "
                "+https://github.com/local/newsanalyzer)"
            ),
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )


def _parse_retry_after_seconds(response: httpx.Response) -> int | None:
    """Parse a numeric Retry-After header value, ignoring HTTP-date variants."""

    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _is_retryable_error(exc: BaseException) -> bool:
    """Return whether a fetch failure should be retried."""

    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _entry_value(entry: Any, key: str) -> Any:
    """Read a feedparser entry key without depending on feedparser internals."""

    if hasattr(entry, "get"):
        return entry.get(key)
    return None


def _pick_entry_content(entry: Any) -> tuple[str | None, str | None]:
    """Return raw_html/raw_text content for a parsed feed entry."""

    content_items = _entry_value(entry, "content")
    if isinstance(content_items, list) and content_items:
        first_item = content_items[0]
        if hasattr(first_item, "get"):
            value = first_item.get("value")
            detail_type = first_item.get("type")
            if isinstance(value, str) and value.strip():
                if detail_type == "text/html":
                    return value, None
                return None, value

    summary = _entry_value(entry, "summary")
    summary_detail = _entry_value(entry, "summary_detail")
    summary_type = summary_detail.get("type") if hasattr(summary_detail, "get") else None
    if isinstance(summary, str) and summary.strip():
        if summary_type == "text/html":
            return summary, None
        return None, summary

    title = _entry_value(entry, "title")
    if isinstance(title, str) and title.strip():
        return None, title
    return None, "(no content)"


def _published_at_from_entry(entry: Any) -> datetime | None:
    """Convert feedparser published_parsed data to a UTC datetime."""

    published_parsed = _entry_value(entry, "published_parsed")
    if published_parsed is None:
        return None
    try:
        return datetime(
            published_parsed.tm_year,
            published_parsed.tm_mon,
            published_parsed.tm_mday,
            published_parsed.tm_hour,
            published_parsed.tm_min,
            published_parsed.tm_sec,
            tzinfo=UTC,
        )
    except AttributeError:
        return None


@register_source("rss")
class RSSSource(Source):
    """RSS source implementation using conditional GET and feedparser."""

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        if not config.url:
            msg = f"RSS source {config.name!r} requires a url."
            raise ValueError(msg)
        self.url = config.url

    async def _request_feed(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Fetch the feed, including special handling for 429 retry-after delays."""

        retry_after_used = False

        async def request_once() -> httpx.Response:
            nonlocal retry_after_used
            response = await client.get(self.url, headers=headers)
            if response.status_code == 429 and not retry_after_used:
                retry_after_seconds = _parse_retry_after_seconds(response)
                if retry_after_seconds is not None and retry_after_seconds <= 60:
                    retry_after_used = True
                    await asyncio.sleep(retry_after_seconds)
                    response = await client.get(self.url, headers=headers)

            if response.status_code >= 400 and response.status_code != 304:
                response.raise_for_status()
            return response

        return await retry_async(
            request_once,
            retryable=_is_retryable_error,
        )

    async def fetch(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[RawArticle]:
        """Yield raw articles from the configured RSS feed."""

        settings = get_settings()
        state = load_state(self.name, settings.raw_storage_path)
        headers: dict[str, str] = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified

        async with _build_async_client(settings.http_timeout_seconds) as client:
            response = await self._request_feed(client, headers)

        if response.status_code == 304:
            return

        save_state(
            self.name,
            SourceState(
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            ),
            settings.raw_storage_path,
        )

        feed = feedparser.parse(response.text)
        feed_language = _entry_value(getattr(feed, "feed", {}), "language")
        entries = cast(list[Any], getattr(feed, "entries", []))
        for entry in entries:
            link = _entry_value(entry, "link")
            if not isinstance(link, str) or not link:
                logger.warning("rss_entry_missing_link", source_name=self.name)
                continue

            published_at = _published_at_from_entry(entry)
            if since is not None and published_at is not None and published_at < since:
                continue

            raw_html, raw_text = _pick_entry_content(entry)
            tags = [
                term
                for tag in cast(list[Any], _entry_value(entry, "tags") or [])
                for term in [tag.get("term") if hasattr(tag, "get") else None]
                if isinstance(term, str) and term
            ]
            extra = {
                key: value
                for key, value in (
                    ("author", _entry_value(entry, "author")),
                    ("tags", tags),
                )
                if value
            }

            yield RawArticle(
                source_name=self.name,
                external_id=cast(str | None, _entry_value(entry, "id") or link),
                url=link,
                title=cast(str | None, _entry_value(entry, "title")),
                raw_html=raw_html,
                raw_text=raw_text,
                published_at=published_at,
                language=cast(str | None, _entry_value(entry, "language") or feed_language),
                extra=extra,
            )
