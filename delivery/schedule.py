"""Per-user local-time delivery schedule."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from types import MappingProxyType
from zoneinfo import ZoneInfo

from engine.models import User

DELIVERY_SLOTS = MappingProxyType(
    {
        "morning": time(9, 0),
        "day": time(14, 0),
        "evening": time(19, 0),
    }
)
DEFAULT_DELIVERY_SLOT = "morning"


def local_delivery_date(user: User, now: datetime) -> date:
    """Return the user's calendar date at an aware instant."""

    if now.tzinfo is None:
        msg = "delivery time must be timezone-aware"
        raise ValueError(msg)
    return now.astimezone(ZoneInfo(user.timezone)).date()


def is_user_due(user: User, now: datetime) -> bool:
    """Return whether today's local slot has passed without a delivery."""

    if now.tzinfo is None:
        msg = "delivery time must be timezone-aware"
        raise ValueError(msg)
    try:
        slot = DELIVERY_SLOTS[user.delivery_slot]
    except KeyError as exc:
        msg = f"unknown delivery slot: {user.delivery_slot}"
        raise ValueError(msg) from exc
    local_now = now.astimezone(ZoneInfo(user.timezone))
    return user.last_delivery_date != local_now.date() and local_now.time() >= slot


def utc_now() -> datetime:
    """Return the current UTC instant; isolated for deterministic callers."""

    return datetime.now(UTC)
