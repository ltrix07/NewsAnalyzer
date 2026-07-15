"""Database access helpers for operator-managed users."""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.models import User
from engine.profile import Profile


async def get_user_by_username(username: str, session: AsyncSession) -> User | None:
    """Return a user by profile slug, if it exists."""

    return cast(User | None, await session.scalar(select(User).where(User.username == username)))


async def get_user_by_chat_id(chat_id: int, session: AsyncSession) -> User | None:
    """Return a user by Telegram chat id, if it exists."""

    return cast(User | None, await session.scalar(select(User).where(User.chat_id == chat_id)))


async def list_enabled_users(session: AsyncSession) -> list[User]:
    """Return enabled users in stable username order."""

    statement = select(User).where(User.enabled.is_(True)).order_by(User.username)
    return list((await session.scalars(statement)).all())


async def resolve_profile(username: str, session: AsyncSession) -> Profile:
    """Resolve and validate a stored profile by username."""

    user = await get_user_by_username(username, session)
    if user is None:
        msg = f"User profile '{username}' does not exist in the users table."
        raise LookupError(msg)
    if user.profile is None:
        msg = f"User profile '{username}' is not available until onboarding is complete."
        raise LookupError(msg)
    return Profile.model_validate(user.profile)
