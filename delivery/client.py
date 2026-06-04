"""Thin async Telegram Bot API client for outbound digest delivery."""

from __future__ import annotations

from typing import Any

import httpx

from engine._retry import retry_async
from engine.config import get_settings


def _build_async_client(*, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 429 or 500 <= status_code < 600

    return False


class TelegramBotClient:
    """Minimal wrapper over Telegram's sendMessage endpoint."""

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
    ) -> dict[str, Any]:
        """Send one Telegram message and return the parsed JSON response body."""

        settings = get_settings()

        async def _send() -> dict[str, Any]:
            async with _build_async_client(timeout=settings.http_timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/bot{self._bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": disable_web_page_preview,
                    },
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
