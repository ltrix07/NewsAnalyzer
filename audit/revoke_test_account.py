"""Revoke a user's synthesized profile so they re-enter onboarding from scratch.

Routing into onboarding is decided by one thing only: `users.profile IS NULL`
(delivery/listener/handlers.py:90). To let a test account fill the questionnaire
again we therefore:

  1. set `users.profile = NULL`  -> next message re-enters onboarding
  2. delete any `onboarding_state` row for that chat_id -> starts at step 0, not resume

The `users` row itself is KEPT. Deleting it would make the bot stop recognizing the
chat_id (handlers.py:87 returns early for unknown chats) and the account would need a
fresh `engine users invite`. Keeping the row preserves the invite; only the profile is
revoked. `enabled` is left untouched.

Runs against whatever DATABASE_URL the environment provides (same resolution the
listener uses), so invoke it with the SAME environment you run the listener with, so it
targets the DB where the account actually lives.

Read-only when called with no username (prints the user list). Mutating only with
--username, and only for that one row.

    # inspect first
    uv run python audit/revoke_test_account.py
    # then revoke one account by its username
    uv run python audit/revoke_test_account.py --username <slug>
"""

import argparse
import asyncio

from sqlalchemy import delete, select

from engine.db import session_scope
from engine.models import OnboardingState, User


async def list_users() -> None:
    async with session_scope() as session:
        users = (await session.execute(select(User).order_by(User.id))).scalars().all()
        if not users:
            print("(no users in this database)")
            return
        print("id  username             chat_id       profile_null  enabled")
        for u in users:
            print(
                f"{u.id:<3} {u.username:<20} {str(u.chat_id):<13} "
                f"{str(u.profile is None):<13} {u.enabled}"
            )


async def revoke(username: str) -> None:
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            print(f"no such user: {username!r} — run without --username to list them")
            return
        print(
            f"before: username={user.username} chat_id={user.chat_id} "
            f"profile_null={user.profile is None} enabled={user.enabled}"
        )
        user.profile = None
        deleted = 0
        if user.chat_id is not None:
            result = await session.execute(
                delete(OnboardingState).where(OnboardingState.chat_id == user.chat_id)
            )
            deleted = result.rowcount or 0
        await session.flush()
        print(
            f"revoked: profile set to NULL, {deleted} onboarding_state row(s) deleted "
            f"for chat_id={user.chat_id}. Next message from this chat restarts the "
            f"questionnaire from step 0."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", help="Revoke this user's profile (omit to list all users).")
    args = parser.parse_args()
    if args.username:
        asyncio.run(revoke(args.username))
    else:
        asyncio.run(list_users())


if __name__ == "__main__":
    main()
