"""Tests for Telegram delivery formatting, dispatch, and client behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import engine._retry as retry_module
from delivery import client as delivery_client
from delivery.dispatcher import deliver_pending
from delivery.formatter import MAX_TELEGRAM_MESSAGE_LENGTH, format_digest
from engine.domain import Digest as DigestDTO
from engine.llm.schemas import Citation
from engine.models import Digest, Event


def _make_digest(
    *,
    profile_name: str = "volodymyr",
    headline: str = "Headline",
    summary: str = "Summary sentence. Another sentence.",
    why_it_matters: str = "Personal framing.",
    confidence_level: str = "high",
    caveats: list[str] | None = None,
    citations: list[Citation] | None = None,
) -> DigestDTO:
    return DigestDTO(
        id=1,
        event_id=1,
        profile_name=profile_name,
        headline=headline,
        summary=summary,
        why_it_matters=why_it_matters,
        confidence_level=confidence_level,
        caveats=caveats or ["Risk one"],
        citations=citations
        or [
            Citation(
                source="source",
                title="Title",
                url="https://example.com/article",
            )
        ],
        stage_version="v1",
        created_at=datetime.now(UTC),
        delivered_at=None,
    )


async def _create_event(session: AsyncSession) -> Event:
    event = Event(
        centroid=[0.0] * 1536,
        article_count=1,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        status="open",
    )
    session.add(event)
    await session.flush()
    return event


async def _create_digest_row(
    session: AsyncSession,
    *,
    event_id: int,
    headline: str,
    delivered_at: datetime | None = None,
) -> Digest:
    digest = Digest(
        event_id=event_id,
        profile_name="volodymyr",
        headline=headline,
        summary=f"Summary for {headline}",
        why_it_matters=f"Why {headline}",
        confidence_level="medium",
        caveats=[f"Caveat for {headline}"],
        citations=[
            {
                "source": "source",
                "title": f"Title {headline}",
                "url": f"https://example.com/{headline}",
            }
        ],
        stage_version="v1",
        created_at=datetime.now(UTC),
        delivered_at=delivered_at,
    )
    session.add(digest)
    await session.flush()
    return digest


def test_format_digest_escapes_html_special_chars() -> None:
    digest = _make_digest(
        headline="Headline <unsafe> & value",
        summary="Summary with <b>tag</b> & ampersand.",
        why_it_matters="Why <this> matters & more.",
        caveats=["Caveat <1> & alert"],
        citations=[
            Citation(
                source="src & co",
                title="Title <unsafe>",
                url="https://example.com/?a=1&b=2",
            )
        ],
    )

    message = format_digest(digest)

    assert "&lt;unsafe&gt;" in message
    assert "&amp; value" in message
    assert "&lt;b&gt;tag&lt;/b&gt;" in message
    assert 'href="https://example.com/?a=1&amp;b=2"' in message
    assert "src &amp; co: Title &lt;unsafe&gt;" in message


def test_format_digest_localizes_labels_by_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    (profile_root / "english.yaml").write_text(
        "\n".join(
            [
                "profile:",
                "  name: english",
                '  location: "US"',
                "  citizenship: US",
                "  languages: [en]",
                "  output_language: en",
                "  interests: [Macro]",
                "  not_interested: [Sports]",
                "  keyword_rules:",
                "    keep_if_matches: []",
                "    drop_if_matches: []",
            ]
        ),
        encoding="utf-8",
    )

    settings = delivery_client.get_settings()
    russian_message = format_digest(_make_digest(profile_name="volodymyr"))
    monkeypatch.setattr(settings, "profile_root", profile_root)
    english_message = format_digest(_make_digest(profile_name="english"))

    assert "Почему это важно:" in russian_message
    assert "Источники:" in russian_message
    assert "Why it matters:" in english_message
    assert "Sources:" in english_message


def test_format_digest_truncates_summary_before_why_and_keeps_citations() -> None:
    digest = _make_digest(
        summary=" ".join(["Sentence."] * 1200),
        why_it_matters="Why section stays present.",
        citations=[
            Citation(source="one", title="First", url="https://example.com/1"),
            Citation(source="two", title="Second", url="https://example.com/2"),
        ],
    )

    message = format_digest(digest)

    assert len(message) <= MAX_TELEGRAM_MESSAGE_LENGTH
    assert "Why section stays present." in message
    assert "https://example.com/1" in message
    assert "https://example.com/2" in message
    assert "Sentence. Sentence." in message


@pytest.mark.asyncio
async def test_dispatcher_sends_only_undelivered_digests(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    first = await _create_digest_row(db_session, event_id=event.id, headline="First pending")
    second = await _create_digest_row(db_session, event_id=event.id, headline="Second pending")
    delivered = await _create_digest_row(
        db_session,
        event_id=event.id,
        headline="Already delivered",
        delivered_at=datetime.now(UTC),
    )

    recorded_calls: list[tuple[int, str]] = []

    class FakeTelegramClient:
        async def send_message(self, chat_id: int, text: str, **_: Any) -> dict[str, Any]:
            recorded_calls.append((chat_id, text))
            return {"ok": True}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)
    monkeypatch.setattr("delivery.dispatcher._build_client", lambda: FakeTelegramClient())

    report = await deliver_pending()

    assert report.sent == 2
    assert report.failed == 0
    assert report.skipped == 0
    assert len(recorded_calls) == 2
    assert all(chat_id == 123456 for chat_id, _ in recorded_calls)
    assert first.delivered_at is not None
    assert second.delivered_at is not None
    assert delivered.delivered_at is not None


@pytest.mark.asyncio
async def test_dispatcher_continues_after_mid_batch_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    first = await _create_digest_row(db_session, event_id=event.id, headline="First")
    second = await _create_digest_row(db_session, event_id=event.id, headline="Second")
    third = await _create_digest_row(db_session, event_id=event.id, headline="Third")

    call_count = 0
    sent_messages: list[str] = []

    class FakeTelegramClient:
        async def send_message(self, chat_id: int, text: str, **_: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("synthetic failure")
            sent_messages.append(text)
            return {"ok": True}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)
    monkeypatch.setattr("delivery.dispatcher._build_client", lambda: FakeTelegramClient())

    report = await deliver_pending()

    assert report.sent == 2
    assert report.failed == 1
    assert first.delivered_at is not None
    assert second.delivered_at is None
    assert third.delivered_at is not None
    assert len(sent_messages) == 2


@pytest.mark.asyncio
async def test_telegram_bot_client_returns_payload_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        delivery_client,
        "_build_async_client",
        lambda *, timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    monkeypatch.setattr(delivery_client.get_settings(), "http_timeout_seconds", 0.01)

    client = delivery_client.TelegramBotClient("token")
    payload = await client.send_message(1, "hello")

    assert payload["ok"] is True
    assert payload["result"]["message_id"] == 1


@pytest.mark.asyncio
async def test_telegram_bot_client_raises_runtime_error_on_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Chat not found"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        delivery_client,
        "_build_async_client",
        lambda *, timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )

    client = delivery_client.TelegramBotClient("token")
    with pytest.raises(RuntimeError, match="Chat not found"):
        await client.send_message(1, "hello")


@pytest.mark.asyncio
async def test_telegram_bot_client_retries_429_and_raises_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "1"}, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        delivery_client,
        "_build_async_client",
        lambda *, timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    monkeypatch.setattr(retry_module.asyncio, "sleep", lambda _: _completed_future())

    client = delivery_client.TelegramBotClient("token")
    with pytest.raises(httpx.HTTPStatusError):
        await client.send_message(1, "hello")

    assert attempts == 3


@pytest.mark.asyncio
async def test_telegram_bot_client_does_not_retry_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        delivery_client,
        "_build_async_client",
        lambda *, timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )

    client = delivery_client.TelegramBotClient("token")
    with pytest.raises(httpx.HTTPStatusError):
        await client.send_message(1, "hello")

    assert attempts == 1


def _completed_future() -> Any:
    class _Awaitable:
        def __await__(self) -> Any:
            if False:
                yield None
            return None

    return _Awaitable()
