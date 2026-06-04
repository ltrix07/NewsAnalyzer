"""Dispatch pending digests to Telegram and mark them delivered."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import Select, select

from delivery.client import TelegramBotClient
from delivery.formatter import format_digest
from engine.config import get_settings
from engine.db import session_scope
from engine.domain import Digest as DigestDTO
from engine.models import Digest as DigestModel

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class DeliveryReport:
    sent: int
    failed: int
    skipped: int


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


async def deliver_pending(limit: int | None = None) -> DeliveryReport:
    """Send undelivered digests to Telegram and persist delivery timestamps."""

    settings = get_settings()
    chat_id = settings.require_telegram_chat_id()
    client = _build_client()
    report = DeliveryReport(sent=0, failed=0, skipped=0)

    async with session_scope() as session:
        digests = (await session.scalars(_pending_digests_query(limit))).all()
        for digest_model in digests:
            digest = DigestDTO.model_validate(digest_model)
            try:
                message = format_digest(digest)
                await client.send_message(chat_id, message)
            except Exception:
                report.failed += 1
                logger.exception(
                    "delivery_send_failed",
                    digest_id=digest_model.id,
                    event_id=digest_model.event_id,
                )
                continue

            digest_model.delivered_at = datetime.now(UTC)
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
