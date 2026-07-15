"""Tests for citation link minting and the redirect ASGI service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.dispatcher import _mint_digest_links, deliver_pending
from delivery.formatter import format_digest
from engine.config import Settings
from engine.domain import Digest
from engine.llm.schemas import Citation
from engine.models import Digest as DigestModel
from engine.models import DigestLink, Event, LinkClick, User
from engine.profile import load_profile
from web import app as web_app

app = web_app.app


@pytest_asyncio.fixture(autouse=True)
async def _seed_profile_user(db_session: AsyncSession) -> None:
    profile = load_profile("volodymyr", Path("config/profiles"))
    db_session.add(User(username="volodymyr", profile=profile.model_dump(mode="json")))
    await db_session.flush()


def _digest() -> Digest:
    return Digest(
        id=1,
        event_id=1,
        profile_name="volodymyr",
        headline="Headline",
        summary="Summary",
        why_it_matters="Why",
        confidence_level="high",
        caveats=[],
        citations=[
            Citation(source="one", title="One", url="https://example.com/one"),
            Citation(source="two", title="Two", url="https://example.com/two"),
        ],
        stage_version="v1",
        created_at=datetime.now(UTC),
        delivered_at=None,
    )


@pytest.mark.asyncio
async def test_format_digest_uses_tracked_urls_with_raw_fallback(
    db_session: AsyncSession,
) -> None:
    message = await format_digest(_digest(), db_session, {0: "https://links.example/r/token"})

    assert 'href="https://links.example/r/token"' in message
    assert 'href="https://example.com/two"' in message


@pytest.mark.asyncio
async def test_healthz_does_not_require_database() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_tracking_configuration_requires_public_origin() -> None:
    settings = Settings(link_tracking_enabled=True, redirect_base_url=None)

    with pytest.raises(RuntimeError, match="REDIRECT_BASE_URL"):
        settings.require_redirect_base_url()


async def _persist_digest(session: AsyncSession) -> DigestModel:
    event = Event(
        centroid=[0.0] * 1536,
        article_count=1,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        status="open",
    )
    session.add(event)
    await session.flush()
    digest = DigestModel(
        event_id=event.id,
        profile_name="volodymyr",
        headline="Headline",
        summary="Summary",
        why_it_matters="Why",
        confidence_level="high",
        caveats=[],
        citations=[
            {"source": "one", "title": "One", "url": "https://example.com/one"},
            {"source": "two", "title": "Two", "url": "https://example.com/two"},
        ],
        stage_version="v1",
    )
    session.add(digest)
    await session.flush()
    return digest


@pytest.mark.asyncio
async def test_minting_reuses_tokens_for_same_digest_and_chat(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_model = await _persist_digest(db_session)
    digest = Digest.model_validate(digest_model)
    monkeypatch.setattr("delivery.dispatcher.session_scope", lambda: _session_scope(db_session))

    first = await _mint_digest_links(digest, 123)
    second = await _mint_digest_links(digest, 123)

    assert second == first
    assert len(first) == 2
    assert await db_session.scalar(select(func.count()).select_from(DigestLink)) == 2


@asynccontextmanager
async def _session_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise


@pytest.mark.asyncio
async def test_redirect_records_click_and_sets_no_store(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = await _persist_digest(db_session)
    link = DigestLink(
        token="known-token",
        digest_id=digest.id,
        chat_id=123,
        citation_index=0,
        url="https://example.com/target",
        source="source",
    )
    db_session.add(link)
    await db_session.flush()
    monkeypatch.setattr(web_app, "session_scope", lambda: _session_scope(db_session))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.get("/r/known-token", headers={"User-Agent": "Browser/1"})

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/target"
    assert response.headers["cache-control"] == "no-store"
    click = await db_session.scalar(select(LinkClick))
    assert click is not None
    assert click.user_agent == "Browser/1"


@pytest.mark.asyncio
async def test_unknown_and_crawler_requests_do_not_record_clicks(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = await _persist_digest(db_session)
    db_session.add(
        DigestLink(
            token="crawler-token",
            digest_id=digest.id,
            chat_id=123,
            citation_index=0,
            url="https://example.com/target",
            source=None,
        )
    )
    await db_session.flush()
    monkeypatch.setattr(web_app, "session_scope", lambda: _session_scope(db_session))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        unknown = await client.get("/r/unknown")
        crawler = await client.get("/r/crawler-token", headers={"User-Agent": "TelegramBot"})

    assert unknown.status_code == 404
    assert unknown.text == "Link not found"
    assert crawler.status_code == 302
    assert await db_session.scalar(select(func.count()).select_from(LinkClick)) == 0


@pytest.mark.asyncio
async def test_click_logging_failure_still_redirects(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = await _persist_digest(db_session)
    db_session.add(
        DigestLink(
            token="failure-token",
            digest_id=digest.id,
            chat_id=123,
            citation_index=0,
            url="https://example.com/target",
            source=None,
        )
    )
    await db_session.flush()
    scope_calls = 0

    @asynccontextmanager
    async def fail_second_scope() -> AsyncIterator[AsyncSession]:
        nonlocal scope_calls
        scope_calls += 1
        try:
            yield db_session
            if scope_calls == 2:
                raise RuntimeError("commit failed")
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    monkeypatch.setattr(web_app, "session_scope", fail_second_scope)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.get("/r/failure-token")

    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert await db_session.scalar(select(func.count()).select_from(LinkClick)) == 0


@pytest.mark.asyncio
async def test_non_http_target_returns_not_found_without_click(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = await _persist_digest(db_session)
    db_session.add(
        DigestLink(
            token="unsafe-token",
            digest_id=digest.id,
            chat_id=123,
            citation_index=0,
            url="javascript:alert(1)",
            source=None,
        )
    )
    await db_session.flush()
    monkeypatch.setattr(web_app, "session_scope", lambda: _session_scope(db_session))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.get("/r/unsafe-token")

    assert response.status_code == 404
    assert response.text == "Link not found"
    assert await db_session.scalar(select(func.count()).select_from(LinkClick)) == 0


class _TelegramClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> dict[str, object]:
        self.messages.append(text)
        return {"ok": True}


def _configure_delivery(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    *,
    tracking: bool,
) -> None:
    settings = Settings(
        telegram_bot_token="token",
        telegram_chat_id=123,
        thread_updates_enabled=False,
        taste_ranking_enabled=False,
        link_tracking_enabled=tracking,
        redirect_base_url="https://links.example" if tracking else None,
    )
    monkeypatch.setattr("delivery.dispatcher.get_settings", lambda: settings)
    monkeypatch.setattr("delivery.dispatcher.session_scope", lambda: _session_scope(db_session))


@pytest.mark.asyncio
@pytest.mark.parametrize("tracking", [False, True])
async def test_dispatcher_switches_between_raw_and_tracked_links(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tracking: bool,
) -> None:
    await _persist_digest(db_session)
    _configure_delivery(monkeypatch, db_session, tracking=tracking)
    client = _TelegramClient()

    report = await deliver_pending(client=client)

    assert report.sent == 1
    link_count = await db_session.scalar(select(func.count()).select_from(DigestLink))
    assert link_count == (2 if tracking else 0)
    if tracking:
        assert "https://links.example/r/" in client.messages[0]
    else:
        assert "https://example.com/one" in client.messages[0]


@pytest.mark.asyncio
async def test_dispatcher_falls_back_to_raw_urls_when_minting_fails(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _persist_digest(db_session)
    _configure_delivery(monkeypatch, db_session, tracking=True)
    client = _TelegramClient()

    async def fail_minting(*args: object, **kwargs: object) -> dict[int, str]:
        raise RuntimeError("minting unavailable")

    monkeypatch.setattr("delivery.dispatcher._mint_digest_links", fail_minting)
    report = await deliver_pending(client=client)

    assert report.sent == 1
    assert "https://example.com/one" in client.messages[0]
    assert "https://example.com/two" in client.messages[0]
    assert "https://links.example/r/" not in client.messages[0]


@pytest.mark.asyncio
async def test_dispatcher_commits_tokens_before_sending(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _persist_digest(db_session)
    _configure_delivery(monkeypatch, db_session, tracking=True)
    commit_count = 0
    original_commit = db_session.commit

    async def record_commit() -> None:
        nonlocal commit_count
        await original_commit()
        commit_count += 1

    monkeypatch.setattr(db_session, "commit", record_commit)

    class AssertDurableClient(_TelegramClient):
        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> dict[str, object]:
            assert commit_count >= 1
            assert await db_session.scalar(select(func.count()).select_from(DigestLink)) == 2
            return await super().send_message(chat_id, text, **kwargs)

    report = await deliver_pending(client=AssertDurableClient())

    assert report.sent == 1
    assert commit_count >= 2
