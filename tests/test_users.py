"""Tests for database-backed user profiles and operator commands."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.cli import filter as filter_cli
from engine.cli import score as score_cli
from engine.cli import users as users_cli
from engine.models import User
from engine.profile import load_profile
from engine.users import list_enabled_users, resolve_profile


@pytest.fixture(autouse=True)
def _use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(users_cli, "session_scope", fake_session_scope)


@pytest.mark.asyncio
async def test_profile_round_trip_matches_yaml(db_session: AsyncSession) -> None:
    expected = load_profile("volodymyr", Path("config/profiles"))
    db_session.add(User(username="volodymyr", profile=expected.model_dump(mode="json")))
    await db_session.flush()

    assert await resolve_profile("volodymyr", db_session) == expected


@pytest.mark.asyncio
async def test_resolve_unknown_profile_is_clear(db_session: AsyncSession) -> None:
    with pytest.raises(LookupError, match="missing.*does not exist"):
        await resolve_profile("missing", db_session)


@pytest.mark.asyncio
async def test_add_rejects_duplicate_username_and_chat_id(db_session: AsyncSession) -> None:
    profile_path = Path("config/profiles/volodymyr.yaml")
    await users_cli.add_user_command(
        username="first", chat_id=100, profile_path=profile_path, ui_language="ru"
    )
    await db_session.flush()

    with pytest.raises(ValueError, match="already exists"):
        await users_cli.add_user_command(
            username="first", chat_id=101, profile_path=profile_path, ui_language="ru"
        )
    with pytest.raises(ValueError, match="chat_id 100"):
        await users_cli.add_user_command(
            username="second", chat_id=100, profile_path=profile_path, ui_language="ru"
        )


@pytest.mark.asyncio
async def test_invite_creates_inactive_user_without_profile_and_rejects_duplicate(
    db_session: AsyncSession,
) -> None:
    await users_cli.invite_user_command(username="invitee", chat_id=777, ui_language="en")
    await db_session.flush()
    user = await db_session.scalar(select(User).where(User.username == "invitee"))
    assert user is not None
    assert user.profile is None
    assert user.enabled is False
    assert user.ui_language == "en"

    with pytest.raises(ValueError, match="already exists"):
        await users_cli.invite_user_command(username="invitee", chat_id=778, ui_language="ru")


@pytest.mark.asyncio
async def test_add_invalid_profile_writes_no_user(db_session: AsyncSession, tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("profile:\n  name: invalid\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        await users_cli.add_user_command(
            username="invalid", chat_id=200, profile_path=invalid, ui_language="ru"
        )

    assert await db_session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.asyncio
async def test_disable_excludes_user_and_enable_restores_it(db_session: AsyncSession) -> None:
    profile = load_profile("volodymyr", Path("config/profiles"))
    db_session.add(User(username="volodymyr", profile=profile.model_dump(mode="json")))
    await db_session.flush()

    await users_cli.set_enabled_command("volodymyr", enabled=False)
    await db_session.flush()
    assert await list_enabled_users(db_session) == []

    await users_cli.set_enabled_command("volodymyr", enabled=True)
    await db_session.flush()
    assert [user.username for user in await list_enabled_users(db_session)] == ["volodymyr"]


@pytest.mark.asyncio
async def test_seed_self_is_idempotent_and_updates_existing_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = users_cli.get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", 123456)
    monkeypatch.setattr(settings, "ui_language", "en")

    await users_cli.seed_self_command()
    await db_session.flush()
    user = await db_session.scalar(select(User))
    assert user is not None
    user.profile = {**user.profile, "location": "stale"}
    await users_cli.seed_self_command()
    await db_session.flush()

    users = list((await db_session.scalars(select(User))).all())
    assert len(users) == 1
    assert users[0].chat_id == 123456
    assert users[0].ui_language == "en"
    assert users[0].profile["location"] != "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_module", [filter_cli, score_cli])
async def test_filter_and_score_resolve_runtime_profile_from_database(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    command_module: object,
) -> None:
    profile = load_profile("volodymyr", Path("config/profiles"))
    db_session.add(User(username="volodymyr", profile=profile.model_dump(mode="json")))
    await db_session.flush()

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(command_module, "session_scope", fake_session_scope)
    if command_module is filter_cli:
        await filter_cli.filter_command(limit=1)
    else:
        await score_cli.score_command(limit=1)
