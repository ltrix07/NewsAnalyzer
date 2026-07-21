"""Restore a user's profile JSONB from a YAML profile file.

Recovery for an account whose `users.profile` was accidentally set to NULL. Only
works for accounts that HAVE a YAML source (the configured single-user profile,
e.g. config/profiles/<username>.yaml). Synthesized onboarding profiles have no
YAML and cannot be rebuilt this way — use a DB point-in-time restore instead.

Raw SQL UPDATE on purpose: the server schema predates the delivery-schedule
migration, so the User ORM model / seed-self select users.timezone, which does not
exist yet, and raise UndefinedColumnError. This touches only the `profile` column.

Reloads the profile through the same loader seed-self uses, so the JSONB written is
identical to what a fresh import would produce.

    # dry run: load the YAML and show it would target the row, write nothing
    uv run python audit/restore_profile_from_yaml.py --username <slug>
    # actually write it back
    uv run python audit/restore_profile_from_yaml.py --username <slug> --commit
"""

import argparse
import asyncio
import json

from sqlalchemy import text

from engine.config import get_settings
from engine.db import session_scope
from engine.profile import load_profile


async def restore(username: str, *, commit: bool) -> None:
    settings = get_settings()
    profile = load_profile(username, settings.profile_root)
    payload = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False)
    print(f"loaded YAML profile for {username!r} ({len(payload)} bytes of JSON)")

    async with session_scope() as session:
        exists = (
            await session.execute(
                text(
                    "SELECT (profile IS NULL) AS profile_null "
                    "FROM users WHERE username = :u"
                ),
                {"u": username},
            )
        ).first()
        if exists is None:
            print(f"no such user row: {username!r} — nothing to restore")
            return
        print(f"target row found; current profile_null={exists.profile_null}")
        if not commit:
            print("dry run — pass --commit to write the profile back")
            # roll back so the read-only transaction leaves no trace
            await session.rollback()
            return
        await session.execute(
            text("UPDATE users SET profile = CAST(:p AS JSONB) WHERE username = :u"),
            {"p": payload, "u": username},
        )
        print(f"restored profile for {username!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="User whose profile to restore.")
    parser.add_argument(
        "--commit", action="store_true", help="Write the profile back (default is dry run)."
    )
    args = parser.parse_args()
    asyncio.run(restore(args.username, commit=args.commit))


if __name__ == "__main__":
    main()
