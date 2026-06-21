"""Dispatch pending digests to Telegram and mark them delivered."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.client import TelegramBotClient
from delivery.formatter import format_digest
from delivery.keyboards import build_digest_keyboard
from engine.config import Settings, get_settings
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
            taste_cosine(centroid, taste)
            if taste is not None and centroid is not None
            else None
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
            "taste_labels": (
                f"{taste.n_like}/{taste.n_dislike}" if taste is not None else None
            ),
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


async def deliver_pending(limit: int | None = None) -> DeliveryReport:
    """Send undelivered digests to Telegram and persist delivery timestamps."""

    settings = get_settings()
    chat_id = settings.require_telegram_chat_id()
    client = _build_client()
    report = DeliveryReport(sent=0, failed=0, skipped=0)

    async with session_scope() as session:
        digests = list((await session.scalars(_pending_digests_query(limit))).all())
        ranked = await _rank_pending_digests(session, digests, settings)
        for item in ranked:
            digest_model = item.digest
            digest = DigestDTO.model_validate(digest_model)
            try:
                message = format_digest(digest)
                await client.send_message(
                    chat_id,
                    message,
                    reply_markup=build_digest_keyboard(digest_model.id),
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
            session.add(
                Impression(
                    digest_id=digest_model.id,
                    event_id=digest_model.event_id,
                    profile_name=digest_model.profile_name,
                    chat_id=chat_id,
                    context=item.context,
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
