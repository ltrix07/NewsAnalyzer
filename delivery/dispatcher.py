"""Dispatch pending digests to Telegram and mark them delivered."""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.client import TelegramBotClient
from delivery.formatter import format_digest
from delivery.keyboards import (
    build_digest_keyboard,
    build_reveal_keyboard,
    build_reveal_more_keyboard,
)
from delivery.strings import t
from engine.config import Settings, get_settings
from engine.consolidation_match import Adjudicator, default_adjudicator
from engine.db import session_scope
from engine.domain import Digest as DigestDTO
from engine.models import DeliveryBatch, DigestLink, Impression
from engine.models import Digest as DigestModel
from engine.models import Event as EventModel
from engine.ranking.taste import (
    blend_score,
    build_taste_vector,
    is_major,
    significance_score,
    taste_cosine,
)

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class DeliveryReport:
    sent: int
    failed: int
    skipped: int


@dataclass(slots=True)
class RankedDigest:
    """A pending digest with its computed ranking score and audit context."""

    digest: DigestModel
    context: dict[str, Any] | None


@dataclass(slots=True)
class ThreadParent:
    """Delivered digest message selected as the reply parent for an update."""

    digest_id: int
    headline: str
    telegram_message_id: int


def _build_client() -> TelegramBotClient:
    settings = get_settings()
    return TelegramBotClient(settings.require_telegram_token())


