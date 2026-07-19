"""Tests for Telegram delivery formatting, dispatch, and client behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import engine._retry as retry_module
from delivery import client as delivery_client
from delivery.discussion import DiscussionAnswer, render_discussion_prompt
from delivery.dispatcher import deliver_due, deliver_pending, reveal_batch_page
from delivery.formatter import MAX_TELEGRAM_MESSAGE_LENGTH, format_digest
from delivery.keyboards import (
    build_digest_keyboard,
    build_discussion_callback,
    build_dislike_reason_callback,
    build_dislike_reason_keyboard,
    build_feedback_callback,
    build_research_callback,
    build_research_keyboard,
    build_reveal_callback,
    build_reveal_more_callback,
    parse_callback_data,
)
from delivery.listener import handlers as listener_handlers
from delivery.listener.handlers import handle_update, latest_feedback
from delivery.listener.service import (
    get_cursor,
    get_updates_with_backoff,
    initialize_cursor_if_missing,
    process_update,
    process_update_safely,
)
from delivery.research import research_digest_question
from delivery.strings import t
from engine.consolidation_match import PairJudgement
from engine.domain import Digest as DigestDTO
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import Citation, DiscussionReply, ResearchReply
from engine.models import (
    Decision,
    DeliveryBatch,
    Digest,
    DigestFeedback,
    DiscussionPending,
    Event,
    Impression,
    ResearchPending,
    UIEvent,
    User,
)
from engine.profile import load_profile
from engine.ranking.taste import build_taste_vector
from engine.search import tavily as tavily_module
from engine.search.tavily import SearchResult, TavilyClient
from engine.stages._event_context import EventArticle


@pytest.mark.asyncio
async def test_multiuser_delivery_isolated_by_profile_chat_language_and_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = await db_session.scalar(select(User).where(User.username == "volodymyr"))
    assert primary is not None
    primary.ui_language = "ru"
    db_session.add(
        User(
            username="english",
            chat_id=222222,
            profile=primary.profile,
            ui_language="en",
        )
    )
    event_a = await _create_event(db_session)
    event_b = await _create_event(db_session)
    digest_a = await _create_digest_row(
        db_session,
        event_id=event_a.id,
        headline="Russian digest",
        profile_name="volodymyr",
    )
    digest_b = await _create_digest_row(
        db_session,
        event_id=event_b.id,
        headline="English digest",
        profile_name="english",
    )
    calls: list[tuple[int, str, dict[str, Any] | None]] = []

    class FakeClient:
        async def send_message(
            self,
            chat_id: int,
            text: str,
            *,
            reply_markup: dict[str, Any] | None = None,
            **_: Any,
        ) -> dict[str, Any]:
            calls.append((chat_id, text, reply_markup))
            return {"ok": True}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr(settings, "batched_delivery_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_pending(client=FakeClient())  # type: ignore[arg-type]

    assert report.sent == 2
    routed_headlines = [
        (chat_id, "Russian digest" in text, "English digest" in text) for chat_id, text, _ in calls
    ]
    assert routed_headlines == [
        (222222, False, True),
        (123456, True, False),
    ]
    assert calls[0][2] == build_digest_keyboard(digest_b.id, lang="en")
    assert calls[1][2] == build_digest_keyboard(digest_a.id, lang="ru")


@pytest.mark.asyncio
async def test_multiuser_delivery_failure_and_missing_chat_do_not_block_other_users(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = await db_session.scalar(select(User).where(User.username == "volodymyr"))
    assert primary is not None
    db_session.add_all(
        [
            User(username="english", chat_id=222222, profile=primary.profile, ui_language="en"),
            User(username="waiting", chat_id=None, profile=primary.profile),
        ]
    )
    for username in ("volodymyr", "english", "waiting"):
        event = await _create_event(db_session)
        await _create_digest_row(
            db_session,
            event_id=event.id,
            headline=username,
            profile_name=username,
        )
    calls: list[int] = []

    class FailingClient:
        async def send_message(self, chat_id: int, *_: Any, **__: Any) -> dict[str, Any]:
            calls.append(chat_id)
            if chat_id == 123456:
                raise RuntimeError("chat unavailable")
            return {"ok": True}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr(settings, "batched_delivery_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_pending(client=FailingClient())  # type: ignore[arg-type]

    assert calls == [222222, 123456]
    assert report.sent == 1
    assert report.failed == 1


@pytest.mark.asyncio
async def test_listener_resolves_enabled_user_language_and_ignores_unknown_or_disabled(
    db_session: AsyncSession,
) -> None:
    primary = await db_session.scalar(select(User).where(User.username == "volodymyr"))
    assert primary is not None
    primary.ui_language = "en"
    disabled = User(
        username="disabled",
        chat_id=333333,
        profile=primary.profile,
        ui_language="ru",
        enabled=False,
    )
    db_session.add(disabled)
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Known")
    answers: list[str] = []

    class FakeClient:
        async def answer_callback_query(self, _callback_id: str, text: str) -> dict[str, Any]:
            answers.append(text)
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    settings = delivery_client.get_settings()
    known = {
        "callback_query": {
            "id": "known",
            "data": build_feedback_callback("like", digest.id),
            "message": {"message_id": 1, "chat": {"id": 123456}},
        }
    }
    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=known,
    )
    for chat_id in (999999, 333333):
        ignored = await handle_update(
            session=db_session,
            settings=settings,
            telegram_client=FakeClient(),  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update={"message": {"chat": {"id": chat_id}, "text": "ignored"}},
        )
        assert ignored == listener_handlers.HandlerResult()

    assert answers == ["Saved ✓"]


@pytest.mark.asyncio
async def test_taste_vector_isolated_by_chat_id(db_session: AsyncSession) -> None:
    liked_event = await _create_event(db_session, centroid=_axis_centroid(0))
    disliked_event = await _create_event(db_session, centroid=_axis_centroid(1))
    liked = await _create_digest_row(db_session, event_id=liked_event.id, headline="Liked")
    disliked = await _create_digest_row(
        db_session,
        event_id=disliked_event.id,
        headline="Disliked",
    )
    db_session.add_all(
        [
            DigestFeedback(digest_id=liked.id, chat_id=123456, feedback="like"),
            DigestFeedback(digest_id=disliked.id, chat_id=123456, feedback="dislike"),
        ]
    )
    await db_session.flush()

    user_a = await build_taste_vector(db_session, chat_id=123456, min_labels_per_class=1)
    user_b = await build_taste_vector(db_session, chat_id=222222, min_labels_per_class=1)

    assert user_a is not None
    assert user_a.n_like == 1
    assert user_a.n_dislike == 1
    assert user_b is None


@pytest_asyncio.fixture(autouse=True)
async def _use_listener_test_session(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep best-effort analytics writes visible in the listener test transaction."""

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(listener_handlers, "session_scope", fake_session_scope)
    profile = load_profile("volodymyr", Path("config/profiles"))
    db_session.add(
        User(
            username="volodymyr",
            chat_id=123456,
            profile=profile.model_dump(mode="json"),
        )
    )
    await db_session.flush()


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


async def _create_event(
    session: AsyncSession,
    *,
    centroid: list[float] | None = None,
    article_count: int = 1,
) -> Event:
    event = Event(
        centroid=centroid or [0.0] * 1536,
        article_count=article_count,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        status="open",
    )
    session.add(event)
    await session.flush()
    return event


def _axis_centroid(index: int, value: float = 1.0) -> list[float]:
    vector = [0.0] * 1536
    vector[index] = value
    return vector


