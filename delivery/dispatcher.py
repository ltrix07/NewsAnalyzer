"""Dispatch pending digests to Telegram and mark them delivered."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.client import TelegramBotClient
from delivery.formatter import format_digest
from delivery.keyboards import build_digest_keyboard
from delivery.strings import t
from engine.config import Settings, get_settings
from engine.consolidation_match import Adjudicator, default_adjudicator
from engine.db import session_scope
from engine.domain import Digest as DigestDTO
from engine.models import Digest as DigestModel
from engine.models import Event as EventModel
from engine.models import Impression
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
        for item in ranked:
            digest_model = item.digest
            digest = DigestDTO.model_validate(digest_model)
            parent: ThreadParent | None = None
            try:
                message = format_digest(digest)
                if settings.thread_updates_enabled:
                    # Threading is best-effort: a failure in parent detection
                    # (e.g. the LLM adjudicator is down or rate-limited) must
                    # degrade to a normal top-level send, never block delivery.
                    try:
                        parent = await _find_thread_parent(
                            session,
                            digest_model,
                            settings,
                            resolved_adjudicator,
                        )
                    except Exception:
                        logger.warning(
                            "thread_parent_detection_failed",
                            digest_id=digest_model.id,
                        )
                        parent = None

                if parent is not None:
                    header = (
                        f"{t('thread_update_header', settings.ui_language)}"
                        f"{html.escape(parent.headline)}\n\n"
                    )
                    try:
                        response = await resolved_client.send_message(
                            chat_id,
                            header + message,
                            reply_markup=build_digest_keyboard(
                                digest_model.id,
                                lang=settings.ui_language,
                            ),
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
                        response = await resolved_client.send_message(
                            chat_id,
                            message,
                            reply_markup=build_digest_keyboard(
                                digest_model.id,
                                lang=settings.ui_language,
                            ),
                        )
                else:
                    response = await resolved_client.send_message(
                        chat_id,
                        message,
                        reply_markup=build_digest_keyboard(
                            digest_model.id,
                            lang=settings.ui_language,
                        ),
                    )
            except Exception:
                report.failed += 1
                logger.exception(
                    "delivery_send_failed",
                    digest_id=digest_model.id,
                    event_id=digest_model.event_id,
                )
                continue

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
            await session.flush()
            report.sent += 1

    return report


async def send_test_message() -> None:
    """Send a connectivity probe message to the configured Telegram chat."""

    settings = get_settings()
    client = _build_client()
    await client.send_message(
        settings.require_telegram_chat_id(),
        "Hello from your news bot. Connected.",
    )