def _pending_digests_query(limit: int | None = None) -> Select[tuple[DigestModel]]:
    statement = (
        select(DigestModel)
        .where(DigestModel.delivered_at.is_(None))
        .order_by(DigestModel.created_at.asc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return statement


async def _rank_pending_digests(
    session: AsyncSession,
    digests: list[DigestModel],
    settings: Settings,
) -> list[RankedDigest]:
    """Order pending digests by blended taste + significance (major tier first).

    Falls back to chronological order when taste ranking is disabled. The taste
    vector itself may be ``None`` (cold start) — significance still orders the
    batch, with a "major" tier floor so confident / multi-source events lead.
    """

    if not settings.taste_ranking_enabled or not digests:
        return [RankedDigest(digest=digest, context=None) for digest in digests]

    taste = await build_taste_vector(
        session,
        min_labels_per_class=settings.taste_min_labels_per_class,
    )
    event_ids = {digest.event_id for digest in digests}
    event_rows = (
        await session.execute(
            select(EventModel.id, EventModel.centroid, EventModel.article_count).where(
                EventModel.id.in_(event_ids)
            )
        )
    ).all()
    events = {row[0]: (row[1], row[2]) for row in event_rows}

    scored: list[tuple[int, float, datetime, int, RankedDigest]] = []
    for digest in digests:
        centroid, article_count = events.get(digest.event_id, (None, 0))
        taste_cos = (
            taste_cosine(centroid, taste) if taste is not None and centroid is not None else None
        )
        significance = significance_score(digest.confidence_level, article_count)
        major = is_major(digest.confidence_level, article_count)
        blend = blend_score(
            taste_cos,
            significance,
            taste_weight=settings.taste_weight,
            significance_weight=settings.significance_weight,
        )
        context = {
            "ranking_version": "v1",
            "taste_cosine": taste_cos,
            "significance": significance,
            "major": major,
            "blend": blend,
            "taste_labels": (f"{taste.n_like}/{taste.n_dislike}" if taste is not None else None),
        }
        scored.append(
            (
                1 if major else 0,
                blend,
                digest.created_at,
                digest.id,
                RankedDigest(digest=digest, context=context),
            )
        )

    # Major tier first, then blended score (both descending), with a stable
    # chronological tie-break so equal-score digests keep deterministic order.
    scored.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return [item[4] for item in scored]


async def _find_thread_parent(
    session: AsyncSession,
    digest_model: DigestModel,
    settings: Settings,
    adjudicator: Adjudicator,
) -> ThreadParent | None:
    """Find the most recent delivered same-story message to reply to."""

    current_event = await session.get(EventModel, digest_model.event_id)
    if current_event is None:
        return None

    vector = [float(value) for value in current_event.centroid]
    distance_expr = EventModel.centroid.cosine_distance(vector).label("distance")
    cutoff = datetime.now(UTC) - timedelta(hours=settings.thread_window_hours)
    rows = (
        await session.execute(
            select(DigestModel, EventModel, distance_expr)
            .join(EventModel, EventModel.id == DigestModel.event_id)
            .where(
                DigestModel.delivered_at.is_not(None),
                DigestModel.delivered_at >= cutoff,
                DigestModel.telegram_message_id.is_not(None),
                DigestModel.event_id != digest_model.event_id,
            )
            .order_by(distance_expr)
            .limit(settings.thread_max_candidates)
        )
    ).all()
    candidates = [
        (candidate_digest, candidate_event)
        for candidate_digest, candidate_event, distance in rows
        if 1.0 - float(distance) >= settings.thread_min_similarity
        and candidate_digest.telegram_message_id is not None
    ]
    candidates.sort(
        key=lambda item: item[0].delivered_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    for candidate_digest, candidate_event in candidates:
        judgement = await adjudicator(session, current_event, candidate_event)
        if judgement.same_event:
            return ThreadParent(
                digest_id=candidate_digest.id,
                headline=candidate_digest.headline,
                telegram_message_id=candidate_digest.telegram_message_id,
            )

    return None


def _message_id_from_response(response: dict[str, Any]) -> int | None:
    result = response.get("result")
    if not isinstance(result, dict):
        return None

    message_id = result.get("message_id")
    return int(message_id) if isinstance(message_id, int) else None


def _is_reply_target_missing(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "message to be replied not found" in message or "message to reply not found" in message


def _impression_context(
    ranking_context: dict[str, Any] | None,
    *,
    threaded_parent_digest_id: int | None,
) -> dict[str, Any]:
    context = dict(ranking_context or {})
    context["threaded_parent_digest_id"] = threaded_parent_digest_id
    return context


async def _mint_digest_links(digest: DigestDTO, chat_id: int) -> dict[int, str]:
    """Mint or reuse all citation tokens in their own committed transaction.

    Deliberately runs in a session of its own: tokens must be durable before the
    message embedding them is sent, and a minting failure must not poison the
    delivery session (a rollback there would expire the loaded digest rows).
    """

    if not digest.citations:
        return {}
    values = [
        {
            "token": secrets.token_urlsafe(16),
            "digest_id": digest.id,
            "chat_id": chat_id,
            "citation_index": index,
            "url": citation.url,
            "source": citation.source,
        }
        for index, citation in enumerate(digest.citations)
    ]
    insert_statement = insert(DigestLink).values(values)
    statement = insert_statement.on_conflict_do_update(
        index_elements=["digest_id", "chat_id", "citation_index"],
        set_={"token": DigestLink.token},
    ).returning(DigestLink.citation_index, DigestLink.token)
    async with session_scope() as link_session:
        rows = (await link_session.execute(statement)).all()
    return {citation_index: token for citation_index, token in rows}


async def reveal_digest(
    *,
    session: AsyncSession,
    client: TelegramBotClient,
    settings: Settings,
    chat_id: int,
    item: RankedDigest,
    parent: ThreadParent | None = None,
) -> None:
    """Send one digest and record the reveal in the caller's transaction."""

    digest_model = item.digest
    digest = DigestDTO.model_validate(digest_model)
    link_urls: dict[int, str] | None = None
    if settings.link_tracking_enabled:
        base_url = settings.require_redirect_base_url()
        try:
            tokens = await _mint_digest_links(digest, chat_id)
            link_urls = {index: f"{base_url}/r/{token}" for index, token in tokens.items()}
        except Exception:
            logger.warning("link_minting_failed", digest_id=digest_model.id)

    message = await format_digest(digest, session, link_urls)
    reply_markup = build_digest_keyboard(digest_model.id, lang=settings.ui_language)
    if parent is not None:
        header = (
            f"{t('thread_update_header', settings.ui_language)}{html.escape(parent.headline)}\n\n"
        )
        try:
            response = await client.send_message(
                chat_id,
                header + message,
                reply_markup=reply_markup,
                reply_to_message_id=parent.telegram_message_id,
                disable_notification=True,
            )
        except Exception as exc:
            if not _is_reply_target_missing(exc):
                raise
            logger.warning(
                "delivery_reply_parent_missing",
                digest_id=digest_model.id,
                parent_digest_id=parent.digest_id,
                parent_message_id=parent.telegram_message_id,
            )
            parent = None
            response = await client.send_message(chat_id, message, reply_markup=reply_markup)
    else:
        response = await client.send_message(chat_id, message, reply_markup=reply_markup)

    digest_model.delivered_at = datetime.now(UTC)
    digest_model.telegram_message_id = _message_id_from_response(response)
    session.add(
        Impression(
            digest_id=digest_model.id,
            event_id=digest_model.event_id,
            profile_name=digest_model.profile_name,
            chat_id=chat_id,
            context=_impression_context(
                item.context,
                threaded_parent_digest_id=parent.digest_id if parent else None,
            ),
        )
    )


async def _upsert_batch_notification(
    client: TelegramBotClient,
    batch: DeliveryBatch,
    count: int,
    settings: Settings,
) -> None:
    text = t("batch_notification", settings.ui_language).format(count=count)
    keyboard = build_reveal_keyboard(batch.id, lang=settings.ui_language)
    try:
        if batch.notification_message_id is None:
            response = await client.send_message(batch.chat_id, text, reply_markup=keyboard)
            batch.notification_message_id = _message_id_from_response(response)
            batch.notified_at = datetime.now(UTC)
        else:
            await client.edit_message_text(
                batch.chat_id, batch.notification_message_id, text, reply_markup=keyboard
            )
    except Exception:
        logger.exception("batch_notification_failed", batch_id=batch.id)


async def _maybe_nudge_batch(
    session: AsyncSession,
    client: TelegramBotClient,
    batch: DeliveryBatch,
    settings: Settings,
) -> None:
    if batch.opened_at is not None or batch.notified_at is None:
        return
    now = datetime.now(UTC)
    interval = timedelta(days=settings.batch_nudge_after_days)
    if now - batch.notified_at < interval:
        return
    if batch.last_nudge_at is not None and now - batch.last_nudge_at < interval:
        return
    count = await session.scalar(
        select(func.count())
        .select_from(DigestModel)
        .where(DigestModel.batch_id == batch.id, DigestModel.delivered_at.is_(None))
    )
    try:
        await client.send_message(
            batch.chat_id,
            t("batch_nudge", settings.ui_language).format(count=count or 0),
        )
        batch.last_nudge_at = now
    except Exception:
        logger.exception("batch_nudge_failed", batch_id=batch.id)


async def reveal_batch_page(
    *,
    batch_id: int,
    chat_id: int,
    client: TelegramBotClient,
    settings: Settings,
) -> None:
    """Reveal one ranked page after the callback transaction has committed."""

    async with session_scope() as session:
        batch = await session.get(DeliveryBatch, batch_id)
        if batch is None or batch.chat_id != chat_id or batch.closed_at is not None:
            return
        digests = list(
            (
                await session.scalars(
                    select(DigestModel).where(
                        DigestModel.batch_id == batch_id,
                        DigestModel.delivered_at.is_(None),
                    )
                )
            ).all()
        )
        ranked = await _rank_pending_digests(session, digests, settings)
        for item in ranked[: settings.batch_reveal_page_size]:
            try:
                await reveal_digest(
                    session=session,
                    client=client,
                    settings=settings,
                    chat_id=chat_id,
                    item=item,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("batch_reveal_failed", batch_id=batch_id, digest_id=item.digest.id)

        remaining = await session.scalar(
            select(func.count())
            .select_from(DigestModel)
            .where(DigestModel.batch_id == batch_id, DigestModel.delivered_at.is_(None))
        )
        if remaining:
            try:
                await client.send_message(
                    chat_id,
                    t("btn_show_more", settings.ui_language).format(count=remaining),
                    reply_markup=build_reveal_more_keyboard(
                        batch_id, remaining, lang=settings.ui_language
                    ),
                )
            except Exception:
                logger.exception("batch_reveal_more_failed", batch_id=batch_id)
            return

        batch.closed_at = datetime.now(UTC)
        await session.commit()
        if batch.notification_message_id is not None:
            try:
                await client.edit_message_text(
                    chat_id,
                    batch.notification_message_id,
                    t("batch_all_shown", settings.ui_language),
                )
            except Exception:
                logger.exception("batch_terminal_edit_failed", batch_id=batch_id)


async def deliver_pending(
    limit: int | None = None,
    *,
    client: TelegramBotClient | None = None,
    adjudicator: Adjudicator | None = None,
) -> DeliveryReport:
    """Send undelivered digests to Telegram and persist delivery timestamps."""

    settings = get_settings()
    chat_id = settings.require_telegram_chat_id()
    resolved_client = client or _build_client()
    resolved_adjudicator = adjudicator or default_adjudicator(settings)
    report = DeliveryReport(sent=0, failed=0, skipped=0)

    async with session_scope() as session:
        digests = list((await session.scalars(_pending_digests_query(limit))).all())
        ranked = await _rank_pending_digests(session, digests, settings)
        open_batch: DeliveryBatch | None = None
        batch_gained = False
        if settings.batched_delivery_enabled:
            open_batch = await session.scalar(
                select(DeliveryBatch).where(
                    DeliveryBatch.chat_id == chat_id, DeliveryBatch.closed_at.is_(None)
                )
            )

        for item in ranked:
            digest_model = item.digest
            if settings.batched_delivery_enabled and digest_model.batch_id is not None:
                continue
            parent: ThreadParent | None = None
            try:
                if settings.thread_updates_enabled:
                    try:
                        parent = await _find_thread_parent(
                            session,
                            digest_model,
                            settings,
                            resolved_adjudicator,
                        )
                    except Exception:
                        logger.warning("thread_parent_detection_failed", digest_id=digest_model.id)

                if settings.batched_delivery_enabled and parent is None:
                    if open_batch is None:
                        open_batch = DeliveryBatch(chat_id=chat_id)
                        session.add(open_batch)
                        await session.flush()
                    digest_model.batch_id = open_batch.id
                    batch_gained = True
                    continue

                await reveal_digest(
                    session=session,
                    client=resolved_client,
                    settings=settings,
                    chat_id=chat_id,
                    item=item,
                    parent=parent,
                )
            except Exception:
                report.failed += 1
                logger.exception(
                    "delivery_send_failed",
                    digest_id=digest_model.id,
                    event_id=digest_model.event_id,
                )
                continue
            await session.commit()
            report.sent += 1

        if settings.batched_delivery_enabled and open_batch is not None:
            if batch_gained or open_batch.notification_message_id is None:
                count = await session.scalar(
                    select(func.count())
                    .select_from(DigestModel)
                    .where(
                        DigestModel.batch_id == open_batch.id,
                        DigestModel.delivered_at.is_(None),
                    )
                )
                await _upsert_batch_notification(resolved_client, open_batch, count or 0, settings)
            await _maybe_nudge_batch(session, resolved_client, open_batch, settings)
            await session.commit()

    return report


async def send_test_message() -> None:
    """Send a connectivity probe message to the configured Telegram chat."""

    settings = get_settings()
    client = _build_client()
    await client.send_message(
        settings.require_telegram_chat_id(),
        "Hello from your news bot. Connected.",
    )
