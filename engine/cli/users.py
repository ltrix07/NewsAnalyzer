"""Operator commands for database-backed user profiles."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import select

from engine.config import get_settings
from engine.db import session_scope
from engine.models import User
from engine.profile import load_profile
from engine.users import get_user_by_chat_id, get_user_by_username

app = typer.Typer(help="Manage database-backed user profiles.")
_USERNAME_RE = re.compile(r"^[a-z0-9_]+$")
USERNAME_OPTION = typer.Option(..., help="Lowercase profile slug.")
CHAT_ID_OPTION = typer.Option(None, help="Telegram delivery chat id.")
PROFILE_OPTION = typer.Option(..., exists=True, dir_okay=False, help="Profile YAML path.")
UI_LANGUAGE_OPTION = typer.Option("ru", help="Telegram UI language.")
INVITE_CHAT_ID_OPTION = typer.Option(..., "--chat-id", help="Invited Telegram chat id.")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("username must match ^[a-z0-9_]+$")


async def add_user_command(
    *, username: str, chat_id: int | None, profile_path: Path, ui_language: str
) -> None:
    """Validate a YAML profile and insert one user."""

    _validate_username(username)
    profile = load_profile(profile_path.stem, profile_path.parent)
    settings = get_settings()
    async with session_scope() as session:
        if await get_user_by_username(username, session) is not None:
            raise ValueError(f"User '{username}' already exists.")
        if chat_id is not None and await get_user_by_chat_id(chat_id, session) is not None:
            raise ValueError(f"Telegram chat_id {chat_id} already exists.")
        session.add(
            User(
                username=username,
                chat_id=chat_id,
                profile=profile.model_dump(mode="json"),
                ui_language=ui_language,
                timezone=settings.default_timezone,
            )
        )


@app.command("add")
def add_user(
    username: str = USERNAME_OPTION,
    chat_id: int | None = CHAT_ID_OPTION,
    profile: Path = PROFILE_OPTION,
    ui_language: str = UI_LANGUAGE_OPTION,
) -> None:
    """Add a validated user profile."""

    asyncio.run(
        add_user_command(
            username=username,
            chat_id=chat_id,
            profile_path=profile,
            ui_language=ui_language,
        )
    )


async def invite_user_command(*, username: str, chat_id: int, ui_language: str) -> None:
    """Insert an invited, inactive user without a synthesized profile."""

    _validate_username(username)
    settings = get_settings()
    async with session_scope() as session:
        if await get_user_by_username(username, session) is not None:
            raise ValueError(f"User '{username}' already exists.")
        if await get_user_by_chat_id(chat_id, session) is not None:
            raise ValueError(f"Telegram chat_id {chat_id} already exists.")
        session.add(
            User(
                username=username,
                chat_id=chat_id,
                profile=None,
                ui_language=ui_language,
                enabled=False,
                timezone=settings.default_timezone,
            )
        )


@app.command("invite")
def invite_user(
    username: str = USERNAME_OPTION,
    chat_id: int = INVITE_CHAT_ID_OPTION,
    ui_language: str = UI_LANGUAGE_OPTION,
) -> None:
    """Invite a Telegram chat to complete onboarding."""

    asyncio.run(invite_user_command(username=username, chat_id=chat_id, ui_language=ui_language))


async def list_users_command() -> None:
    async with session_scope() as session:
        users = list((await session.scalars(select(User).order_by(User.username))).all())
    typer.echo("username\tchat_id\tenabled\tui_language\toutput_language")
    for user in users:
        typer.echo(
            f"{user.username}\t{user.chat_id or '-'}\t{str(user.enabled).lower()}\t"
            f"{user.ui_language}\t{user.profile['output_language'] if user.profile else '-'}"
        )


@app.command("list")
def list_users() -> None:
    """List configured users."""

    asyncio.run(list_users_command())


async def show_user_command(username: str) -> None:
    async with session_scope() as session:
        user = await get_user_by_username(username, session)
        if user is None:
            raise LookupError(f"User '{username}' does not exist.")
        typer.echo(json.dumps(user.profile, ensure_ascii=False, indent=2, sort_keys=True))


@app.command("show")
def show_user(username: str) -> None:
    """Print one stored profile."""

    asyncio.run(show_user_command(username))


async def set_enabled_command(username: str, *, enabled: bool) -> None:
    async with session_scope() as session:
        user = await get_user_by_username(username, session)
        if user is None:
            raise LookupError(f"User '{username}' does not exist.")
        user.enabled = enabled
        user.updated_at = datetime.now(UTC)


@app.command("enable")
def enable_user(username: str) -> None:
    """Enable one user."""

    asyncio.run(set_enabled_command(username, enabled=True))


@app.command("disable")
def disable_user(username: str) -> None:
    """Disable one user."""

    asyncio.run(set_enabled_command(username, enabled=False))


async def seed_self_command() -> None:
    """Idempotently import the configured single-user YAML profile."""

    settings = get_settings()
    username = settings.profile_name
    _validate_username(username)
    profile = load_profile(username, settings.profile_root)
    async with session_scope() as session:
        user = await get_user_by_username(username, session)
        chat_id = settings.require_telegram_chat_id()
        chat_owner = await get_user_by_chat_id(chat_id, session)
        if chat_owner is not None and chat_owner.username != username:
            raise ValueError(f"Telegram chat_id {chat_id} already belongs to another user.")
        if user is None:
            session.add(
                User(
                    username=username,
                    chat_id=chat_id,
                    profile=profile.model_dump(mode="json"),
                    ui_language=settings.ui_language,
                    timezone=settings.default_timezone,
                )
            )
        else:
            user.chat_id = chat_id
            user.profile = profile.model_dump(mode="json")
            user.ui_language = settings.ui_language
            user.updated_at = datetime.now(UTC)


@app.command("seed-self")
def seed_self() -> None:
    """Import or refresh the configured single-user profile."""

    asyncio.run(seed_self_command())