async def _create_digest_row(
    session: AsyncSession,
    *,
    event_id: int,
    headline: str,
    profile_name: str = "volodymyr",
    delivered_at: datetime | None = None,
    confidence_level: str = "medium",
    created_at: datetime | None = None,
    telegram_message_id: int | None = None,
) -> Digest:
    digest = Digest(
        event_id=event_id,
        profile_name=profile_name,
        headline=headline,
        summary=f"Summary for {headline}",
        why_it_matters=f"Why {headline}",
        confidence_level=confidence_level,
        caveats=[f"Caveat for {headline}"],
        citations=[
            {
                "source": "source",
                "title": f"Title {headline}",
                "url": f"https://example.com/{headline}",
            }
        ],
        stage_version="v1",
        created_at=created_at or datetime.now(UTC),
        delivered_at=delivered_at,
        telegram_message_id=telegram_message_id,
    )
    session.add(digest)
    await session.flush()
    return digest


@pytest.mark.asyncio
async def test_format_digest_escapes_html_special_chars(db_session: AsyncSession) -> None:
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

    message = await format_digest(digest, db_session)

    assert "&lt;unsafe&gt;" in message
    assert "&amp; value" in message
    assert "&lt;b&gt;tag&lt;/b&gt;" in message
    assert 'href="https://example.com/?a=1&amp;b=2"' in message
    assert "src &amp; co: Title &lt;unsafe&gt;" in message


@pytest.mark.asyncio
async def test_format_digest_localizes_labels_by_profile(db_session: AsyncSession) -> None:
    russian = load_profile("volodymyr", Path("config/profiles"))
    english = russian.model_copy(update={"name": "english", "output_language": "en"})
    db_session.add_all(
        [
            User(username="english", profile=english.model_dump(mode="json")),
        ]
    )
    await db_session.flush()
    russian_message = await format_digest(_make_digest(profile_name="volodymyr"), db_session)
    english_message = await format_digest(_make_digest(profile_name="english"), db_session)

    assert "Почему это важно:" in russian_message
    assert "Источники:" in russian_message
    assert "Why it matters:" in english_message
    assert "Sources:" in english_message


@pytest.mark.asyncio
async def test_format_digest_truncates_summary_before_why_and_keeps_citations(
    db_session: AsyncSession,
) -> None:
    digest = _make_digest(
        summary=" ".join(["Sentence."] * 1200),
        why_it_matters="Why section stays present.",
        citations=[
            Citation(source="one", title="First", url="https://example.com/1"),
            Citation(source="two", title="Second", url="https://example.com/2"),
        ],
    )

    message = await format_digest(digest, db_session)

    assert len(message) <= MAX_TELEGRAM_MESSAGE_LENGTH
    assert "Why section stays present." in message
    assert "https://example.com/1" in message
    assert "https://example.com/2" in message
    assert "Sentence. Sentence." in message


def test_keyboard_callback_data_round_trip_and_size() -> None:
    digest_id = 123456789

    like = build_feedback_callback("like", digest_id)
    dislike = build_feedback_callback("dislike", digest_id)
    off_topic = build_dislike_reason_callback("off_topic", digest_id)
    weak_analysis = build_dislike_reason_callback("weak_analysis", digest_id)
    discussion = build_discussion_callback(digest_id)
    research = build_research_callback(digest_id)
    like_payload = parse_callback_data(like)
    dislike_payload = parse_callback_data(dislike)
    off_topic_payload = parse_callback_data(off_topic)
    weak_analysis_payload = parse_callback_data(weak_analysis)
    discussion_payload = parse_callback_data(discussion)
    research_payload = parse_callback_data(research)

    assert like_payload is not None
    assert dislike_payload is not None
    assert off_topic_payload is not None
    assert weak_analysis_payload is not None
    assert discussion_payload is not None
    assert research_payload is not None
    assert like_payload.action == "like"
    assert like_payload.digest_id == digest_id
    assert dislike_payload.action == "dislike"
    assert off_topic_payload.action == "dislike_reason"
    assert off_topic_payload.digest_id == digest_id
    assert off_topic_payload.reason == "off_topic"
    assert weak_analysis_payload.action == "dislike_reason"
    assert weak_analysis_payload.reason == "weak_analysis"
    assert discussion_payload.action == "discussion"
    assert research_payload.action == "research"
    assert research_payload.digest_id == digest_id
    assert (
        max(
            len(value.encode("utf-8"))
            for value in (like, dislike, off_topic, weak_analysis, discussion, research)
        )
        <= 64
    )
    assert build_digest_keyboard(digest_id)["inline_keyboard"]
    assert build_dislike_reason_keyboard(digest_id)["inline_keyboard"] == [
        [
            {"text": "📌 Не моя тема", "callback_data": off_topic},
            {"text": "🛠 Слабый разбор", "callback_data": weak_analysis},
        ]
    ]
    assert build_research_keyboard(digest_id)["inline_keyboard"]


def test_keyboard_labels_localize_without_changing_callback_data() -> None:
    digest_id = 123

    default_keyboard = build_digest_keyboard(digest_id)
    russian_keyboard = build_digest_keyboard(digest_id, lang="ru")
    english_keyboard = build_digest_keyboard(digest_id, lang="en")
    selected_english_keyboard = build_digest_keyboard(
        digest_id,
        selected_feedback="like",
        lang="en",
    )

    assert default_keyboard == russian_keyboard
    assert [button["text"] for row in english_keyboard["inline_keyboard"] for button in row] == [
        "👍 Interesting",
        "👎 Not interesting",
        "💬 Discuss",
    ]
    assert selected_english_keyboard["inline_keyboard"][0][0]["text"] == "✅ 👍 Interesting"
    assert [
        button["callback_data"] for row in english_keyboard["inline_keyboard"] for button in row
    ] == [button["callback_data"] for row in russian_keyboard["inline_keyboard"] for button in row]


def test_dislike_reason_and_research_keyboards_localize() -> None:
    digest_id = 123

    assert build_dislike_reason_keyboard(digest_id, lang="en")["inline_keyboard"] == [
        [
            {
                "text": "📌 Not my topic",
                "callback_data": build_dislike_reason_callback("off_topic", digest_id),
            },
            {
                "text": "🛠 Weak analysis",
                "callback_data": build_dislike_reason_callback("weak_analysis", digest_id),
            },
        ]
    ]
    assert build_dislike_reason_keyboard(digest_id)["inline_keyboard"][0][0]["text"] == (
        "📌 Не моя тема"
    )
    assert build_research_keyboard(digest_id, lang="en")["inline_keyboard"] == [
        [{"text": "🔎 Check the web", "callback_data": build_research_callback(digest_id)}]
    ]
    assert build_research_keyboard(digest_id)["inline_keyboard"][0][0]["text"] == (
        "🔎 Уточнить в сети"
    )


def test_ui_string_lookup_falls_back_to_russian() -> None:
    assert t("btn_like", "en") == "👍 Interesting"
    assert t("btn_like", "xx") == "👍 Интересно"


def test_discussion_prompt_assembles_digest_excerpts_and_output_language() -> None:
    digest = _make_digest(
        headline="NBP decision",
        summary="The central bank held rates.",
        why_it_matters="Mortgage costs may stay elevated.",
    )
    prompt = render_discussion_prompt(
        digest=digest,
        articles=[
            EventArticle(
                source_name="official",
                title="Rate statement",
                url="https://example.com/statement",
                excerpt="The council kept rates unchanged.",
            )
        ],
        question="What does this mean for mortgages?",
        output_language="ru",
    )

    assert "NBP decision" in prompt
    assert "The council kept rates unchanged." in prompt
    assert "What does this mean for mortgages?" in prompt
    assert "ALWAYS answer in ru" in prompt
    assert "needs_research=true" in prompt


