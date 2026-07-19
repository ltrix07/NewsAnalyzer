"""Invited-user Telegram onboarding state machine and profile synthesis."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog
import yaml  # type: ignore[import-untyped]
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

QuestionKind = Literal["single", "multi", "text"]
OptionsFactory = Callable[[dict[str, Any], str], list[tuple[str, str]]]
Predicate = Callable[[dict[str, Any]], bool]
AnswerStore = Callable[[dict[str, Any], Any, str], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Question:
    answer_key: str
    kind: QuestionKind
    prompt_key: str
    options: OptionsFactory | None = None
    applies: Predicate = lambda _answers: True
    store: AnswerStore | None = None
    ineligible_value: str | None = None
    completes_residence: bool = False


def _static_options(options: list[tuple[str, str]]) -> OptionsFactory:
    return lambda _answers, _lang: options


@lru_cache(maxsize=1)
def _countries() -> dict[str, dict[str, Any]]:
    path = Path(__file__).parents[1] / "config/countries.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    countries = payload.get("countries") if isinstance(payload, dict) else None
    if not isinstance(countries, dict):
        msg = f"{path} must contain a countries mapping"
        raise RuntimeError(msg)
    return countries


def _country_options(_answers: dict[str, Any], lang: str) -> list[tuple[str, str]]:
    options = []
    for code, data in _countries().items():
        labels = data.get("labels", {})
        options.append((code, str(labels.get(lang, labels.get("ru", code)))))
    return [*options, ("other", t("onboarding_other_country", lang))]


def _gate_options(_answers: dict[str, Any], lang: str) -> list[tuple[str, str]]:
    return [("yes", t("onboarding_yes", lang)), ("no", t("onboarding_no", lang))]


def _legal_options(answers: dict[str, Any], _lang: str) -> list[tuple[str, str]]:
    country = _countries().get(str(answers.get("residence_country")), {})
    return [(str(value), str(label)) for value, label in country.get("legal_status_options", [])]


def _has_legal_options(answers: dict[str, Any]) -> bool:
    return bool(_legal_options(answers, "ru"))


@lru_cache(maxsize=1)
def _source_countries() -> frozenset[str]:
    path = Path(__file__).parents[1] / "config/sources.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        msg = f"{path} must contain a sources list"
        raise RuntimeError(msg)
    return frozenset(
        str(source["country"])
        for source in sources
        if isinstance(source, dict) and source.get("enabled") is True and source.get("country")
    )


def _has_residence_coverage(answers: dict[str, Any]) -> bool:
    return str(answers.get("residence_country")) in _source_countries()


def _store_answer(key: str) -> AnswerStore:
    def store(answers: dict[str, Any], value: Any, _lang: str) -> dict[str, Any]:
        return {**answers, key: value}

    return store


def _store_ukrainian_citizenship(
    answers: dict[str, Any], _value: Any, _lang: str
) -> dict[str, Any]:
    return {**answers, "citizenship": "Ukraine"}


def _store_residence_choice(answers: dict[str, Any], value: Any, lang: str) -> dict[str, Any]:
    updated = {**answers, "residence_choice": value}
    if value == "other":
        return updated
    labels = _countries()[str(value)]["labels"]
    return {
        **updated,
        "residence_country": value,
        "location": labels.get(lang, labels["ru"]),
    }


def _store_free_text_residence(answers: dict[str, Any], value: Any, _lang: str) -> dict[str, Any]:
    return {
        **answers,
        "location": value,
        "residence_country": _resolve_country_code(str(value)),
    }


QUESTIONS = [
    Question(
        "ukrainian_citizen",
        "single",
        "onboarding_q_ua_citizen",
        _gate_options,
        store=_store_ukrainian_citizenship,
        ineligible_value="no",
    ),
    Question(
        "residence_choice",
        "single",
        "onboarding_q_country",
        _country_options,
        store=_store_residence_choice,
        completes_residence=True,
    ),
    Question(
        "location",
        "text",
        "onboarding_q_country_other",
        applies=lambda answers: answers.get("residence_choice") == "other",
        store=_store_free_text_residence,
        completes_residence=True,
    ),
    Question(
        "languages",
        "multi",
        "onboarding_q_languages",
        _static_options([("uk", "UA"), ("ru", "RU"), ("pl", "PL"), ("en", "EN")]),
    ),
    Question(
        "output_language",
        "single",
        "onboarding_q_output",
        _static_options([("ru", "RU"), ("uk", "UK"), ("pl", "PL"), ("en", "EN")]),
    ),
    Question(
        "occupation",
        "single",
        "onboarding_q_occupation",
        _static_options(
            [
                ("IT", "IT"),
                ("finance-trading", "Finance / Trading"),
                ("business-owner", "Business owner"),
                ("student", "Student"),
                ("other", "Other / Другое"),
            ]
        ),
    ),
    Question("legal_status", "single", "onboarding_q_legal", _legal_options, _has_legal_options),
    Question("wanted", "text", "onboarding_q_wanted"),
    Question("unwanted", "text", "onboarding_q_unwanted"),
    Question("context", "text", "onboarding_q_context"),
]


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
            (
                t("onboarding_welcome", user.ui_language),
                t(QUESTIONS[0].prompt_key, user.ui_language),
            )
        ),
        reply_markup=_keyboard(0, answers, user.ui_language),
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
    acknowledgement = "✓"
    try:
        if data == "onb:confirm:yes" and state.step == len(QUESTIONS):
            profile = Profile.model_validate(state.answers["profile"])
            user.profile, user.enabled, user.updated_at = (
                profile.model_dump(mode="json"),
                True,
                datetime.now(UTC),
            )
            session.add(UIEvent(chat_id=state.chat_id, action="onboarding_completed"))
            await session.delete(state)
            await session.flush()
            acknowledgement = t("onboarding_confirm", user.ui_language)
            await client.send_message(state.chat_id, t("onboarding_complete", user.ui_language))
            return

        parts = data.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) != state.step:
            acknowledgement = t("onboarding_stale", user.ui_language)
            return
        if state.step >= len(QUESTIONS):
            acknowledgement = t("onboarding_invalid", user.ui_language)
            return
        question = QUESTIONS[state.step]
        if question.kind == "text" or question.options is None:
            acknowledgement = t("onboarding_invalid", user.ui_language)
            return
        value = parts[2]
        allowed = {item[0] for item in question.options(state.answers, user.ui_language)}
        if question.kind == "multi":
            selected = list(state.answers.get(question.answer_key, []))
            if value == "done":
                if not selected:
                    acknowledgement = t("onboarding_invalid", user.ui_language)
                    return
                await _advance(session, settings, client, llm, user, state, selected)
                return
            if value not in allowed:
                acknowledgement = t("onboarding_invalid", user.ui_language)
                return
            selected = (
                [item for item in selected if item != value]
                if value in selected
                else [*selected, value]
            )
            state.answers = {**state.answers, question.answer_key: selected}
            state.updated_at = datetime.now(UTC)
            await session.flush()
            message = callback.get("message")
            message_id = message.get("message_id") if isinstance(message, dict) else None
            if isinstance(message_id, int):
                try:
                    await client.edit_message_reply_markup(
                        state.chat_id,
                        message_id,
                        _keyboard(state.step, state.answers, user.ui_language, selected=selected),
                    )
                except (httpx.HTTPError, RuntimeError):
                    logger.warning("onboarding_keyboard_repaint_failed", chat_id=state.chat_id)
            return
        if value not in allowed:
            acknowledgement = t("onboarding_invalid", user.ui_language)
            return
        if question.ineligible_value is not None and value == question.ineligible_value:
            session.add(UIEvent(chat_id=state.chat_id, action="onboarding_ineligible"))
            await session.delete(state)
            await session.flush()
            await client.send_message(state.chat_id, t("onboarding_ineligible", user.ui_language))
            return
        await _advance(session, settings, client, llm, user, state, value)
    finally:
        if isinstance(callback_id, str):
            await client.answer_callback_query(callback_id, acknowledgement)


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
    if (
        state.step >= len(QUESTIONS)
        or QUESTIONS[state.step].kind != "text"
        or not isinstance(text, str)
        or not text.strip()
    ):
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
    question = QUESTIONS[answered_step]
    store = question.store or _store_answer(question.answer_key)
    answers = store(dict(state.answers), value, user.ui_language)
    state.answers = answers
    state.step = _next_applicable(answered_step + 1, answers)
    state.updated_at = datetime.now(UTC)
    session.add(
        UIEvent(
            chat_id=state.chat_id,
            action="onboarding_step",
            context={"step": answered_step + 1, "answer_key": question.answer_key},
        )
    )
    await session.flush()
    residence_was_completed = question.completes_residence and "residence_country" in answers
    if residence_was_completed and not _has_residence_coverage(answers):
        await client.send_message(
            state.chat_id, t("onboarding_unsupported_country", user.ui_language)
        )
    await _send_current_question(session, settings, client, llm, user, state)


def _next_applicable(step: int, answers: dict[str, Any]) -> int:
    while step < len(QUESTIONS) and not QUESTIONS[step].applies(answers):
        step += 1
    return step


async def _send_current_question(
    session: AsyncSession,
    settings: Settings,
    client: TelegramBotClient,
    llm: LLMClient,
    user: User,
    state: OnboardingState,
) -> None:
    if state.step < len(QUESTIONS):
        question = QUESTIONS[state.step]
        markup = (
            _keyboard(state.step, state.answers, user.ui_language)
            if question.kind != "text"
            else None
        )
        await client.send_message(
            state.chat_id, t(question.prompt_key, user.ui_language), reply_markup=markup
        )
        return
    profile = await synthesize_profile(settings, llm, state.answers)
    state.answers = {**state.answers, "profile": profile.model_dump(mode="json")}
    state.step = len(QUESTIONS)
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


def _resolve_country_code(location: str) -> str:
    normalized = location.strip().casefold()
    for code, data in _countries().items():
        aliases = [str(item).casefold() for item in data.get("aliases", [])]
        labels = [str(item).casefold() for item in data.get("labels", {}).values()]
        if normalized == code.casefold() or normalized in aliases or normalized in labels:
            return code
    return "ZZ"


async def synthesize_profile(
    settings: Settings, llm: LLMClient, answers: dict[str, Any]
) -> Profile:
    """Synthesize taste lists, while always enforcing deterministic answers."""

    deterministic = dict(
        name=str(answers["name"]),
        location=str(answers["location"]),
        residence_country=str(answers["residence_country"]),
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
            residence_country=str(answers["residence_country"]),
            citizenship=str(answers["citizenship"]),
            languages=[str(item) for item in answers["languages"]],
            output_language=str(answers["output_language"]),
            interests=[item for item in (wanted, occupation) if item],
            not_interested=[unwanted] if unwanted else [],
            keyword_rules=KeywordRules(),
        )


def _keyboard(
    step: int,
    answers: dict[str, Any],
    lang: str,
    *,
    selected: list[str] | None = None,
) -> dict[str, list[list[dict[str, str]]]]:
    question = QUESTIONS[step]
    assert question.options is not None
    return build_onboarding_keyboard(
        step,
        question.options(answers, lang),
        lang=lang,
        done=question.kind == "multi",
        selected=selected,
    )
