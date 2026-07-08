"""Dependency-free UI string lookup for Telegram interaction chrome."""

from __future__ import annotations

UI_STRINGS: dict[str, dict[str, str]] = {
    "btn_like": {"ru": "👍 Интересно", "en": "👍 Interesting"},
    "btn_dislike": {"ru": "👎 Не интересно", "en": "👎 Not interesting"},
    "btn_off_topic": {"ru": "📌 Не моя тема", "en": "📌 Not my topic"},
    "btn_weak_analysis": {"ru": "🛠 Слабый разбор", "en": "🛠 Weak analysis"},
    "btn_discussion": {"ru": "💬 Обсудить", "en": "💬 Discuss"},
    "btn_research": {"ru": "🔎 Уточнить в сети", "en": "🔎 Check the web"},
    "ack_like": {"ru": "Записал ✓", "en": "Saved ✓"},
    "ack_dislike_reason_prompt": {
        "ru": "Почему не интересно?",
        "en": "Why not interesting?",
    },
    "ack_discussion": {"ru": "Ок, жду вопрос", "en": "OK, send your question"},
    "msg_ask_question": {
        "ru": "Задайте вопрос по этому разбору одним сообщением.",
        "en": "Ask a question about this brief in one message.",
    },
    "msg_question_expired": {
        "ru": "Срок вопроса истёк — нажмите 💬 ещё раз.",
        "en": "Question expired — tap 💬 again.",
    },
    "msg_research_expired": {
        "ru": "Запрос устарел, нажмите 💬 заново.",
        "en": "Request expired — tap 💬 again.",
    },
    "ack_research_stale": {"ru": "Запрос устарел", "en": "Request expired"},
    "ack_research_searching": {"ru": "Ищу в сети…", "en": "Searching the web…"},
    "research_disclaimer": {
        "ru": (
            "🔎 Собрано из открытых источников, точность не гарантируется — перепроверяйте важное."
        ),
        "en": "🔎 Gathered from open sources; accuracy not guaranteed — verify anything important.",
    },
    "research_digest_not_found": {
        "ru": "Разбор недоступен, поэтому уточнить в сети не получилось.",
        "en": "This brief is unavailable, so the web lookup failed.",
    },
    "research_daily_cap": {
        "ru": "Лимит уточнений в сети на сегодня исчерпан. Попробуйте завтра.",
        "en": "Daily web-lookup limit reached. Try again tomorrow.",
    },
    "research_sources_header": {"ru": "<b>Источники:</b>", "en": "<b>Sources:</b>"},
    "discussion_digest_not_found": {
        "ru": "Не нашёл этот разбор. Возможно, он уже недоступен.",
        "en": "Couldn't find this brief. It may no longer be available.",
    },
    "thread_update_header": {
        "ru": "🔄 <b>Обновление по теме:</b> ",
        "en": "🔄 <b>Update on:</b> ",
    },
}


def t(key: str, lang: str) -> str:
    """Return a localized UI string, falling back to Russian for unknown languages."""

    entry = UI_STRINGS[key]
    return entry.get(lang, entry["ru"])