@pytest.mark.asyncio
async def test_discussion_needs_research_writes_pending_and_keyboard_is_sent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Uncovered")

    class FakeLLMClient:
        async def call_structured(self, **_: Any) -> LLMResponse[DiscussionReply]:
            return LLMResponse[DiscussionReply](
                output=DiscussionReply(answer="Не хватает данных.", needs_research=True),
                usage=LLMUsage(input_tokens=10, output_tokens=5, cost_usd=Decimal("0.000010")),
                model="gpt-4o-mini",
            )

    from delivery.discussion import answer_digest_question

    settings = delivery_client.get_settings()
    answer = await answer_digest_question(
        session=db_session,
        settings=settings,
        llm_client=FakeLLMClient(),  # type: ignore[arg-type]
        chat_id=123456,
        digest_id=digest.id,
        question="What is missing?",
    )

    pending = await db_session.get(ResearchPending, 123456)
    assert answer.offer_research is True
    assert pending is not None
    assert pending.digest_id == digest.id
    assert pending.question == "What is missing?"

    sent: list[tuple[int, str, dict[str, Any] | None]] = []

    class FakeTelegramClient:
        async def send_message(
            self,
            chat_id: int,
            text: str,
            *,
            reply_markup: dict[str, Any] | None = None,
            **_: Any,
        ) -> dict[str, Any]:
            sent.append((chat_id, text, reply_markup))
            return {"ok": True}

    async def fake_answer_digest_question(**_: Any) -> DiscussionAnswer:
        return DiscussionAnswer(text="Grounded but incomplete", offer_research=True)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr("delivery.listener.service.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "delivery.listener.service.answer_digest_question",
        fake_answer_digest_question,
    )
    db_session.add(DiscussionPending(chat_id=123456, digest_id=digest.id))
    await db_session.flush()

    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={"update_id": 501, "message": {"chat": {"id": 123456}, "text": "Explain?"}},
    )

    assert sent == [(123456, "Grounded but incomplete", build_research_keyboard(digest.id))]


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

    recorded_calls: list[tuple[int, str, dict[str, Any] | None]] = []

    class FakeTelegramClient:
        async def send_message(
            self,
            chat_id: int,
            text: str,
            *,
            reply_markup: dict[str, Any] | None = None,
            **_: Any,
        ) -> dict[str, Any]:
            recorded_calls.append((chat_id, text, reply_markup))
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
    assert all(chat_id == 123456 for chat_id, _, _ in recorded_calls)
    assert all(reply_markup is not None for _, _, reply_markup in recorded_calls)
    assert first.delivered_at is not None
    assert second.delivered_at is not None
    assert delivered.delivered_at is not None
    assert await db_session.scalar(select(func.count()).select_from(Impression)) == 2


class _RecordingTelegramClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_message_id = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        self._next_message_id += 1
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return {"ok": True, "result": {"message_id": self._next_message_id}}

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "edited": True, **kwargs}
        )
        return {"ok": True, "result": {"message_id": message_id}}


@pytest.mark.asyncio
async def test_batched_delivery_notifies_without_revealing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_a = await _create_event(db_session)
    event_b = await _create_event(db_session)
    first = await _create_digest_row(db_session, event_id=event_a.id, headline="First")
    second = await _create_digest_row(db_session, event_id=event_b.id, headline="Second")
    client = _RecordingTelegramClient()

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def unrelated(*_: Any) -> PairJudgement:
        return _thread_judgement(False)

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "batched_delivery_enabled", True)
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_pending(client=client, adjudicator=unrelated)

    batch = await db_session.scalar(select(DeliveryBatch))
    assert report.sent == 0
    assert batch is not None
    assert first.batch_id == second.batch_id == batch.id
    assert first.delivered_at is None and second.delivered_at is None
    assert len(client.calls) == 1
    assert "2" in client.calls[0]["text"]
    assert client.calls[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        build_reveal_callback(batch.id)
    )
    assert await db_session.scalar(select(func.count()).select_from(Impression)) == 0


@pytest.mark.asyncio
async def test_due_tick_with_batching_notifies_once_and_records_local_date(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await db_session.scalar(select(User).where(User.username == "volodymyr"))
    assert user is not None
    user.timezone = "Pacific/Auckland"
    user.delivery_slot = "morning"
    event = await _create_event(db_session)
    await _create_digest_row(db_session, event_id=event.id, headline="Scheduled")
    client = _RecordingTelegramClient()

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "batched_delivery_enabled", True)
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)
    instant = datetime(2026, 1, 10, 22, 30, tzinfo=UTC)  # Jan 11, 11:30 in Auckland.

    first = await deliver_due(now=instant, client=client)
    second = await deliver_due(now=instant + timedelta(hours=1), client=client)

    assert first.sent == 0 and first.failed == 0
    assert second.sent == 0 and second.skipped == 1
    assert user.last_delivery_date == date(2026, 1, 11)
    assert len(client.calls) == 1
    assert t("batch_notification", user.ui_language).split("{")[0] in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_due_tick_retries_only_digest_that_failed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await db_session.scalar(select(User).where(User.username == "volodymyr"))
    assert user is not None
    user.delivery_slot = "morning"
    digests: list[Digest] = []
    for headline in ("First", "Retry me", "Third"):
        event = await _create_event(db_session)
        digests.append(await _create_digest_row(db_session, event_id=event.id, headline=headline))

    class FailOnceClient(_RecordingTelegramClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
            if "Retry me" in text and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("temporary Telegram failure")
            return await super().send_message(chat_id, text, **kwargs)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "batched_delivery_enabled", False)
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)
    instant = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
    client = FailOnceClient()

    first = await deliver_due(now=instant, client=client)

    assert first.sent == 2 and first.failed == 1
    assert [digest.delivered_at is not None for digest in digests] == [True, False, True]
    assert user.last_delivery_date is None

    second = await deliver_due(now=instant + timedelta(hours=1), client=client)

    assert second.sent == 1 and second.failed == 0
    assert all(digest.delivered_at is not None for digest in digests)
    assert user.last_delivery_date == date(2026, 1, 10)
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_invalid_timezone_does_not_block_later_user(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = await db_session.scalar(select(User).where(User.username == "volodymyr"))
    assert primary is not None
    malformed = User(
        username="aaa_malformed",
        chat_id=222222,
        profile=primary.profile,
        timezone="Europe/Warsaw",
    )
    db_session.add(malformed)
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Still delivered")
    await db_session.flush()
    await db_session.execute(
        update(User)
        .where(User.id == malformed.id)
        .values(timezone="Mars/Olympus_Mons")
        .execution_options(synchronize_session=False)
    )
    db_session.expunge(malformed)
    client = _RecordingTelegramClient()

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "batched_delivery_enabled", False)
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_due(
        now=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
        client=client,
    )

    assert report.sent == 1 and report.failed == 1
    assert digest.delivered_at is not None
    assert [call["chat_id"] for call in client.calls] == [primary.chat_id]


@pytest.mark.asyncio
async def test_reveal_page_records_impressions_and_closes_batch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = DeliveryBatch(chat_id=123456, notification_message_id=77, notified_at=datetime.now(UTC))
    db_session.add(batch)
    await db_session.flush()
    for headline in ("One", "Two"):
        event = await _create_event(db_session)
        digest = await _create_digest_row(db_session, event_id=event.id, headline=headline)
        digest.batch_id = batch.id
    client = _RecordingTelegramClient()

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "batch_reveal_page_size", 5)
    monkeypatch.setattr(settings, "link_tracking_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    await reveal_batch_page(
        batch_id=batch.id,
        chat_id=123456,
        client=client,
        settings=settings,
        ui_language="ru",
    )

    assert await db_session.scalar(select(func.count()).select_from(Impression)) == 2
    assert batch.closed_at is not None
    assert len([call for call in client.calls if not call.get("edited")]) == 2
    assert client.calls[-1]["edited"] is True
    assert client.calls[-1]["text"] == t("batch_all_shown", settings.ui_language)


def test_reveal_callback_data_is_typed() -> None:
    reveal = parse_callback_data(build_reveal_callback(42))
    more = parse_callback_data(build_reveal_more_callback(42))

    assert reveal is not None and reveal.action == "reveal" and reveal.batch_id == 42
    assert more is not None and more.action == "reveal_more" and more.batch_id == 42
    assert reveal.digest_id is None and more.digest_id is None


def _thread_judgement(same_event: bool) -> PairJudgement:
    return PairJudgement(
        same_event=same_event,
        reason="test verdict",
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=3,
        cost_usd=Decimal("0.000004"),
    )


@pytest.mark.asyncio
async def test_dispatcher_threads_same_story_update(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0), article_count=3)
    update_event = await _create_event(db_session, centroid=_axis_centroid(0), article_count=2)
    parent = await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Kyiv strike",
        delivered_at=datetime.now(UTC),
        telegram_message_id=321,
    )
    update = await _create_digest_row(db_session, event_id=update_event.id, headline="Kyiv toll")
    client = _RecordingTelegramClient()

    async def same_adjudicator(*_: Any) -> PairJudgement:
        return _thread_judgement(True)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_pending(client=client, adjudicator=same_adjudicator)

    assert report.sent == 1
    assert client.calls[0]["reply_to_message_id"] == 321
    assert client.calls[0]["disable_notification"] is True
    assert "Обновление по теме" in client.calls[0]["text"]
    assert "Kyiv strike" in client.calls[0]["text"]
    assert client.calls[0]["reply_markup"] == build_digest_keyboard(update.id)
    assert update.telegram_message_id == 1001
    impression = await db_session.scalar(
        select(Impression).where(Impression.digest_id == update.id)
    )
    assert impression is not None
    assert impression.context is not None
    assert impression.context["threaded_parent_digest_id"] == parent.id


@pytest.mark.asyncio
async def test_dispatcher_sends_top_level_when_unrelated(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update_event = await _create_event(db_session, centroid=_axis_centroid(0))
    await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Kyiv strike",
        delivered_at=datetime.now(UTC),
        telegram_message_id=321,
    )
    update = await _create_digest_row(db_session, event_id=update_event.id, headline="Kharkiv")
    client = _RecordingTelegramClient()

    async def different_adjudicator(*_: Any) -> PairJudgement:
        return _thread_judgement(False)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    await deliver_pending(client=client, adjudicator=different_adjudicator)

    assert "reply_to_message_id" not in client.calls[0]
    assert "disable_notification" not in client.calls[0]
    assert update.telegram_message_id == 1001


@pytest.mark.asyncio
async def test_dispatcher_sends_top_level_when_adjudicator_fails(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update_event = await _create_event(db_session, centroid=_axis_centroid(0))
    await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Kyiv strike",
        delivered_at=datetime.now(UTC),
        telegram_message_id=321,
    )
    update = await _create_digest_row(db_session, event_id=update_event.id, headline="Kyiv toll")
    client = _RecordingTelegramClient()

    async def failing_adjudicator(*_: Any) -> PairJudgement:
        raise RuntimeError("openai unavailable")

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_pending(client=client, adjudicator=failing_adjudicator)

    # Adjudicator failure must not block delivery — the post still goes out top-level.
    assert report.sent == 1
    assert report.failed == 0
    assert "reply_to_message_id" not in client.calls[0]
    assert update.telegram_message_id == 1001


@pytest.mark.asyncio
async def test_dispatcher_chains_to_latest_thread_message(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update1_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update2_event = await _create_event(db_session, centroid=_axis_centroid(0))
    old_time = datetime.now(UTC) - timedelta(hours=2)
    recent_time = datetime.now(UTC) - timedelta(minutes=5)
    await _create_digest_row(
        db_session,
        event_id=root_event.id,
        headline="Kyiv strike",
        delivered_at=old_time,
        telegram_message_id=321,
    )
    update1 = await _create_digest_row(
        db_session,
        event_id=update1_event.id,
        headline="Kyiv toll update",
        delivered_at=recent_time,
        telegram_message_id=654,
    )
    await _create_digest_row(db_session, event_id=update2_event.id, headline="Kyiv toll update 2")
    client = _RecordingTelegramClient()

    async def same_adjudicator(*_: Any) -> PairJudgement:
        return _thread_judgement(True)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    await deliver_pending(client=client, adjudicator=same_adjudicator)

    assert client.calls[0]["reply_to_message_id"] == 654
    impression = await db_session.scalar(select(Impression))
    assert impression is not None
    assert impression.context is not None
    assert impression.context["threaded_parent_digest_id"] == update1.id


@pytest.mark.asyncio
async def test_dispatcher_ignores_pre_feature_parent_without_message_id(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update_event = await _create_event(db_session, centroid=_axis_centroid(0))
    await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Old delivered",
        delivered_at=datetime.now(UTC),
        telegram_message_id=None,
    )
    await _create_digest_row(db_session, event_id=update_event.id, headline="Update")
    client = _RecordingTelegramClient()
    adjudicator_calls = 0

    async def unused_adjudicator(*_: Any) -> PairJudgement:
        nonlocal adjudicator_calls
        adjudicator_calls += 1
        return _thread_judgement(True)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    await deliver_pending(client=client, adjudicator=unused_adjudicator)

    assert "reply_to_message_id" not in client.calls[0]
    assert adjudicator_calls == 0


@pytest.mark.asyncio
async def test_dispatcher_retries_top_level_when_reply_target_missing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update_event = await _create_event(db_session, centroid=_axis_centroid(0))
    await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Kyiv strike",
        delivered_at=datetime.now(UTC),
        telegram_message_id=321,
    )
    update = await _create_digest_row(db_session, event_id=update_event.id, headline="Kyiv toll")
    calls: list[dict[str, Any]] = []

    class MissingReplyClient:
        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"chat_id": chat_id, "text": text, **kwargs})
            if len(calls) == 1:
                raise RuntimeError("Bad Request: message to be replied not found")
            return {"ok": True, "result": {"message_id": 999}}

    async def same_adjudicator(*_: Any) -> PairJudgement:
        return _thread_judgement(True)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    report = await deliver_pending(client=MissingReplyClient(), adjudicator=same_adjudicator)  # type: ignore[arg-type]

    assert report.sent == 1
    assert calls[0]["reply_to_message_id"] == 321
    assert "reply_to_message_id" not in calls[1]
    assert "disable_notification" not in calls[1]
    assert update.delivered_at is not None
    assert update.telegram_message_id == 999


