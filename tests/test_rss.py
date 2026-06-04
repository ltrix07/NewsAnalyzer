"""Tests for the RSS source implementation and raw fetch state handling."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from engine.config import get_settings
from engine.sources._state import SourceState, load_state, save_state
from engine.sources.base import RawArticle, SourceConfig
from engine.sources.rss import RSSSource

RSS_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example Feed</title>
    <language>pl</language>
    <item>
      <guid>entry-1</guid>
      <title>First story</title>
      <link>https://example.com/1</link>
      <pubDate>Tue, 03 Jun 2026 10:00:00 GMT</pubDate>
      <content:encoded><![CDATA[<p>Alpha</p>]]></content:encoded>
      <author>Author One</author>
      <category>economy</category>
    </item>
    <item>
      <guid>entry-2</guid>
      <title>Second story</title>
      <link>https://example.com/2</link>
      <pubDate>Tue, 03 Jun 2026 11:00:00 GMT</pubDate>
      <content:encoded><![CDATA[<p>Beta</p>]]></content:encoded>
    </item>
    <item>
      <guid>entry-3</guid>
      <title>Third story</title>
      <link>https://example.com/3</link>
      <pubDate>Tue, 03 Jun 2026 12:00:00 GMT</pubDate>
      <content:encoded><![CDATA[<p>Gamma</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

TITLE_ONLY_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Title Fallback Feed</title>
    <item>
      <guid>fallback-1</guid>
      <link>https://example.com/fallback</link>
    </item>
  </channel>
</rss>
"""


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point raw storage into a temporary directory and clear cached settings."""

    monkeypatch.setenv("RAW_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "1.0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fast_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable retry sleeps to keep tests fast and deterministic."""

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("engine._retry.asyncio.sleep", no_sleep)
    monkeypatch.setattr("engine.sources.rss.asyncio.sleep", no_sleep)


def _source_config() -> SourceConfig:
    """Return a standard RSS source config for tests."""

    return SourceConfig(name="bankier_rss", kind="rss", url="https://example.com/feed.xml")


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Patch RSS HTTP client construction to use a mock transport."""

    def build_client(timeout_seconds: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout_seconds)

    monkeypatch.setattr("engine.sources.rss._build_async_client", build_client)


@pytest.mark.asyncio
async def test_rss_fetch_yields_three_raw_articles_with_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed feed should produce one RawArticle per entry."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS_FEED)

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    articles = [article async for article in source.fetch()]

    assert len(articles) == 3
    assert all(isinstance(article, RawArticle) for article in articles)
    assert articles[0].source_name == "bankier_rss"
    assert articles[0].url == "https://example.com/1"
    assert articles[0].title == "First story"
    assert articles[0].published_at == datetime(2026, 6, 3, 10, 0, 0, tzinfo=UTC)
    assert articles[0].raw_html == "<p>Alpha</p>"
    assert articles[0].raw_text is None


@pytest.mark.asyncio
async def test_rss_fetch_returns_empty_on_304_without_overwriting_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 304 response should yield nothing and preserve the previous state file."""

    save_state("bankier_rss", SourceState(etag="old-etag", last_modified="old-modified"), tmp_path)
    before = (tmp_path / ".state" / "bankier_rss.json").read_text(encoding="utf-8")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(304, text="")

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    articles = [article async for article in source.fetch()]
    after = (tmp_path / ".state" / "bankier_rss.json").read_text(encoding="utf-8")

    assert articles == []
    assert before == after


@pytest.mark.asyncio
async def test_rss_fetch_saves_and_reuses_conditional_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """State should persist ETag/Last-Modified and be reused on the next fetch."""

    requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(
                200,
                headers={"ETag": '"etag-1"', "Last-Modified": "Tue, 03 Jun 2026 12:30:00 GMT"},
                text=RSS_FEED,
            ),
            httpx.Response(304, text=""),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    _ = [article async for article in source.fetch()]
    state = load_state("bankier_rss", tmp_path)
    _ = [article async for article in source.fetch()]

    assert state.etag == '"etag-1"'
    assert state.last_modified == "Tue, 03 Jun 2026 12:30:00 GMT"
    assert requests[1].headers["If-None-Match"] == '"etag-1"'
    assert requests[1].headers["If-Modified-Since"] == "Tue, 03 Jun 2026 12:30:00 GMT"


@pytest.mark.asyncio
async def test_rss_fetch_filters_entries_older_than_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries older than the cutoff should be excluded."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS_FEED)

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    since = datetime(2026, 6, 3, 10, 30, 0, tzinfo=UTC)
    articles = [article async for article in source.fetch(since=since)]

    assert [article.url for article in articles] == [
        "https://example.com/2",
        "https://example.com/3",
    ]


@pytest.mark.asyncio
async def test_rss_fetch_retries_transport_errors_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retryable transport failures should be retried until a success response arrives."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(200, text=RSS_FEED)

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    articles = [article async for article in source.fetch()]

    assert len(articles) == 3
    assert call_count == 3


@pytest.mark.asyncio
async def test_rss_fetch_does_not_retry_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client errors other than 429 should fail immediately."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, request=request, text="not found")

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    with pytest.raises(httpx.HTTPStatusError):
        _ = [article async for article in source.fetch()]

    assert call_count == 1


@pytest.mark.asyncio
async def test_rss_fetch_falls_back_when_entry_has_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries without summary/content/title should still yield a fallback raw_text."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=TITLE_ONLY_FEED)

    _patch_transport(monkeypatch, handler)
    source = RSSSource(_source_config())

    articles = [article async for article in source.fetch()]

    assert len(articles) == 1
    assert articles[0].raw_text == "(no content)"
    assert articles[0].raw_html is None
