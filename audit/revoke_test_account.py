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

Uses raw SQL against only the long-standing columns (id, username, chat_id, profile,
enabled). It deliberately does NOT go through the `User` ORM model: on a server whose
schema predates the delivery-schedule migration, the model selects columns
(users.timezone, ...) that do not exist yet and every query raises UndefinedColumnError.
Raw SQL sidesteps that drift so this can run before the migration is applied.

Runs against whatever DATABASE_URL the environment provides (same resolution the
listener uses).

Read-only when called with no username (prints the user list). Mutating only with
--username, and only for that one row.

    # inspect first
    uv run python audit/revoke_test_account.py
    # then revoke one account by its username
    uv run python audit/revoke_test_account.py --username <slug>
"""

import argparse
import asyncio

from sqlalchemy import text

from engine.db import session_scope


async def list_users() -> None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, username, chat_id, (profile IS NULL) AS profile_null, enabled "
                    "FROM users ORDER BY id"
                )
            )
        ).all()
        if not rows:
            print("(no users in this database)")
            return
        print("id  username             chat_id       profile_null  enabled")
        for r in rows:
            print(
                f"{r.id:<3} {r.username:<20} {str(r.chat_id):<13} "
                f"{str(r.profile_null):<13} {r.enabled}"
            )


async def revoke(username: str) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "UPDATE users SET profile = NULL WHERE username = :u "
                    "RETURNING chat_id, (profile IS NULL) AS profile_null"
                ),
                {"u": username},
            )
        ).first()
        if row is None:
            print(f"no such user: {username!r} — run without --username to list them")
            return
        chat_id = row.chat_id
        deleted = 0
        if chat_id is not None:
            result = await session.execute(
                text("DELETE FROM onboarding_state WHERE chat_id = :cid"),
                {"cid": chat_id},
            )
            deleted = result.rowcount or 0
        print(
            f"revoked username={username}: profile set to NULL, "
            f"{deleted} onboarding_state row(s) deleted for chat_id={chat_id}. "
            f"Next message from this chat restarts the questionnaire from step 0."
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