@pytest.mark.asyncio
async def test_dispatcher_threading_disabled_never_adjudicates(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update_event = await _create_event(db_session, centroid=_axis_centroid(0))
    await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Kyiv strike",
        delivered_at=datetime.now(UTC),
        telegram_message_id=321,
    )
    await _create_digest_row(db_session, event_id=update_event.id, headline="Kyiv toll")
    client = _RecordingTelegramClient()

    async def forbidden_adjudicator(*_: Any) -> PairJudgement:
        raise AssertionError("adjudicator should not be called")

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", False)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    await deliver_pending(client=client, adjudicator=forbidden_adjudicator)

    assert "reply_to_message_id" not in client.calls[0]


@pytest.mark.asyncio
async def test_dispatcher_thread_window_bound(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_event = await _create_event(db_session, centroid=_axis_centroid(0))
    update_event = await _create_event(db_session, centroid=_axis_centroid(0))
    await _create_digest_row(
        db_session,
        event_id=parent_event.id,
        headline="Old Kyiv strike",
        delivered_at=datetime.now(UTC) - timedelta(hours=73),
        telegram_message_id=321,
    )
    await _create_digest_row(db_session, event_id=update_event.id, headline="Kyiv toll")
    client = _RecordingTelegramClient()

    async def forbidden_adjudicator(*_: Any) -> PairJudgement:
        raise AssertionError("old parent should not be adjudicated")

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "thread_updates_enabled", True)
    monkeypatch.setattr(settings, "thread_window_hours", 72)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)

    await deliver_pending(client=client, adjudicator=forbidden_adjudicator)

    assert "reply_to_message_id" not in client.calls[0]


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
    impressions = (await db_session.scalars(select(Impression))).all()
    assert {impression.digest_id for impression in impressions} == {first.id, third.id}


@pytest.mark.asyncio
async def test_dispatcher_taste_reranks_and_major_floor_protects_big_events(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed one like (topic axis 0) and one dislike (topic axis 1) to define taste.
    liked_event = await _create_event(db_session, centroid=_axis_centroid(0))
    disliked_event = await _create_event(db_session, centroid=_axis_centroid(1))
    liked_digest = await _create_digest_row(
        db_session, event_id=liked_event.id, headline="Liked seed", delivered_at=datetime.now(UTC)
    )
    disliked_digest = await _create_digest_row(
        db_session,
        event_id=disliked_event.id,
        headline="Disliked seed",
        delivered_at=datetime.now(UTC),
    )
    db_session.add_all(
        [
            DigestFeedback(digest_id=liked_digest.id, chat_id=123456, feedback="like"),
            DigestFeedback(digest_id=disliked_digest.id, chat_id=123456, feedback="dislike"),
        ]
    )
    await db_session.flush()

    # Three pending digests: on-taste (low sig), off-taste routine (low sig),
    # and an off-taste but MAJOR event (high confidence) that the floor must lift.
    taste_event = await _create_event(db_session, centroid=_axis_centroid(0))
    routine_event = await _create_event(db_session, centroid=_axis_centroid(1))
    major_event = await _create_event(db_session, centroid=_axis_centroid(1))
    await _create_digest_row(
        db_session, event_id=taste_event.id, headline="On taste", confidence_level="low"
    )
    await _create_digest_row(
        db_session, event_id=routine_event.id, headline="Routine strike", confidence_level="low"
    )
    await _create_digest_row(
        db_session, event_id=major_event.id, headline="Major strike", confidence_level="high"
    )

    recorded_headlines: list[str] = []

    class FakeTelegramClient:
        async def send_message(self, _chat_id: int, text: str, **_: Any) -> dict[str, Any]:
            recorded_headlines.append(text)
            return {"ok": True}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "taste_min_labels_per_class", 1)
    monkeypatch.setattr("delivery.dispatcher.session_scope", fake_session_scope)
    monkeypatch.setattr("delivery.dispatcher._build_client", lambda: FakeTelegramClient())

    report = await deliver_pending()

    assert report.sent == 3
    order = [
        next(h for h in ["Major strike", "On taste", "Routine strike"] if h in text)
        for text in recorded_headlines
    ]
    # Major floor first; then on-taste above off-taste routine.
    assert order == ["Major strike", "On taste", "Routine strike"]

    # taste_score is logged into each impression for later calibration.
    impressions = (await db_session.scalars(select(Impression))).all()
    contexts = [imp.context for imp in impressions]
    assert all(ctx is not None and "taste_cosine" in ctx for ctx in contexts)
    assert any(ctx["major"] for ctx in contexts)


@pytest.mark.asyncio
async def test_taste_vector_excludes_weak_analysis_dislikes(
    db_session: AsyncSession,
) -> None:
    liked_event = await _create_event(db_session, centroid=_axis_centroid(0))
    weak_event = await _create_event(db_session, centroid=_axis_centroid(1))
    off_topic_event = await _create_event(db_session, centroid=_axis_centroid(2))
    legacy_event = await _create_event(db_session, centroid=_axis_centroid(3))
    liked_digest = await _create_digest_row(
        db_session, event_id=liked_event.id, headline="Liked", delivered_at=datetime.now(UTC)
    )
    weak_digest = await _create_digest_row(
        db_session,
        event_id=weak_event.id,
        headline="Weak analysis",
        delivered_at=datetime.now(UTC),
    )
    off_topic_digest = await _create_digest_row(
        db_session,
        event_id=off_topic_event.id,
        headline="Off topic",
        delivered_at=datetime.now(UTC),
    )
    legacy_digest = await _create_digest_row(
        db_session, event_id=legacy_event.id, headline="Legacy", delivered_at=datetime.now(UTC)
    )
    db_session.add_all(
        [
            DigestFeedback(digest_id=liked_digest.id, chat_id=123456, feedback="like"),
            DigestFeedback(
                digest_id=weak_digest.id,
                chat_id=123456,
                feedback="dislike",
                reason="weak_analysis",
            ),
            DigestFeedback(
                digest_id=off_topic_digest.id,
                chat_id=123456,
                feedback="dislike",
                reason="off_topic",
            ),
            DigestFeedback(digest_id=legacy_digest.id, chat_id=123456, feedback="dislike"),
        ]
    )
    await db_session.flush()

    taste = await build_taste_vector(db_session, chat_id=123456, min_labels_per_class=1)

    assert taste is not None
    assert taste.n_like == 1
    assert taste.n_dislike == 2


@pytest.mark.asyncio
async def test_feedback_append_latest_wins_and_duplicate_reprocessing_is_idempotent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Feedback")

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    update = {
        "callback_query": {
            "id": "cb1",
            "data": build_feedback_callback("like", digest.id),
            "message": {"message_id": 10, "chat": {"id": 123456}},
        }
    }

    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )
    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )

    assert await db_session.scalar(select(func.count()).select_from(DigestFeedback)) == 1

    update["callback_query"]["data"] = build_feedback_callback("dislike", digest.id)
    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )

    current = await latest_feedback(db_session, digest_id=digest.id, chat_id=123456)
    assert current is not None
    assert current.feedback == "dislike"
    assert current.reason is None
    assert await db_session.scalar(select(func.count()).select_from(DigestFeedback)) == 2


