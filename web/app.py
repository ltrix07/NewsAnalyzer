"""Small ASGI application serving tracked citation redirects."""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.db import session_scope
from engine.models import DigestLink, LinkClick

logger = structlog.get_logger(__name__)
app = FastAPI()

_CRAWLER_MARKERS = (
    "telegrambot",
    "bot",
    "crawler",
    "spider",
    "preview",
    "facebookexternalhit",
)


def _is_crawler(user_agent: str | None) -> bool:
    normalized = (user_agent or "").lower()
    return any(marker in normalized for marker in _CRAWLER_MARKERS)


async def _record_click(
    session: AsyncSession,
    *,
    link_id: int,
    user_agent: str | None,
) -> None:
    session.add(LinkClick(link_id=link_id, user_agent=user_agent[:512] if user_agent else None))


@app.get("/healthz", response_class=JSONResponse)
async def healthz() -> dict[str, str]:
    """Return process health without touching the database."""

    return {"status": "ok"}


@app.get("/r/{token}", response_class=RedirectResponse, response_model=None)
async def redirect_link(token: str, request: Request) -> RedirectResponse | PlainTextResponse:
    """Record a human click when possible, then redirect to the stored target."""

    async with session_scope() as session:
        link = await session.scalar(select(DigestLink).where(DigestLink.token == token))
        if link is None:
            return PlainTextResponse("Link not found", status_code=404)
        link_id = link.id
        target_url = link.url

    if not target_url.lower().startswith(("http://", "https://")):
        logger.warning("invalid_link_redirect_url", link_id=link_id)
        return PlainTextResponse("Link not found", status_code=404)

    user_agent = request.headers.get("user-agent")
    if not _is_crawler(user_agent):
        try:
            async with session_scope() as session:
                await _record_click(session, link_id=link_id, user_agent=user_agent)
        except Exception:
            logger.warning("link_click_logging_failed", link_id=link_id)

    return RedirectResponse(
        target_url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )
