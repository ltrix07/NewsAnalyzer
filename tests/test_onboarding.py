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
        self.callback_answers: list[tuple[str, str]] = []
        self.markup_edits: list[tuple[int, int, dict[str, Any]]] = []

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

    async def answer_callback_query(self, callback_id: str, text: str) -> dict[str, Any]:
        self.callback_answers.append((callback_id, text))
        return {"ok": True}

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict[str, Any]
    ) -> dict[str, Any]:
        self.markup_edits.append((chat_id, message_id, reply_markup))
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
            residence_country="PL",
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
        "onb:0:yes",
        "onb:1:PL",
        "onb:3:uk",
        "onb:3:en",
        "onb:3:done",
        "onb:4:en",
        "onb:5:IT",
        "onb:6:karta pobytu",
    ):
        await send(_callback(chat_id, data))
    await send(_message(chat_id, "AI regulation and Polish immigration law"))
    await send(_message(chat_id, "celebrity gossip"))
    await send(_message(chat_id, "I live in Warsaw"))

    state = await db_session.get(OnboardingState, chat_id)
    assert state is not None and state.step == 10
    await send(_callback(chat_id, "onb:confirm:yes"))
    await db_session.flush()

    assert user.enabled is True
    assert user.profile is not None
    assert user.profile["location"] == "Poland"
    assert user.profile["residence_country"] == "PL"
    assert user.profile["citizenship"] == "Ukraine"
    assert user.profile["languages"] == ["uk", "en"]
    assert user.profile["output_language"] == "en"
    assert user.profile["keyword_rules"] == {"keep_if_matches": [], "drop_if_matches": []}
    assert await db_session.get(OnboardingState, chat_id) is None
    actions = list((await db_session.scalars(select(UIEvent.action))).all())
    assert actions.count("onboarding_started") == 1
    assert actions.count("onboarding_step") == 9
    assert actions.count("onboarding_completed") == 1
    callback_ids = [callback_id for callback_id, _text in client.callback_answers]
    assert len(callback_ids) == 9
    assert len(callback_ids) == len(set(callback_ids))


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


@pytest.mark.asyncio
async def test_language_toggle_acknowledges_and_repaints_selection(
    db_session: AsyncSession,
) -> None:
    chat_id = 7003
    db_session.add(User(username="toggle", chat_id=chat_id, profile=None, enabled=False))
    db_session.add(
        OnboardingState(
            chat_id=chat_id,
            step=3,
            answers={
                "name": "Toggle",
                "citizenship": "Ukraine",
                "location": "Poland",
                "residence_country": "PL",
            },
        )
    )
    await db_session.flush()
    client = FakeTelegramClient()

    for _ in range(2):
        await handle_update(
            session=db_session,
            settings=get_settings(),
            telegram_client=client,  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update=_callback(chat_id, "onb:3:uk"),
        )

    assert len(client.callback_answers) == 2
    assert len(client.markup_edits) == 2
    first_labels = [row[0]["text"] for row in client.markup_edits[0][2]["inline_keyboard"]]
    second_labels = [row[0]["text"] for row in client.markup_edits[1][2]["inline_keyboard"]]
    assert "✅ UA" in first_labels
    assert "UA" in second_labels and "✅ UA" not in second_labels


