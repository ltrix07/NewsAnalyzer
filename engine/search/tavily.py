"""Async Tavily Search API client."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel

from engine._retry import retry_async
from engine.config import Settings

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class SearchResult(BaseModel):
    """One normalized Tavily search result."""

    title: str
    url: str
    content: str


def _build_async_client(*, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 429 or 500 <= status_code < 600

    return False


class TavilyClient:
    """Minimal wrapper over Tavily Search API."""

    def __init__(self, *, api_key: str, timeout: float) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(
        self,
        *,
        query: str,
        search_depth: Literal["basic", "advanced"],
        max_results: int,
    ) -> list[SearchResult]:
        """Search Tavily and return normalized result rows."""

        body: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": False,
        }

        async def _send() -> list[SearchResult]:
            async with _build_async_client(timeout=self._timeout) as client:
                response = await client.post(_TAVILY_SEARCH_URL, json=body)
                response.raise_for_status()
                payload = response.json()

            if not isinstance(payload, dict):
                msg = "Tavily returned a non-object JSON payload."
                raise RuntimeError(msg)

            raw_results = payload.get("results", [])
            if not isinstance(raw_results, list):
                msg = "Tavily returned a non-list results payload."
                raise RuntimeError(msg)

            results: list[SearchResult] = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                title = item.get("title")
                url = item.get("url")
                content = item.get("content")
                if isinstance(title, str) and isinstance(url, str) and isinstance(content, str):
                    results.append(SearchResult(title=title, url=url, content=content))
            return results

        return await retry_async(_send, retryable=_is_retryable_error)


def make_tavily_client(settings: Settings) -> TavilyClient:
    """Build a Tavily client from settings. Raises if API key missing."""

    return TavilyClient(
        api_key=settings.require_tavily_key(),
        timeout=settings.http_timeout_seconds,
    )
