"""Invited-user Telegram onboarding state machine and profile synthesis."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Template
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from delivery.client import TelegramBotClient
from delivery.keyboards import build_onboarding_confirm_keyboard, build_onboarding_keyboard
from delivery.strings import t
from engine.config import Settings
from engine.llm.client import LLMClient
from engine.models import OnboardingState, UIEvent, User
from engine.profile import KeywordRules, Profile

logger = structlog.get_logger(__name__)

_BUTTON_QUESTIONS: dict[int, tuple[str, list[tuple[str, str]]]] = {
    0: (
        "onboarding_q_country",
        [
            ("Poland", "Poland / Польша"),
            ("Ukraine", "Ukraine / Украина"),
            ("other EU", "Other EU / Другая страна ЕС"),
            ("other", "Other / Другое"),
        ],
    ),
    1: (
        "onboarding_q_citizenship",
        [
            ("Ukraine", "Ukraine / Украина"),
            ("Poland", "Poland / Польша"),
            ("other", "Other / Другое"),
        ],
    ),
    2: ("onboarding_q_languages", [("uk", "UA"), ("ru", "RU"), ("pl", "PL"), ("en", "EN")]),
    3: ("onboarding_q_output", [("ru", "RU"), ("uk", "UK"), ("pl", "PL"), ("en", "EN")]),
    4: (
        "onboarding_q_occupation",
        [
            ("IT", "IT"),
            ("finance-trading", "Finance / Trading"),
            ("business-owner", "Business owner"),
            ("student", "Student"),
            ("other", "Other / Другое"),
        ],
    ),
    5: (
        "onboarding_q_legal",
        [
            ("work permit", "Work permit"),
            ("karta pobytu", "Karta pobytu"),
            ("studies", "Studies"),
            ("citizen", "Citizen"),
            ("n-a", "N/A / Пропустить"),
        ],
    ),
}
_TEXT_KEYS = {6: "onboarding_q_wanted", 7: "onboarding_q_unwanted", 8: "onboarding_q_context"}
_ANSWER_KEYS = {
    0: "location",
    1: "citizenship",
    2: "languages",
    3: "output_language",
    4: "occupation",
    5: "pl_legal",
    6: "wanted",
    7: "unwanted",
    8: "context",
}


async def handle_invited_update(
    *,
    session: AsyncSession,
    settings: Settings,
    telegram_client: TelegramBotClient,
    llm_client: LLMClient,
    user: User,
    update: dict[str, Any],
) -> None:
    """Handle one update for a known user whose profile is not yet populated."""

    message = update.get("message")
    if isinstance(message, dict) and message.get("text") == "/start":
        await _start(session, telegram_client, user, message)
        return

    state = await session.get(OnboardingState, user.chat_id)
    if state is None:
        return
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        await _handle_callback(
            session, settings, telegram_client, llm_client, user, state, callback
        )
    elif isinstance(message, dict):
        await _handle_text(session, settings, telegram_client, llm_client, user, state, message)


async def _start(
    session: AsyncSession, client: TelegramBotClient, user: User, message: dict[str, Any]
) -> None:
    assert user.chat_id is not None
    sender = message.get("from")
    first_name = sender.get("first_name") if isinstance(sender, dict) else None
    state = await session.get(OnboardingState, user.chat_id)
    answers = {
        "name": first_name.strip()
        if isinstance(first_name, str) and first_name.strip()
        else user.username
    }
    if state is None:
        session.add(OnboardingState(chat_id=user.chat_id, step=0, answers=answers))
    else:
        state.step, state.answers, state.updated_at = 0, answers, datetime.now(UTC)
    session.add(UIEvent(chat_id=user.chat_id, action="onboarding_started"))
    await session.flush()
    await client.send_message(
        user.chat_id,
        "\n\n".join(
            (t("onboarding_welcome", user.ui_language), t("onboarding_q_country", user.ui_language))
        ),
        reply_markup=_keyboard(0, user.ui_language),
    )


async def _handle_callback(
    session: AsyncSession,
    settings: Settings,
    client: TelegramBotClient,
    llm: LLMClient,
    user: User,
    state: OnboardingState,
    callback: dict[str, Any],
) -> None:
    data = callback.get("data")
    callback_id = callback.get("id")
    if not isinstance(data, str) or not data.startswith("onb:"):
        return
    if data == "onb:confirm:yes" and state.step == 9:
        profile = Profile.model_validate(state.answers["profile"])
        user.profile, user.enabled, user.updated_at = (
            profile.model_dump(mode="json"),
            True,
            datetime.now(UTC),
        )
        session.add(UIEvent(chat_id=state.chat_id, action="onboarding_completed"))
        await session.delete(state)
        await session.flush()
        if isinstance(callback_id, str):
            await client.answer_callback_query(
                callback_id, t("onboarding_confirm", user.ui_language)
            )
        await client.send_message(state.chat_id, t("onboarding_complete", user.ui_language))
        return
    parts = data.split(":", 2)
    if (
        len(parts) != 3
        or not parts[1].isdigit()
        or int(parts[1]) != state.step
        or state.step not in _BUTTON_QUESTIONS
    ):
        return
    value = parts[2]
    allowed = {item[0] for item in _BUTTON_QUESTIONS[state.step][1]}
    if state.step == 2:
        selected = list(state.answers.get("languages", []))
        if value == "done":
            if not selected:
                if isinstance(callback_id, str):
                    await client.answer_callback_query(
                        callback_id, t("onboarding_invalid", user.ui_language)
                    )
                return
            await _advance(session, settings, client, llm, user, state, selected)
        elif value in allowed:
            selected = (
                [item for item in selected if item != value]
                if value in selected
                else [*selected, value]
            )
            state.answers = {**state.answers, "languages": selected}
            state.updated_at = datetime.now(UTC)
            await session.flush()
        return
    if value not in allowed:
        return
    await _advance(session, settings, client, llm, user, state, value)
    if isinstance(callback_id, str):
        await client.answer_callback_query(callback_id, "✓")


async def _handle_text(
    session: AsyncSession,
    settings: Settings,
    client: TelegramBotClient,
    llm: LLMClient,
    user: User,
    state: OnboardingState,
    message: dict[str, Any],
) -> None:
    text = message.get("text")
    if state.step not in _TEXT_KEYS or not isinstance(text, str) or not text.strip():
        return
    await _advance(session, settings, client, llm, user, state, text.strip())


async def _advance(
    session: AsyncSession,
    settings: Settings,
    client: TelegramBotClient,
    llm: LLMClient,
    user: User,
    state: OnboardingState,
    value: Any,
) -> None:
    answered_step = state.step
    state.answers = {**state.answers, _ANSWER_KEYS[answered_step]: value}
    state.step += 1
    state.updated_at = datetime.now(UTC)
    session.add(
        UIEvent(
            chat_id=state.chat_id, action="onboarding_step", context={"step": answered_step + 1}
        )
    )
    await session.flush()
    if state.step <= 5:
        await client.send_message(
            state.chat_id,
            t(_BUTTON_QUESTIONS[state.step][0], user.ui_language),
            reply_markup=_keyboard(state.step, user.ui_language),
        )
    elif state.step <= 8:
        await client.send_message(state.chat_id, t(_TEXT_KEYS[state.step], user.ui_language))
    else:
        profile = await synthesize_profile(settings, llm, state.answers)
        state.answers = {**state.answers, "profile": profile.model_dump(mode="json")}
        state.step = 9
        await session.flush()
        summary = t("onboarding_summary", user.ui_language).format(
            location=escape(profile.location),
            citizenship=escape(profile.citizenship),
            languages=escape(", ".join(profile.languages)),
            output_language=escape(profile.output_language),
            interests=escape(", ".join(profile.interests) or "—"),
            not_interested=escape(", ".join(profile.not_interested) or "—"),
        )
        await client.send_message(
            state.chat_id,
            summary,
            reply_markup=build_onboarding_confirm_keyboard(lang=user.ui_language),
        )


async def synthesize_profile(
    settings: Settings, llm: LLMClient, answers: dict[str, Any]
) -> Profile:
    """Synthesize taste lists, while always enforcing deterministic answers."""

    deterministic = dict(
        name=str(answers["name"]),
        location=str(answers["location"]),
        citizenship=str(answers["citizenship"]),
        languages=list(answers["languages"]),
        output_language=str(answers["output_language"]),
        keyword_rules=KeywordRules(),
    )
    prompt_path = Path(__file__).parents[1] / "engine/llm/prompts/onboarding_profile.j2"
    prompt = Template(prompt_path.read_text(encoding="utf-8")).render(
        answers=json.dumps(answers, ensure_ascii=False)
    )
    try:
        response = await llm.call_structured(
            model=settings.openai_model_summarize,
            system="Synthesize a strict user profile.",
            prompt=prompt,
            output_schema=Profile,
        )
        return Profile.model_validate({**response.output.model_dump(), **deterministic})
    except (ValidationError, RuntimeError, KeyError, TypeError, ValueError):
        logger.exception("onboarding_profile_synthesis_failed", chat_id=None)
        wanted = str(answers.get("wanted", "")).strip()
        unwanted = str(answers.get("unwanted", "")).strip()
        occupation = str(answers.get("occupation", "")).strip()
        return Profile(
            name=str(answers["name"]),
            location=str(answers["location"]),
            citizenship=str(answers["citizenship"]),
            languages=[str(item) for item in answers["languages"]],
            output_language=str(answers["output_language"]),
            interests=[item for item in (wanted, occupation) if item],
            not_interested=[unwanted] if unwanted else [],
            keyword_rules=KeywordRules(),
        )


def _keyboard(step: int, lang: str) -> dict[str, list[list[dict[str, str]]]]:
    return build_onboarding_keyboard(step, _BUTTON_QUESTIONS[step][1], lang=lang, done=step == 2)