@pytest.mark.asyncio
async def test_failed_language_repaint_still_acknowledges_callback(
    db_session: AsyncSession,
) -> None:
    class FailingRepaintClient(FakeTelegramClient):
        async def edit_message_reply_markup(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("message is not modified")

    chat_id = 7004
    db_session.add(User(username="repaint", chat_id=chat_id, profile=None, enabled=False))
    db_session.add(OnboardingState(chat_id=chat_id, step=3, answers={"languages": []}))
    await db_session.flush()
    client = FailingRepaintClient()

    await handle_update(
        session=db_session,
        settings=get_settings(),
        telegram_client=client,  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=_callback(chat_id, "onb:3:uk"),
    )

    assert client.callback_answers == [("callback-onb:3:uk", "✓")]


@pytest.mark.asyncio
async def test_stale_callback_is_acknowledged_without_changing_step(
    db_session: AsyncSession,
) -> None:
    chat_id = 7005
    db_session.add(User(username="stale", chat_id=chat_id, profile=None, enabled=False))
    db_session.add(OnboardingState(chat_id=chat_id, step=3, answers={}))
    await db_session.flush()
    client = FakeTelegramClient()

    await handle_update(
        session=db_session,
        settings=get_settings(),
        telegram_client=client,  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=_callback(chat_id, "onb:1:PL"),
    )

    state = await db_session.get(OnboardingState, chat_id)
    assert state is not None and state.step == 3
    assert client.callback_answers[0][1] == "Этот шаг уже пройден."


@pytest.mark.asyncio
async def test_citizenship_gate_rejects_without_enabling_user(
    db_session: AsyncSession,
) -> None:
    chat_id = 7006
    user = User(username="ineligible", chat_id=chat_id, profile=None, enabled=False)
    db_session.add(user)
    await db_session.flush()
    client = FakeTelegramClient()

    await handle_update(
        session=db_session,
        settings=get_settings(),
        telegram_client=client,  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=_message(chat_id, "/start"),
    )
    await handle_update(
        session=db_session,
        settings=get_settings(),
        telegram_client=client,  # type: ignore[arg-type]
        llm_client=object(),  # type: ignore[arg-type]
        update=_callback(chat_id, "onb:0:no"),
    )

    assert user.profile is None and user.enabled is False
    assert await db_session.get(OnboardingState, chat_id) is None
    actions = list((await db_session.scalars(select(UIEvent.action))).all())
    assert "onboarding_ineligible" in actions


@pytest.mark.asyncio
async def test_other_country_stores_typed_value_and_unknown_code(
    db_session: AsyncSession,
) -> None:
    chat_id = 7007
    db_session.add(User(username="other_country", chat_id=chat_id, profile=None, enabled=False))
    db_session.add(
        OnboardingState(
            chat_id=chat_id,
            step=1,
            answers={"name": "Other", "citizenship": "Ukraine"},
        )
    )
    await db_session.flush()
    client = FakeTelegramClient()

    for update in (_callback(chat_id, "onb:1:other"), _message(chat_id, "Portugal")):
        await handle_update(
            session=db_session,
            settings=get_settings(),
            telegram_client=client,  # type: ignore[arg-type]
            llm_client=object(),  # type: ignore[arg-type]
            update=update,
        )

    state = await db_session.get(OnboardingState, chat_id)
    assert state is not None
    assert state.answers["location"] == "Portugal"
    assert state.answers["location"] != "other"
    assert state.answers["residence_country"] == "ZZ"
    assert state.step == 3
    assert any("Новости Украины" in text for _chat, text, _markup in client.messages)


@pytest.mark.asyncio
async def test_spain_skips_legal_status_and_synthesizes_correct_profile(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id = 7008
    db_session.add(
        User(
            username="spain",
            chat_id=chat_id,
            profile=None,
            enabled=False,
            ui_language="en",
        )
    )
    await db_session.flush()
    client = FakeTelegramClient()

    async def fake_synthesize(_settings: Any, _llm: Any, answers: dict[str, Any]) -> Profile:
        return Profile(
            name=str(answers["name"]),
            location=str(answers["location"]),
            residence_country=str(answers["residence_country"]),
            citizenship=str(answers["citizenship"]),
            languages=list(answers["languages"]),
            output_language=str(answers["output_language"]),
            interests=[str(answers["wanted"])],
            not_interested=[str(answers["unwanted"])],
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
        "onb:0:yes",
        "onb:1:ES",
        "onb:3:uk",
        "onb:3:done",
        "onb:4:uk",
        "onb:5:student",
    ):
        await send(_callback(chat_id, data))
    state = await db_session.get(OnboardingState, chat_id)
    assert state is not None and state.step == 7
    await send(_message(chat_id, "Spanish immigration rules"))
    await send(_message(chat_id, "sports"))
    await send(_message(chat_id, "Madrid"))

    assert state.step == 10
    assert "legal_status" not in state.answers
    assert state.answers["profile"]["location"] == "Spain"
    assert state.answers["profile"]["residence_country"] == "ES"
    assert state.answers["profile"]["interests"] == ["Spanish immigration rules"]