@pytest.mark.asyncio
async def test_each_button_press_writes_one_uniform_ui_event(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="UI events")

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def send_message(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    callbacks = (
        ("like", build_feedback_callback("like", digest.id)),
        ("dislike", build_feedback_callback("dislike", digest.id)),
        (
            "dislike_reason",
            build_dislike_reason_callback("weak_analysis", digest.id),
        ),
        ("discussion", build_discussion_callback(digest.id)),
        ("research", build_research_callback(digest.id)),
    )

    for action, callback_data in callbacks:
        await handle_update(
            session=db_session,
            settings=settings,
            telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update={
                "callback_query": {
                    "id": f"cb-{action}",
                    "data": callback_data,
                    "message": {"message_id": 10, "chat": {"id": 123456}},
                }
            },
        )

    ui_events = list((await db_session.scalars(select(UIEvent).order_by(UIEvent.id))).all())
    assert [(item.action, item.chat_id, item.digest_id) for item in ui_events] == [
        (action, 123456, digest.id) for action, _ in callbacks
    ]
    assert ui_events[2].context == {"reason": "weak_analysis"}
    assert await db_session.scalar(select(func.count()).select_from(DigestFeedback)) == 2


@pytest.mark.asyncio
async def test_unknown_callback_is_logged_without_crashing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    result = await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=object(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb-stale",
                "data": "old:button:payload",
                "message": {"chat": {"id": 123456}},
            }
        },
    )

    ui_event = await db_session.scalar(select(UIEvent))
    assert result == listener_handlers.HandlerResult()
    assert ui_event is not None
    assert ui_event.action == "unknown_callback"
    assert ui_event.digest_id is None
    assert ui_event.context == {"data": "old:button:payload"}


@pytest.mark.asyncio
async def test_discussion_answer_logs_length_without_question_text(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Question")
    db_session.add(DiscussionPending(chat_id=123456, digest_id=digest.id))
    await db_session.flush()
    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    result = await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=object(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={"message": {"chat": {"id": 123456}, "text": "  Private question?  "}},
    )

    ui_event = await db_session.scalar(select(UIEvent))
    assert result.discussion is not None
    assert ui_event is not None
    assert ui_event.action == "discussion_question"
    assert ui_event.context == {"question_length": 17}
    assert "Private question" not in str(ui_event.context)


@pytest.mark.asyncio
async def test_ui_event_failure_does_not_affect_feedback_or_callback(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Best effort")
    answered: list[str] = []

    @asynccontextmanager
    async def failing_session_scope() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("analytics unavailable")
        yield db_session

    class FakeTelegramClient:
        async def answer_callback_query(self, callback_id: str, _text: str) -> dict[str, Any]:
            answered.append(callback_id)
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    monkeypatch.setattr(listener_handlers, "session_scope", failing_session_scope)
    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    result = await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb-like",
                "data": build_feedback_callback("like", digest.id),
                "message": {"message_id": 10, "chat": {"id": 123456}},
            }
        },
    )

    feedback = await latest_feedback(db_session, digest_id=digest.id, chat_id=123456)
    assert result == listener_handlers.HandlerResult()
    assert feedback is not None and feedback.feedback == "like"
    assert answered == ["cb-like"]


@pytest.mark.asyncio
async def test_dislike_callback_records_feedback_and_shows_reason_keyboard(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Feedback reason")
    answers: list[str] = []
    markups: list[dict[str, Any]] = []

    class FakeTelegramClient:
        async def answer_callback_query(self, _callback_id: str, text: str) -> dict[str, Any]:
            answers.append(text)
            return {"ok": True}

        async def edit_message_reply_markup(
            self,
            _chat_id: int,
            _message_id: int,
            reply_markup: dict[str, Any],
        ) -> dict[str, Any]:
            markups.append(reply_markup)
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb-dislike",
                "data": build_feedback_callback("dislike", digest.id),
                "message": {"message_id": 10, "chat": {"id": 123456}},
            }
        },
    )

    current = await latest_feedback(db_session, digest_id=digest.id, chat_id=123456)
    assert current is not None
    assert current.feedback == "dislike"
    assert current.reason is None
    assert answers == ["Почему не интересно?"]
    assert markups == [build_dislike_reason_keyboard(digest.id)]


@pytest.mark.asyncio
async def test_feedback_callbacks_use_english_ui_language(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    liked = await _create_digest_row(db_session, event_id=event.id, headline="Liked")
    disliked = await _create_digest_row(db_session, event_id=event.id, headline="Disliked")
    answers: list[str] = []
    markups: list[dict[str, Any]] = []

    class FakeTelegramClient:
        async def answer_callback_query(self, _callback_id: str, text: str) -> dict[str, Any]:
            answers.append(text)
            return {"ok": True}

        async def edit_message_reply_markup(
            self,
            _chat_id: int,
            _message_id: int,
            reply_markup: dict[str, Any],
        ) -> dict[str, Any]:
            markups.append(reply_markup)
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "ui_language", "en")
    user = await db_session.scalar(select(User).where(User.chat_id == 123456))
    assert user is not None
    user.ui_language = "en"
    await db_session.flush()

    for callback_id, callback_data in (
        ("cb-like", build_feedback_callback("like", liked.id)),
        ("cb-dislike", build_feedback_callback("dislike", disliked.id)),
    ):
        await handle_update(
            session=db_session,
            settings=settings,
            telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update={
                "callback_query": {
                    "id": callback_id,
                    "data": callback_data,
                    "message": {"message_id": 10, "chat": {"id": 123456}},
                }
            },
        )

    assert answers == ["Saved ✓", "Why not interesting?"]
    assert markups == [
        build_digest_keyboard(liked.id, selected_feedback="like", lang="en"),
        build_dislike_reason_keyboard(disliked.id, lang="en"),
    ]


@pytest.mark.asyncio
async def test_feedback_callbacks_default_to_russian_ui_language(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    liked = await _create_digest_row(db_session, event_id=event.id, headline="Liked")
    disliked = await _create_digest_row(db_session, event_id=event.id, headline="Disliked")
    answers: list[str] = []

    class FakeTelegramClient:
        async def answer_callback_query(self, _callback_id: str, text: str) -> dict[str, Any]:
            answers.append(text)
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "ui_language", "ru")

    for callback_id, callback_data in (
        ("cb-like", build_feedback_callback("like", liked.id)),
        ("cb-dislike", build_feedback_callback("dislike", disliked.id)),
    ):
        await handle_update(
            session=db_session,
            settings=settings,
            telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update={
                "callback_query": {
                    "id": callback_id,
                    "data": callback_data,
                    "message": {"message_id": 10, "chat": {"id": 123456}},
                }
            },
        )

    assert answers == ["Записал ✓", "Почему не интересно?"]


@pytest.mark.asyncio
async def test_dislike_reason_callbacks_update_latest_dislike_and_restore_keyboard(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Feedback reason")
    answers: list[str] = []
    markups: list[dict[str, Any]] = []

    class FakeTelegramClient:
        async def answer_callback_query(self, _callback_id: str, text: str) -> dict[str, Any]:
            answers.append(text)
            return {"ok": True}

        async def edit_message_reply_markup(
            self,
            _chat_id: int,
            _message_id: int,
            reply_markup: dict[str, Any],
        ) -> dict[str, Any]:
            markups.append(reply_markup)
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    db_session.add(DigestFeedback(digest_id=digest.id, chat_id=123456, feedback="dislike"))
    await db_session.flush()

    for callback_data, expected_reason in (
        (build_dislike_reason_callback("weak_analysis", digest.id), "weak_analysis"),
        (build_dislike_reason_callback("off_topic", digest.id), "off_topic"),
    ):
        await handle_update(
            session=db_session,
            settings=settings,
            telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update={
                "callback_query": {
                    "id": f"cb-{expected_reason}",
                    "data": callback_data,
                    "message": {"message_id": 10, "chat": {"id": 123456}},
                }
            },
        )
        current = await latest_feedback(db_session, digest_id=digest.id, chat_id=123456)
        assert current is not None
        assert current.reason == expected_reason

    assert answers == ["Записал ✓", "Записал ✓"]
    assert markups == [
        build_digest_keyboard(digest.id, selected_feedback="dislike"),
        build_digest_keyboard(digest.id, selected_feedback="dislike"),
    ]


@pytest.mark.asyncio
async def test_dislike_reason_callback_without_prior_dislike_is_noop(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="No prior dislike")

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb-reason-without-dislike",
                "data": build_dislike_reason_callback("weak_analysis", digest.id),
                "message": {"message_id": 10, "chat": {"id": 123456}},
            }
        },
    )

    assert await db_session.scalar(select(func.count()).select_from(DigestFeedback)) == 0


