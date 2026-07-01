"""Tests for Telegram delivery formatting, dispatch, and client behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import engine._retry as retry_module
from delivery import client as delivery_client
from delivery.discussion import DiscussionAnswer, render_discussion_prompt
from delivery.dispatcher import deliver_pending
from delivery.formatter import MAX_TELEGRAM_MESSAGE_LENGTH, format_digest
from delivery.keyboards import (
    build_digest_keyboard,
    build_discussion_callback,
    build_dislike_reason_callback,
    build_dislike_reason_keyboard,
    build_feedback_callback,
    build_research_callback,
    build_research_keyboard,
    parse_callback_data,
)
from delivery.listener.handlers import handle_update, latest_feedback
from delivery.listener.service import (
    get_cursor,
    get_updates_with_backoff,
    initialize_cursor_if_missing,
    process_update,
    process_update_safely,
)
from delivery.research import research_digest_question
from engine.domain import Digest as DigestDTO
from engine.llm.client import LLMResponse, LLMUsage
from engine.llm.schemas import Citation, DiscussionReply, ResearchReply
from engine.models import (
    Decision,
    Digest,
    DigestFeedback,
    DiscussionPending,
    Event,
    Impression,
    ResearchPending,
)
from engine.ranking.taste import build_taste_vector
from engine.search import tavily as tavily_module
from engine.search.tavily import SearchResult, TavilyClient
from engine.stages._event_context import EventArticle


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
    delivered_at: datetime | None = None,
    confidence_level: str = "medium",
    created_at: datetime | None = None,
) -> Digest:
    digest = Digest(
        event_id=event_id,
        profile_name="volodymyr",
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

    taste = await build_taste_vector(db_session, min_labels_per_class=1)

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
