"""Long-poll service loop for Telegram feedback and discussion updates."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.client import TelegramBotClient
from delivery.discussion import answer_digest_question
from delivery.keyboards import build_research_keyboard
from delivery.listener.handlers import HandlerResult, handle_update
from delivery.research import SearchClient, research_digest_question
from engine.config import Settings, get_settings
from engine.db import session_scope
from engine.llm.client import LLMClient, make_llm_client
from engine.models import TelegramCursor
from engine.search.tavily import make_tavily_client

logger = structlog.get_logger(__name__)
_INITIAL_FETCH_TIMEOUT_SECONDS = 0
_FETCH_BACKOFF_INITIAL_SECONDS = 2.0
_FETCH_BACKOFF_MAX_SECONDS = 30.0


async def get_cursor(session: AsyncSession) -> int:
    """Return the last fully processed Telegram update id."""

    cursor = await session.get(TelegramCursor, 1)
    return cursor.last_update_id if cursor is not None else -1


async def save_cursor(session: AsyncSession, update_id: int) -> None:
    """Persist the last fully processed Telegram update id."""

    statement = (
        insert(TelegramCursor)
        .values(id=1, last_update_id=update_id)
        .on_conflict_do_update(
            index_elements=[TelegramCursor.id],
            set_={
                "last_update_id": func.greatest(
                    TelegramCursor.last_update_id,
                    update_id,
                )
            },
        )
    )
    await session.execute(statement)
    await session.flush()


async def process_update(
    *,
    settings: Settings,
    telegram_client: TelegramBotClient,
    llm_client: LLMClient,
    search_client: SearchClient | None = None,
    update: dict[str, Any],
) -> None:
    """Process and acknowledge one update, then run post-commit work."""

    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        logger.info("telegram_update_ignored_missing_id")
        return

    async with session_scope() as session:
        result = await handle_update(
            session=session,
            settings=settings,
            telegram_client=telegram_client,
            llm_client=llm_client,
            update=update,
        )
        await save_cursor(session, update_id)

    await _run_post_commit_work(
        settings=settings,
        telegram_client=telegram_client,
        llm_client=llm_client,
        search_client=search_client,
        result=result,
    )


async def mark_update_processed(update_id: int) -> None:
    """Persist a cursor advance for a poisoned update after handler failure."""

    async with session_scope() as session:
        await save_cursor(session, update_id)


async def initialize_cursor_if_missing(
    *,
    telegram_client: TelegramBotClient,
) -> None:
    """Skip pre-existing Telegram backlog on first listener boot."""

    async with session_scope() as session:
        cursor_exists = await session.scalar(
            select(func.count()).select_from(TelegramCursor).where(TelegramCursor.id == 1)
        )
        if cursor_exists:
            return

    payload = await telegram_client.get_updates(
        offset=0,
        timeout=_INITIAL_FETCH_TIMEOUT_SECONDS,
    )
    updates = payload.get("result", [])
    if not isinstance(updates, list) or not updates:
        return

    update_ids = [
        update_id
        for update in updates
        if isinstance(update, dict) and isinstance((update_id := update.get("update_id")), int)
    ]
    if not update_ids:
        return
    latest_update_id = max(update_ids)

    async with session_scope() as session:
        await save_cursor(session, latest_update_id)
    logger.info("telegram_cursor_initialized", update_id=latest_update_id)


async def run_listener() -> None:
    """Run the standalone Telegram long-poll listener until SIGINT/SIGTERM."""

    settings = get_settings()
    telegram_client = TelegramBotClient(settings.require_telegram_token())
    llm_client = make_llm_client(settings)
    # Tavily is optional: without a key the listener still runs feedback and
    # discussion; only the "уточнить в сети" research follow-up is disabled.
    search_client: SearchClient | None = None
    if settings.tavily_api_key is not None:
        search_client = make_tavily_client(settings)
    else:
        logger.warning("tavily_api_key_missing_research_disabled")
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    try:
        await initialize_cursor_if_missing(telegram_client=telegram_client)
    except Exception:
        logger.exception("telegram_cursor_initialization_failed")
    fetch_backoff_seconds = _FETCH_BACKOFF_INITIAL_SECONDS

    while not stop_event.is_set():
        async with session_scope() as session:
            offset = await get_cursor(session) + 1

        payload, fetch_backoff_seconds = await get_updates_with_backoff(
            telegram_client=telegram_client,
            offset=offset,
            timeout=settings.telegram_long_poll_seconds,
            backoff_seconds=fetch_backoff_seconds,
        )
        if payload is None:
            continue

        updates = payload.get("result", [])
        if not isinstance(updates, list):
            logger.warning("telegram_get_updates_result_not_list")
            continue

        for update in updates:
            if not isinstance(update, dict):
                continue
            await process_update_safely(
                settings=settings,
                telegram_client=telegram_client,
                llm_client=llm_client,
                search_client=search_client,
                update=update,
            )
            if stop_event.is_set():
                break


async def process_update_safely(
    *,
    settings: Settings,
    telegram_client: TelegramBotClient,
    llm_client: LLMClient,
    search_client: SearchClient | None = None,
    update: dict[str, Any],
) -> None:
    """Process one update; skip poisoned updates so the daemon survives."""

    try:
        await process_update(
            settings=settings,
            telegram_client=telegram_client,
            llm_client=llm_client,
            search_client=search_client,
            update=update,
        )
    except Exception:
        update_id = update.get("update_id")
        logger.exception("telegram_update_processing_failed", update_id=update_id)
        if isinstance(update_id, int):
            await mark_update_processed(update_id)


async def get_updates_with_backoff(
    *,
    telegram_client: TelegramBotClient,
    offset: int,
    timeout: int,
    backoff_seconds: float,
) -> tuple[dict[str, Any] | None, float]:
    """Fetch Telegram updates, backing off without advancing the cursor on failure."""

    try:
        payload = await telegram_client.get_updates(offset=offset, timeout=timeout)
    except Exception:
        logger.exception("telegram_get_updates_failed", offset=offset)
        await asyncio.sleep(backoff_seconds)
        return None, min(backoff_seconds * 2, _FETCH_BACKOFF_MAX_SECONDS)

    return payload, _FETCH_BACKOFF_INITIAL_SECONDS


async def _run_post_commit_work(
    *,
    settings: Settings,
    telegram_client: TelegramBotClient,
    llm_client: LLMClient,
    search_client: SearchClient | None,
    result: HandlerResult,
) -> None:
    for chat_id, text in result.messages:
        try:
            await telegram_client.send_message(chat_id, text)
        except Exception:
            logger.exception("telegram_post_commit_message_failed", chat_id=chat_id)

    if result.discussion is not None:
        async with session_scope() as session:
            answer = await answer_digest_question(
                session=session,
                settings=settings,
                llm_client=llm_client,
                chat_id=result.discussion.chat_id,
                digest_id=result.discussion.digest_id,
                question=result.discussion.question,
            )

        # The pending row and cursor are already committed, so replay exits before
        # another LLM call. A send failure here is intentionally at-most-once.
        await telegram_client.send_message(
            result.discussion.chat_id,
            answer.text,
            reply_markup=(
                build_research_keyboard(result.discussion.digest_id)
                if answer.offer_research
                else None
            ),
        )

    if result.research is not None:
        if search_client is None:
            logger.error("research_requested_without_search_client")
            return
        async with session_scope() as session:
            chunks = await research_digest_question(
                session=session,
                settings=settings,
                llm_client=llm_client,
                search_client=search_client,
                chat_id=result.research.chat_id,
                digest_id=result.research.digest_id,
                question=result.research.question,
            )
        for chunk in chunks:
            try:
                await telegram_client.send_message(result.research.chat_id, chunk)
            except Exception:
                logger.exception(
                    "telegram_research_message_failed",
                    chat_id=result.research.chat_id,
                )


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame: stop_event.set())