@pytest.mark.asyncio
async def test_feedback_persists_when_cosmetic_callback_calls_fail(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Cosmetic")

    class FailingCosmeticTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("callback too old")

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("message too old")

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FailingCosmeticTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "old-cb",
                "data": build_feedback_callback("like", digest.id),
                "message": {"message_id": 10, "chat": {"id": 123456}},
            }
        },
    )

    current = await latest_feedback(db_session, digest_id=digest.id, chat_id=123456)
    assert current is not None
    assert current.feedback == "like"


@pytest.mark.asyncio
async def test_listener_rejects_foreign_chat_id(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Foreign")

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise AssertionError("foreign chat should not be answered")

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb1",
                "data": build_feedback_callback("like", digest.id),
                "message": {"message_id": 10, "chat": {"id": 999}},
            }
        },
    )

    assert await db_session.scalar(select(func.count()).select_from(DigestFeedback)) == 0


@pytest.mark.asyncio
async def test_discussion_pending_second_click_replaces_target_and_expired_is_consumed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    first = await _create_digest_row(db_session, event_id=event.id, headline="First discussion")
    second = await _create_digest_row(db_session, event_id=event.id, headline="Second discussion")
    sent_messages: list[str] = []

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def send_message(self, _chat_id: int, text: str, **__: Any) -> dict[str, Any]:
            sent_messages.append(text)
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    for digest in (first, second):
        await handle_update(
            session=db_session,
            settings=settings,
            telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update={
                "callback_query": {
                    "id": f"cb-{digest.id}",
                    "data": build_discussion_callback(digest.id),
                    "message": {"message_id": 10, "chat": {"id": 123456}},
                }
            },
        )

    pending = await db_session.get(DiscussionPending, 123456)
    assert pending is not None
    assert pending.digest_id == second.id
    prompt_message = "Задайте вопрос по этому разбору одним сообщением."
    assert sent_messages == [
        prompt_message,
        prompt_message,
    ]

    pending.created_at = datetime.now(UTC) - timedelta(minutes=16)
    await db_session.flush()
    sent_messages.clear()

    result = await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={"message": {"chat": {"id": 123456}, "text": "Explain?"}},
    )

    assert await db_session.get(DiscussionPending, 123456) is None
    assert sent_messages == []
    assert result.messages == [
        (123456, "Срок вопроса истёк — нажмите 💬 ещё раз."),
    ]


@pytest.mark.asyncio
async def test_discussion_pending_persists_when_prompt_acknowledgement_fails(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Prompt fails")

    class FailingTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("query is too old")

        async def send_message(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("cannot send prompt")

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FailingTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb-discuss",
                "data": build_discussion_callback(digest.id),
                "message": {"message_id": 10, "chat": {"id": 123456}},
            }
        },
    )

    pending = await db_session.get(DiscussionPending, 123456)
    assert pending is not None
    assert pending.digest_id == digest.id


@pytest.mark.asyncio
async def test_discussion_message_reprocessing_does_not_call_llm_twice(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Discuss once")
    db_session.add(DiscussionPending(chat_id=123456, digest_id=digest.id))
    await db_session.flush()
    answers: list[tuple[int, str]] = []
    llm_calls = 0

    class FakeTelegramClient:
        async def send_message(self, chat_id: int, text: str, **__: Any) -> dict[str, Any]:
            answers.append((chat_id, text))
            return {"ok": True}

    async def fake_answer_digest_question(**_: Any) -> DiscussionAnswer:
        nonlocal llm_calls
        llm_calls += 1
        return DiscussionAnswer(text="Grounded answer", offer_research=False)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr("delivery.listener.service.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "delivery.listener.service.answer_digest_question",
        fake_answer_digest_question,
    )
    update = {"update_id": 50, "message": {"chat": {"id": 123456}, "text": "Explain?"}}

    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )
    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )

    assert llm_calls == 1
    assert answers == [(123456, "Grounded answer")]
    assert await db_session.get(DiscussionPending, 123456) is None


@pytest.mark.asyncio
async def test_research_callback_consumes_pending_and_post_commit_runs_once(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Research once")
    db_session.add(ResearchPending(chat_id=123456, digest_id=digest.id, question="What changed?"))
    await db_session.flush()
    sent_messages: list[str] = []
    callback_answers: list[str] = []
    search_calls = 0
    llm_calls = 0

    class FakeTelegramClient:
        async def answer_callback_query(self, _callback_id: str, text: str) -> dict[str, Any]:
            callback_answers.append(text)
            return {"ok": True}

        async def send_message(self, _chat_id: int, text: str, **_: Any) -> dict[str, Any]:
            sent_messages.append(text)
            return {"ok": True}

    class FakeSearchClient:
        async def search(self, **_: Any) -> list[SearchResult]:
            nonlocal search_calls
            search_calls += 1
            return [
                SearchResult(
                    title="Official update",
                    url="https://example.com/update",
                    content="Official detail.",
                )
            ]

    class FakeLLMClient:
        async def call_structured(self, **_: Any) -> LLMResponse[ResearchReply]:
            nonlocal llm_calls
            llm_calls += 1
            return LLMResponse[ResearchReply](
                output=ResearchReply(answer="Fresh answer [1]."),
                usage=LLMUsage(input_tokens=20, output_tokens=8, cost_usd=Decimal("0.000100")),
                model="gpt-4o",
            )

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr("delivery.listener.service.session_scope", fake_session_scope)
    update = {
        "update_id": 601,
        "callback_query": {
            "id": "cb-research",
            "data": build_research_callback(digest.id),
            "message": {"message_id": 10, "chat": {"id": 123456}},
        },
    }

    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=FakeLLMClient(),  # type: ignore[arg-type]
        search_client=FakeSearchClient(),
        update=update,
    )
    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=FakeLLMClient(),  # type: ignore[arg-type]
        search_client=FakeSearchClient(),
        update=update,
    )

    assert search_calls == 1
    assert llm_calls == 1
    assert await db_session.get(ResearchPending, 123456) is None
    assert callback_answers == ["Ищу в сети…", "Запрос устарел"]
    assert any("Fresh answer [1]." in message for message in sent_messages)
    assert any("https://example.com/update" in message for message in sent_messages)
    assert any("Запрос устарел" in message for message in sent_messages)


@pytest.mark.asyncio
async def test_research_callback_expired_pending_sends_stale_message(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Expired research")
    db_session.add(
        ResearchPending(
            chat_id=123456,
            digest_id=digest.id,
            question="Still valid?",
            created_at=datetime.now(UTC) - timedelta(minutes=16),
        )
    )
    await db_session.flush()
    sent_messages: list[str] = []

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def send_message(self, _chat_id: int, text: str, **_: Any) -> dict[str, Any]:
            sent_messages.append(text)
            return {"ok": True}

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)

    result = await handle_update(
        session=db_session,
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={
            "callback_query": {
                "id": "cb-expired-research",
                "data": build_research_callback(digest.id),
                "message": {"message_id": 10, "chat": {"id": 123456}},
            }
        },
    )

    assert result.research is None
    assert sent_messages == ["Запрос устарел, нажмите 💬 заново."]
    assert await db_session.get(ResearchPending, 123456) is None


