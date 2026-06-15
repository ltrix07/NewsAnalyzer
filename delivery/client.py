"""Thin async Telegram Bot API client for outbound digest delivery."""

from __future__ import annotations

from typing import Any

import httpx

from engine._retry import retry_async
from engine.config import get_settings


def _build_async_client(*, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 429 or 500 <= status_code < 600

    return False


class TelegramBotClient:
    """Minimal wrapper over Telegram Bot API endpoints used by delivery."""

    def __init__(self, bot_token: str, base_url: str = "https://api.telegram.org") -> None:
        self._bot_token = bot_token
        self._base_url = base_url.rstrip("/")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one Telegram message and return the parsed JSON response body."""

        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup

        return await self._post("sendMessage", body)

    async def get_updates(self, offset: int, timeout: int) -> dict[str, Any]:
        """Long-poll Telegram updates starting at offset."""

        return await self._post(
            "getUpdates",
            {"offset": offset, "timeout": timeout},
            request_timeout=httpx.Timeout(timeout + 10.0),
        )

    async def answer_callback_query(self, callback_query_id: str, text: str) -> dict[str, Any]:
        """Acknowledge an inline button callback in Telegram."""

        return await self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace inline keyboard markup for an already-sent Telegram message."""

        return await self._post(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        )

    async def _post(
        self,
        method: str,
        body: dict[str, Any],
        *,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        timeout = request_timeout if request_timeout is not None else settings.http_timeout_seconds

        async def _send() -> dict[str, Any]:
            async with _build_async_client(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/bot{self._bot_token}/{method}",
                    json=body,
                )
                response.raise_for_status()

                payload = response.json()
                if not isinstance(payload, dict):
                    msg = "Telegram returned a non-object JSON payload."
                    raise RuntimeError(msg)

                if payload.get("ok") is not True:
                    description = payload.get("description", "Unknown Telegram API error")
                    raise RuntimeError(str(description))

                return payload

        return await retry_async(_send, retryable=_is_retryable_error)
