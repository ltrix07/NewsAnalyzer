"""Listener-level regressions for invited-user onboarding."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.listener.handlers import handle_update
from engine.config import get_settings
from engine.models import DigestFeedback, OnboardingState, UIEvent, User
from engine.profile import KeywordRules, Profile


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict[str, Any] | None]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.messages.append((chat_id, text, reply_markup))
        return {"ok": True}

    async def answer_callback_query(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"ok": True}


def _message(chat_id: int, text: str, *, first_name: str = "Ada") -> dict[str, Any]:
    return {
        "message": {
            "chat": {"id": chat_id},
            "from": {"first_name": first_name},
            "text": text,
        }
    }


def _callback(chat_id: int, data: str) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": f"callback-{data}",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": 1},
        }
    }


@pytest.mark.asyncio
async def test_full_onboarding_populates_profile_and_funnel_events(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id = 7001
    user = User(username="ada", chat_id=chat_id, profile=None, enabled=False, ui_language="en")
    db_session.add(user)
    await db_session.flush()
    client = FakeTelegramClient()

    async def fake_synthesize(*_: Any) -> Profile:
        return Profile(
            name="Ada",
            location="Poland",
            citizenship="Ukraine",
            languages=["uk", "en"],
            output_language="en",
            interests=["AI regulation", "Polish immigration law"],
            not_interested=["celebrity gossip"],
            keyword_rules=KeywordRules(),
        )

    monkeypatch.setattr("delivery.onboarding.synthesize_profile", fake_synthesize)

    async def send(update: dict[str, Any]) -> None:
        await handle_update(
            session=db_session,
            settings=get_settings(),
            telegram_client=client,  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update=update,
        )

    await send(_message(chat_id, "/start"))
    for data in (
        "onb:0:Poland",
        "onb:1:Ukraine",
        "onb:2:uk",
        "onb:2:en",
        "onb:2:done",
        "onb:3:en",
        "onb:4:IT",
        "onb:5:karta pobytu",
    ):
        await send(_callback(chat_id, data))
    await send(_message(chat_id, "AI regulation and Polish immigration law"))
    await send(_message(chat_id, "celebrity gossip"))
    await send(_message(chat_id, "I live in Warsaw"))

    state = await db_session.get(OnboardingState, chat_id)
    assert state is not None and state.step == 9
    await send(_callback(chat_id, "onb:confirm:yes"))
    await db_session.flush()

    assert user.enabled is True
    assert user.profile is not None
    assert user.profile["location"] == "Poland"
    assert user.profile["citizenship"] == "Ukraine"
    assert user.profile["languages"] == ["uk", "en"]
    assert user.profile["output_language"] == "en"
    assert user.profile["keyword_rules"] == {"keep_if_matches": [], "drop_if_matches": []}
    assert await db_session.get(OnboardingState, chat_id) is None
    actions = list((await db_session.scalars(select(UIEvent.action))).all())
    assert actions.count("onboarding_started") == 1
    assert actions.count("onboarding_step") == 9
    assert actions.count("onboarding_completed") == 1


@pytest.mark.asyncio
async def test_invited_feedback_is_ignored_and_start_restarts_state(
    db_session: AsyncSession,
) -> None:
    chat_id = 7002
    db_session.add(User(username="beta", chat_id=chat_id, profile=None, enabled=False))
    db_session.add(OnboardingState(chat_id=chat_id, step=6, answers={"stale": True}))
    await db_session.flush()
    client = FakeTelegramClient()

    await handle_update(
        session=db_session,
        settings=get_settings(),
        telegram_client=client,  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=_callback(chat_id, "fb:l:123"),
    )
    assert list((await db_session.scalars(select(DigestFeedback))).all()) == []

    await handle_update(
        session=db_session,
        settings=get_settings(),
        telegram_client=client,  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=_message(chat_id, "/start", first_name="Beta"),
    )
    state = await db_session.get(OnboardingState, chat_id)
    assert state is not None
    assert state.step == 0
    assert state.answers == {"name": "Beta"}