@pytest.mark.asyncio
async def test_research_daily_cap_skips_search(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Cap")
    db_session.add(
        Decision(
            run_id=uuid4(),
            stage_name="research",
            stage_version="v1",
            target_type="research",
            target_id=digest.id,
            model="gpt-4o",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.000001"),
            decision_json={"question": "already used"},
        )
    )
    await db_session.flush()

    class FailingSearchClient:
        async def search(self, **_: Any) -> list[SearchResult]:
            raise AssertionError("search should not run when cap is reached")

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "research_daily_cap", 1)

    chunks = await research_digest_question(
        session=db_session,
        settings=settings,
        llm_client=object(),  # type: ignore[arg-type]
        search_client=FailingSearchClient(),
        chat_id=123456,
        digest_id=digest.id,
        question="Need more?",
    )

    assert chunks == ["Лимит уточнений в сети на сегодня исчерпан. Попробуйте завтра."]


@pytest.mark.asyncio
async def test_research_daily_cap_uses_english_ui_language(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Cap")
    db_session.add(
        Decision(
            run_id=uuid4(),
            stage_name="research",
            stage_version="v1",
            target_type="research",
            target_id=digest.id,
            model="gpt-4o",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.000001"),
            decision_json={"question": "already used"},
        )
    )
    await db_session.flush()

    class FailingSearchClient:
        async def search(self, **_: Any) -> list[SearchResult]:
            raise AssertionError("search should not run when cap is reached")

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "research_daily_cap", 1)
    monkeypatch.setattr(settings, "ui_language", "en")

    chunks = await research_digest_question(
        session=db_session,
        settings=settings,
        llm_client=object(),  # type: ignore[arg-type]
        search_client=FailingSearchClient(),
        chat_id=123456,
        digest_id=digest.id,
        question="Need more?",
    )

    assert chunks == ["Daily web-lookup limit reached. Try again tomorrow."]


@pytest.mark.asyncio
async def test_discussion_digest_not_found_uses_english_ui_language(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delivery.discussion import answer_digest_question

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "ui_language", "en")

    answer = await answer_digest_question(
        session=db_session,
        settings=settings,
        llm_client=object(),  # type: ignore[arg-type]
        chat_id=123456,
        digest_id=999999,
        question="Can you explain?",
    )

    assert answer.text == "Couldn't find this brief. It may no longer be available."
    assert answer.offer_research is False


@pytest.mark.asyncio
async def test_poison_update_is_marked_processed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def failing_process_update(**_: Any) -> None:
        raise RuntimeError("poison update")

    settings = delivery_client.get_settings()
    monkeypatch.setattr("delivery.listener.service.session_scope", fake_session_scope)
    monkeypatch.setattr("delivery.listener.service.process_update", failing_process_update)

    await process_update_safely(
        settings=settings,
        telegram_client=object(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update={"update_id": 77},
    )

    assert await get_cursor(db_session) == 77


@pytest.mark.asyncio
async def test_get_updates_failure_backs_off_without_advancing_cursor(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    class FailingTelegramClient:
        async def get_updates(self, offset: int, timeout: int) -> dict[str, Any]:
            assert offset == 12
            assert timeout == 25
            raise RuntimeError("network after retries")

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("delivery.listener.service.asyncio.sleep", fake_sleep)

    payload, next_backoff = await get_updates_with_backoff(
        telegram_client=FailingTelegramClient(),  # type: ignore[arg-type]
        offset=12,
        timeout=25,
        backoff_seconds=2.0,
    )

    assert payload is None
    assert next_backoff == 4.0
    assert slept == [2.0]
    assert await get_cursor(db_session) == -1


@pytest.mark.asyncio
async def test_first_boot_initializes_cursor_to_latest_pending_update(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTelegramClient:
        async def get_updates(self, offset: int, timeout: int) -> dict[str, Any]:
            assert offset == 0
            assert timeout == 0
            return {"ok": True, "result": [{"update_id": 100}, {"update_id": 105}]}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("delivery.listener.service.session_scope", fake_session_scope)

    await initialize_cursor_if_missing(
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
    )

    assert await get_cursor(db_session) == 105


@pytest.mark.asyncio
async def test_process_update_advances_cursor_after_handling_duplicate_is_idempotent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await _create_event(db_session)
    digest = await _create_digest_row(db_session, event_id=event.id, headline="Cursor")

    class FakeTelegramClient:
        async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"ok": True}

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    settings = delivery_client.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr("delivery.listener.service.session_scope", fake_session_scope)

    update = {
        "update_id": 42,
        "callback_query": {
            "id": "cb1",
            "data": build_feedback_callback("like", digest.id),
            "message": {"message_id": 10, "chat": {"id": 123456}},
        },
    }
    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )
    await process_update(
        settings=settings,
        telegram_client=FakeTelegramClient(),  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=update,
    )

    assert await get_cursor(db_session) == 42
    assert await db_session.scalar(select(func.count()).select_from(DigestFeedback)) == 1


@pytest.mark.asyncio
async def test_telegram_bot_client_returns_payload_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        body = request.read().decode("utf-8")
        assert '"reply_to_message_id":42' in body
        assert '"disable_notification":true' in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        delivery_client,
        "_build_async_client",
        lambda *, timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    monkeypatch.setattr(delivery_client.get_settings(), "http_timeout_seconds", 0.01)

    client = delivery_client.TelegramBotClient("token")
    payload = await client.send_message(
        1,
        "hello",
        reply_to_message_id=42,
        disable_notification=True,
    )

    assert payload["ok"] is True
    assert payload["result"]["message_id"] == 1


@pytest.mark.asyncio
async def test_tavily_client_posts_search_body_and_normalizes_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.tavily.com/search"
        payload = httpx.QueryParams(request.url.query)
        assert payload == httpx.QueryParams()
        body = request.read().decode("utf-8")
        assert '"api_key":"key"' in body
        assert '"query":"query text"' in body
        assert '"search_depth":"advanced"' in body
        assert '"max_results":2' in body
        assert '"include_answer":false' in body
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Title",
                        "url": "https://example.com",
                        "content": "Snippet",
                    },
                    {"title": "Incomplete"},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        tavily_module,
        "_build_async_client",
        lambda *, timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )

    client = TavilyClient(api_key="key", timeout=0.01)
    results = await client.search(query="query text", search_depth="advanced", max_results=2)

    assert results == [SearchResult(title="Title", url="https://example.com", content="Snippet")]


@pytest.mark.asyncio
async def test_telegram_bot_client_get_updates_uses_long_poll_timeout_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeout: httpx.Timeout | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getUpdates")
        return httpx.Response(200, json={"ok": True, "result": []})

    def build_client(*, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
        nonlocal observed_timeout
        assert isinstance(timeout, httpx.Timeout)
        observed_timeout = timeout
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )

    monkeypatch.setattr(delivery_client, "_build_async_client", build_client)

    client = delivery_client.TelegramBotClient("token")
    await client.get_updates(offset=1, timeout=25)

    assert observed_timeout is not None
    assert observed_timeout.read == 35.0


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
