"""Inline keyboard helpers for Telegram digest actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from delivery.strings import t

FeedbackAction = Literal["like", "dislike"]
DislikeReason = Literal["off_topic", "weak_analysis"]
KeyboardAction = Literal[
    "like", "dislike", "dislike_reason", "discussion", "research", "reveal", "reveal_more"
]
UIEventAction = Literal[
    "like",
    "dislike",
    "dislike_reason",
    "discussion",
    "research",
    "reveal",
    "reveal_more",
    "discussion_question",
    "unknown_callback",
]

_MAX_CALLBACK_BYTES = 64


@dataclass(frozen=True, slots=True)
class CallbackPayload:
    """Parsed callback_data for one digest action."""

    action: KeyboardAction
    digest_id: int | None = None
    batch_id: int | None = None
    reason: str | None = None


def build_feedback_callback(feedback: FeedbackAction, digest_id: int) -> str:
    """Build compact callback_data for a feedback button."""

    prefix = "fb:l" if feedback == "like" else "fb:d"
    return _validate_callback_data(f"{prefix}:{digest_id}")


def build_discussion_callback(digest_id: int) -> str:
    """Build compact callback_data for the discussion button."""

    return _validate_callback_data(f"dis:{digest_id}")


def build_research_callback(digest_id: int) -> str:
    """Build compact callback_data for the research button."""

    return _validate_callback_data(f"res:{digest_id}")


def build_reveal_callback(batch_id: int) -> str:
    return _validate_callback_data(f"rev:{batch_id}")


def build_reveal_more_callback(batch_id: int) -> str:
    return _validate_callback_data(f"revm:{batch_id}")


def build_dislike_reason_callback(reason: DislikeReason, digest_id: int) -> str:
    """Build compact callback_data for a dislike reason button."""

    prefix = "dr:o" if reason == "off_topic" else "dr:q"
    return _validate_callback_data(f"{prefix}:{digest_id}")


def parse_callback_data(callback_data: str) -> CallbackPayload | None:
    """Parse supported callback_data into a typed payload."""

    parts = callback_data.split(":")
    if len(parts) == 3 and parts[0] == "fb" and parts[1] in {"l", "d"}:
        digest_id = _parse_positive_int(parts[2])
        if digest_id is None:
            return None
        return CallbackPayload(
            action="like" if parts[1] == "l" else "dislike",
            digest_id=digest_id,
        )

    if len(parts) == 3 and parts[0] == "dr" and parts[1] in {"o", "q"}:
        digest_id = _parse_positive_int(parts[2])
        if digest_id is None:
            return None
        return CallbackPayload(
            action="dislike_reason",
            digest_id=digest_id,
            reason="off_topic" if parts[1] == "o" else "weak_analysis",
        )

    if len(parts) == 2 and parts[0] == "dis":
        digest_id = _parse_positive_int(parts[1])
        if digest_id is None:
            return None
        return CallbackPayload(action="discussion", digest_id=digest_id)

    if len(parts) == 2 and parts[0] == "res":
        digest_id = _parse_positive_int(parts[1])
        if digest_id is None:
            return None
        return CallbackPayload(action="research", digest_id=digest_id)

    if len(parts) == 2 and parts[0] in {"rev", "revm"}:
        batch_id = _parse_positive_int(parts[1])
        if batch_id is None:
            return None
        return CallbackPayload(
            action="reveal" if parts[0] == "rev" else "reveal_more",
            batch_id=batch_id,
        )

    return None


def build_digest_keyboard(
    digest_id: int,
    *,
    selected_feedback: FeedbackAction | None = None,
    lang: str = "ru",
) -> dict[str, list[list[dict[str, str]]]]:
    """Build the three-button inline keyboard for one digest."""

    base_like_label = t("btn_like", lang)
    base_dislike_label = t("btn_dislike", lang)
    like_label = base_like_label if selected_feedback != "like" else f"✅ {base_like_label}"
    dislike_label = (
        base_dislike_label if selected_feedback != "dislike" else f"✅ {base_dislike_label}"
    )
    return {
        "inline_keyboard": [
            [
                {"text": like_label, "callback_data": build_feedback_callback("like", digest_id)},
                {
                    "text": dislike_label,
                    "callback_data": build_feedback_callback("dislike", digest_id),
                },
            ],
            [
                {
                    "text": t("btn_discussion", lang),
                    "callback_data": build_discussion_callback(digest_id),
                }
            ],
        ]
    }


def build_reveal_keyboard(
    batch_id: int, *, lang: str = "ru"
) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": t("btn_show_digests", lang),
                    "callback_data": build_reveal_callback(batch_id),
                }
            ]
        ]
    }


def build_reveal_more_keyboard(
    batch_id: int, count: int, *, lang: str = "ru"
) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": t("btn_show_more", lang).format(count=count),
                    "callback_data": build_reveal_more_callback(batch_id),
                }
            ]
        ]
    }


def build_dislike_reason_keyboard(
    digest_id: int,
    *,
    lang: str = "ru",
) -> dict[str, list[list[dict[str, str]]]]:
    """Build the two-button drill-down keyboard for dislike reasons."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": t("btn_off_topic", lang),
                    "callback_data": build_dislike_reason_callback("off_topic", digest_id),
                },
                {
                    "text": t("btn_weak_analysis", lang),
                    "callback_data": build_dislike_reason_callback("weak_analysis", digest_id),
                },
            ]
        ]
    }


def build_research_keyboard(
    digest_id: int,
    *,
    lang: str = "ru",
) -> dict[str, list[list[dict[str, str]]]]:
    """Build an inline keyboard for optional web research."""

    return {
        "inline_keyboard": [
            [{"text": t("btn_research", lang), "callback_data": build_research_callback(digest_id)}]
        ]
    }


def _parse_positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _validate_callback_data(callback_data: str) -> str:
    if len(callback_data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
        msg = f"callback_data exceeds {_MAX_CALLBACK_BYTES} bytes"
        raise ValueError(msg)
    return callback_data
