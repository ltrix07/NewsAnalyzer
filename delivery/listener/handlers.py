"""Handlers for Telegram feedback callbacks and discussion messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.client import TelegramBotClient
from delivery.keyboards import (
    FeedbackAction,
    build_digest_keyboard,
    build_dislike_reason_keyboard,
    parse_callback_data,
)
from engine.config import Settings
from engine.llm.client import LLMClient
from engine.models import DigestFeedback, DiscussionPending, ResearchPending

logger = structlog.get_logger(__name__)

DISCUSSION_PENDING_TTL = timedelta(minutes=15)
_ASK_QUESTION_MESSAGE = "Задайте вопрос по этому разбору одним сообщением."
_EXPIRED_QUESTION_MESSAGE = "Срок вопроса истёк — нажмите 💬 ещё раз."
_EXPIRED_RESEARCH_MESSAGE = "Запрос устарел, нажмите 💬 заново."


@dataclass(frozen=True, slots=True)
class DiscussionRequest:
    """A consumed pending discussion ready to answer after DB commit."""

    chat_id: int
    digest_id: int
    question: str


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """A consumed pending research request ready to run after DB commit."""

    chat_id: int
    digest_id: int
    question: str


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """Work that is safe to run only after the handler transaction commits."""

    discussion: DiscussionRequest | None = None
    research: ResearchRequest | None = None
    messages: list[tuple[int, str]] = field(default_factory=list)


async def handle_update(
    *,
    session: AsyncSession,
    settings: Settings,
    telegram_client: TelegramBotClient,
    llm_client: LLMClient,
    update: dict[str, Any],
) -> HandlerResult:
    """Process one Telegram update if it belongs to the configured chat."""

    expected_chat_id = settings.require_telegram_chat_id()
    chat_id = extract_chat_id(update)
    if chat_id != expected_chat_id:
        logger.info("telegram_update_ignored_foreign_chat", chat_id=chat_id)
        return HandlerResult()

    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        return await _handle_callback_query(
            session=session,
            settings=settings,
            telegram_client=telegram_client,
            callback_query=callback_query,
            chat_id=expected_chat_id,
        )

    message = update.get("message")
    if isinstance(message, dict):
        return await _handle_message(
            session=session,
            message=message,
            chat_id=expected_chat_id,
        )

    return HandlerResult()


def extract_chat_id(update: dict[str, Any]) -> int | None:
    """Return the chat id from a supported Telegram update shape."""

    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        message = callback_query.get("message")
        if isinstance(message, dict):
            return _chat_id_from_message(message)

    message = update.get("message")
    if isinstance(message, dict):
        return _chat_id_from_message(message)

    return None


async def latest_feedback(
    session: AsyncSession,
    *,
    digest_id: int,
    chat_id: int,
) -> DigestFeedback | None:
    """Return the current feedback row for a digest/chat pair."""

    result = await session.scalar(
        select(DigestFeedback)
        .where(DigestFeedback.digest_id == digest_id, DigestFeedback.chat_id == chat_id)
        .order_by(DigestFeedback.created_at.desc(), DigestFeedback.id.desc())
        .limit(1)
    )
    return result


async def _handle_callback_query(
    *,
    session: AsyncSession,
    settings: Settings,
    telegram_client: TelegramBotClient,
    callback_query: dict[str, Any],
    chat_id: int,
) -> HandlerResult:
    callback_query_id = callback_query.get("id")
    data = callback_query.get("data")
    if not isinstance(callback_query_id, str) or not isinstance(data, str):
        return HandlerResult()

    payload = parse_callback_data(data)
    if payload is None:
        return HandlerResult()

    if payload.action in {"like", "dislike"}:
        feedback: FeedbackAction = "like" if payload.action == "like" else "dislike"
        current = await latest_feedback(session, digest_id=payload.digest_id, chat_id=chat_id)
        if current is None or current.feedback != feedback:
            session.add(
                DigestFeedback(
                    digest_id=payload.digest_id,
                    chat_id=chat_id,
                    feedback=feedback,
                )
            )
            await session.flush()

        await _best_effort_answer_callback(
            telegram_client,
            callback_query_id,
            "Почему не интересно?" if feedback == "dislike" else "Записал ✓",
        )
        message = callback_query.get("message")
        message_id = _message_id(message)
        if message_id is not None:
            reply_markup = (
                build_dislike_reason_keyboard(payload.digest_id)
                if feedback == "dislike"
                else build_digest_keyboard(
                    payload.digest_id,
                    selected_feedback=feedback,
                )
            )
            await _best_effort_edit_reply_markup(
                telegram_client,
                chat_id,
                message_id,
                reply_markup,
            )
        return HandlerResult()

    if payload.action == "dislike_reason":
        current = await latest_feedback(session, digest_id=payload.digest_id, chat_id=chat_id)
        if current is not None and current.feedback == "dislike":
            current.reason = payload.reason
            await session.flush()

        await _best_effort_answer_callback(
            telegram_client,
            callback_query_id,
            "Записал ✓",
        )
        message = callback_query.get("message")
        message_id = _message_id(message)
        if message_id is not None:
            await _best_effort_edit_reply_markup(
                telegram_client,
                chat_id,
                message_id,
                build_digest_keyboard(
                    payload.digest_id,
                    selected_feedback="dislike",
                ),
            )
        return HandlerResult()

    if payload.action == "research":
        return await _handle_research_callback(
            session=session,
            settings=settings,
            telegram_client=telegram_client,
            callback_query_id=callback_query_id,
            chat_id=chat_id,
            digest_id=payload.digest_id,
        )

    await _upsert_discussion_pending(session, chat_id=chat_id, digest_id=payload.digest_id)
    await _best_effort_answer_callback(
        telegram_client,
        callback_query_id,
        "Ок, жду вопрос",
    )
    await _best_effort_send_message(
        telegram_client,
        chat_id,
        _ASK_QUESTION_MESSAGE,
    )
    return HandlerResult()


async def _handle_message(
    *,
    session: AsyncSession,
    message: dict[str, Any],
    chat_id: int,
) -> HandlerResult:
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return HandlerResult()

    pending = await session.get(DiscussionPending, chat_id)
    if pending is None:
        return HandlerResult()

    now = datetime.now(UTC)
    digest_id = pending.digest_id
    created_at = pending.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    await session.delete(pending)
    await session.flush()

    if now - created_at > DISCUSSION_PENDING_TTL:
        return HandlerResult(messages=[(chat_id, _EXPIRED_QUESTION_MESSAGE)])

    return HandlerResult(
        discussion=DiscussionRequest(
            chat_id=chat_id,
            digest_id=digest_id,
            question=text.strip(),
        )
    )


async def _upsert_discussion_pending(
    session: AsyncSession,
    *,
    chat_id: int,
    digest_id: int,
) -> None:
    created_at = datetime.now(UTC)
    statement = (
        insert(DiscussionPending)
        .values(chat_id=chat_id, digest_id=digest_id, created_at=created_at)
        .on_conflict_do_update(
            index_elements=[DiscussionPending.chat_id],
            set_={"digest_id": digest_id, "created_at": created_at},
        )
    )
    await session.execute(statement)
    await session.flush()


async def _handle_research_callback(
    *,
    session: AsyncSession,
    settings: Settings,
    telegram_client: TelegramBotClient,
    callback_query_id: str,
    chat_id: int,
    digest_id: int,
) -> HandlerResult:
    pending = await session.get(ResearchPending, chat_id)
    now = datetime.now(UTC)
    if pending is None or pending.digest_id != digest_id:
        await _best_effort_answer_callback(
            telegram_client,
            callback_query_id,
            "Запрос устарел",
        )
        await _best_effort_send_message(telegram_client, chat_id, _EXPIRED_RESEARCH_MESSAGE)
        return HandlerResult()

    created_at = pending.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    question = pending.question
    await session.delete(pending)
    await session.flush()

    if now - created_at > timedelta(minutes=settings.research_pending_ttl_minutes):
        await _best_effort_answer_callback(
            telegram_client,
            callback_query_id,
            "Запрос устарел",
        )
        await _best_effort_send_message(telegram_client, chat_id, _EXPIRED_RESEARCH_MESSAGE)
        return HandlerResult()

    await _best_effort_answer_callback(
        telegram_client,
        callback_query_id,
        "Ищу в сети…",
    )
    return HandlerResult(
        research=ResearchRequest(
            chat_id=chat_id,
            digest_id=digest_id,
            question=question,
        )
    )


async def _best_effort_answer_callback(
    telegram_client: TelegramBotClient,
    callback_query_id: str,
    text: str,
) -> None:
    try:
        await telegram_client.answer_callback_query(callback_query_id, text)
    except Exception:
        logger.exception("telegram_answer_callback_failed")


async def _best_effort_edit_reply_markup(
    telegram_client: TelegramBotClient,
    chat_id: int,
    message_id: int,
    reply_markup: dict[str, Any],
) -> None:
    try:
        await telegram_client.edit_message_reply_markup(chat_id, message_id, reply_markup)
    except Exception:
        logger.exception("telegram_edit_reply_markup_failed")


async def _best_effort_send_message(
    telegram_client: TelegramBotClient,
    chat_id: int,
    text: str,
) -> None:
    try:
        await telegram_client.send_message(chat_id, text)
    except Exception:
        logger.exception("telegram_send_message_failed")


def _chat_id_from_message(message: dict[str, Any]) -> int | None:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    return chat_id if isinstance(chat_id, int) else None


def _message_id(message: Any) -> int | None:
    if not isinstance(message, dict):
        return None
    message_id = message.get("message_id")
    return message_id if isinstance(message_id, int) else None
