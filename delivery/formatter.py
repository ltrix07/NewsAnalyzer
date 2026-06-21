"""Digest to Telegram HTML formatting helpers."""

from __future__ import annotations

import html
import re
from pathlib import Path

from engine.config import get_settings
from engine.domain import Digest
from engine.profile import load_profile

MAX_TELEGRAM_MESSAGE_LENGTH = 4096

_LABELS = {
    "ru": {"why": "Почему это важно:", "sources": "Источники:"},
    "uk": {"why": "Чому це важливо:", "sources": "Джерела:"},
    "pl": {"why": "Dlaczego to ważne:", "sources": "Źródła:"},
    "en": {"why": "Why it matters:", "sources": "Sources:"},
}
_CONFIDENCE_PREFIXES = {"high": "[HIGH]", "medium": "[MEDIUM]", "low": "[LOW]"}
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


def _escape_text(value: str) -> str:
    return html.escape(value, quote=False)


def _escape_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _profile_root() -> Path:
    settings = get_settings()
    return settings.profile_root if settings.profile_root.exists() else Path("config/profiles")


def _load_labels(profile_name: str) -> dict[str, str]:
    profile = load_profile(profile_name, _profile_root())
    return _LABELS.get(profile.output_language.lower(), _LABELS["en"])


def _truncate_text(text: str, max_length: int) -> str:
    stripped = text.strip()
    if max_length <= 0 or not stripped:
        return ""
    if len(stripped) <= max_length:
        return stripped

    sentences = _SENTENCE_BOUNDARY_RE.split(stripped)
    parts: list[str] = []
    current_length = 0
    for sentence in sentences:
        candidate = sentence.strip()
        if not candidate:
            continue
        separator = " " if parts else ""
        candidate_length = current_length + len(separator) + len(candidate)
        if candidate_length + 3 > max_length:
            break
        parts.append(candidate)
        current_length = candidate_length

    if parts:
        return " ".join(parts).rstrip() + "..."

    if max_length <= 3:
        return "." * max_length
    return stripped[: max_length - 3].rstrip() + "..."


def truncate_telegram_message(text: str) -> str:
    """Trim arbitrary Telegram text to the platform message limit."""

    return _truncate_text(text, MAX_TELEGRAM_MESSAGE_LENGTH)


def split_telegram_message(
    text: str,
    limit: int = MAX_TELEGRAM_MESSAGE_LENGTH,
) -> list[str]:
    """Split Telegram HTML text by paragraphs without cutting HTML entities."""

    stripped = text.strip()
    if not stripped:
        return []
    if limit < 1:
        msg = "limit must be positive"
        raise ValueError(msg)

    chunks: list[str] = []
    current = ""
    for paragraph in stripped.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= limit:
            current = paragraph
            continue

        chunks.extend(_split_long_paragraph(paragraph, limit))

    if current:
        chunks.append(current)
    return chunks


def _split_long_paragraph(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        split_at = _avoid_entity_split(remaining, split_at)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _avoid_entity_split(text: str, split_at: int) -> int:
    ampersand = text.rfind("&", 0, split_at)
    semicolon = text.rfind(";", 0, split_at)
    if ampersand > semicolon:
        return max(1, ampersand)
    return split_at


def _build_message(
    *,
    labels: dict[str, str],
    confidence_prefix: str,
    headline: str,
    summary: str,
    why_it_matters: str,
    caveats: list[str],
    citations: list[str],
) -> str:
    sections = [f"<b>{confidence_prefix} {headline}</b>"]

    if summary:
        sections.append(summary)

    sections.append(f"<i>{labels['why']}</i> {why_it_matters}")

    if caveats:
        sections.append("\n".join(caveats))

    sections.append("<b>{}</b>\n{}".format(labels["sources"], "\n".join(citations)))
    return "\n\n".join(sections)


def format_digest(digest: Digest) -> str:
    """Render one digest into Telegram-compatible HTML under the 4096-char limit."""

    labels = _load_labels(digest.profile_name)
    confidence_prefix = _CONFIDENCE_PREFIXES[digest.confidence_level]
    headline = _escape_text(digest.headline)
    summary = _escape_text(digest.summary)
    why_it_matters = _escape_text(digest.why_it_matters)
    caveats = [f"⚠ {_escape_text(caveat)}" for caveat in digest.caveats]
    citations = [
        (
            f'• <a href="{_escape_attr(citation.url)}">'
            f"{_escape_text(citation.source)}: {_escape_text(citation.title)}</a>"
        )
        for citation in digest.citations
    ]

    message = _build_message(
        labels=labels,
        confidence_prefix=confidence_prefix,
        headline=headline,
        summary=summary,
        why_it_matters=why_it_matters,
        caveats=caveats,
        citations=citations,
    )
    if len(message) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        return message

    truncated_summary = summary
    while len(message) > MAX_TELEGRAM_MESSAGE_LENGTH and truncated_summary:
        overflow = len(message) - MAX_TELEGRAM_MESSAGE_LENGTH
        next_length = max(0, len(truncated_summary) - max(overflow, 32))
        updated_summary = _truncate_text(truncated_summary, next_length)
        if updated_summary == truncated_summary:
            break
        truncated_summary = updated_summary
        message = _build_message(
            labels=labels,
            confidence_prefix=confidence_prefix,
            headline=headline,
            summary=truncated_summary,
            why_it_matters=why_it_matters,
            caveats=caveats,
            citations=citations,
        )

    truncated_why = why_it_matters
    while len(message) > MAX_TELEGRAM_MESSAGE_LENGTH and truncated_why:
        overflow = len(message) - MAX_TELEGRAM_MESSAGE_LENGTH
        next_length = max(0, len(truncated_why) - max(overflow, 32))
        updated_why = _truncate_text(truncated_why, next_length)
        if updated_why == truncated_why:
            break
        truncated_why = updated_why
        message = _build_message(
            labels=labels,
            confidence_prefix=confidence_prefix,
            headline=headline,
            summary=truncated_summary,
            why_it_matters=truncated_why,
            caveats=caveats,
            citations=citations,
        )

    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        return message[:MAX_TELEGRAM_MESSAGE_LENGTH]
    return message
