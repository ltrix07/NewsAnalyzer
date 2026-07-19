"""Tests for local-time delivery slot calculations."""

from datetime import UTC, date, datetime

import pytest

from delivery.schedule import is_user_due, local_delivery_date
from engine.models import User


def _user(
    timezone: str,
    *,
    slot: str = "morning",
    last_delivery_date: date | None = None,
) -> User:
    return User(
        username="scheduled",
        timezone=timezone,
        delivery_slot=slot,
        last_delivery_date=last_delivery_date,
    )


def test_due_then_guarded_same_day_then_due_next_local_day() -> None:
    user = _user("Europe/Warsaw")
    first = datetime(2026, 1, 10, 9, 0, tzinfo=UTC)  # 10:00 local

    assert is_user_due(user, first)
    user.last_delivery_date = local_delivery_date(user, first)
    assert not is_user_due(user, datetime(2026, 1, 10, 20, 0, tzinfo=UTC))
    assert is_user_due(user, datetime(2026, 1, 11, 9, 0, tzinfo=UTC))


def test_missed_tick_self_heals_after_slot() -> None:
    assert is_user_due(_user("Europe/Kyiv"), datetime(2026, 1, 10, 15, 0, tzinfo=UTC))


def test_same_slot_becomes_due_at_different_utc_instants() -> None:
    warsaw = _user("Europe/Warsaw")
    kyiv = _user("Europe/Kyiv")
    at_seven_utc = datetime(2026, 1, 10, 7, 0, tzinfo=UTC)
    at_eight_utc = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)

    assert is_user_due(kyiv, at_seven_utc)
    assert not is_user_due(warsaw, at_seven_utc)
    assert is_user_due(warsaw, at_eight_utc)


@pytest.mark.parametrize(
    ("instant", "expected_due"),
    [
        (datetime(2026, 3, 28, 8, 0, tzinfo=UTC), True),
        (datetime(2026, 3, 29, 7, 0, tzinfo=UTC), True),
        (datetime(2026, 3, 29, 6, 59, tzinfo=UTC), False),
    ],
)
def test_dst_keeps_morning_at_nine_local(instant: datetime, expected_due: bool) -> None:
    assert is_user_due(_user("Europe/Warsaw"), instant) is expected_due


def test_delivery_date_uses_local_date_when_utc_date_differs() -> None:
    instant = datetime(2026, 1, 10, 22, 30, tzinfo=UTC)

    assert local_delivery_date(_user("Europe/Kyiv"), instant) == date(2026, 1, 11)


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_user_due(_user("Europe/Warsaw"), datetime(2026, 1, 10, 9, 0))


def test_invalid_timezone_is_rejected_when_written() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        _user("Mars/Olympus_Mons")
