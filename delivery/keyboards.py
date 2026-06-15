"""Inline keyboard helpers for Telegram digest actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FeedbackAction = Literal["like", "dislike"]
KeyboardAction = Literal["like", "dislike", "discussion"]

_LIKE_LABEL = "👍 Интересно"
_DISLIKE_LABEL = "👎 Не интересно"
_DISCUSSION_LABEL = "💬 Обсудить"
_MAX_CALLBACK_BYTES = 64


@dataclass(frozen=True, slots=True)
class CallbackPayload:
    """Parsed callback_data for one digest action."""

    action: KeyboardAction
    digest_id: int


def build_feedback_callback(feedback: FeedbackAction, digest_id: int) -> str:
    """Build compact callback_data for a feedback button."""

    prefix = "fb:l" if feedback == "like" else "fb:d"
    return _validate_callback_data(f"{prefix}:{digest_id}")


def build_discussion_callback(digest_id: int) -> str:
    """Build compact callback_data for the discussion button."""

    return _validate_callback_data(f"dis:{digest_id}")


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

    if len(parts) == 2 and parts[0] == "dis":
        digest_id = _parse_positive_int(parts[1])
        if digest_id is None:
            return None
        return CallbackPayload(action="discussion", digest_id=digest_id)

    return None


def build_digest_keyboard(
    digest_id: int,
    *,
    selected_feedback: FeedbackAction | None = None,
) -> dict[str, list[list[dict[str, str]]]]:
    """Build the three-button inline keyboard for one digest."""

    like_label = _LIKE_LABEL if selected_feedback != "like" else f"✅ {_LIKE_LABEL}"
    dislike_label = _DISLIKE_LABEL if selected_feedback != "dislike" else f"✅ {_DISLIKE_LABEL}"
    return {
        "inline_keyboard": [
            [
                {"text": like_label, "callback_data": build_feedback_callback("like", digest_id)},
                {
                    "text": dislike_label,
                    "callback_data": build_feedback_callback("dislike", digest_id),
                },
            ],
            [{"text": _DISCUSSION_LABEL, "callback_data": build_discussion_callback(digest_id)}],
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
