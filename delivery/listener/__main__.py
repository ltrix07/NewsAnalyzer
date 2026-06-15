"""CLI module for the Telegram long-poll listener."""

from __future__ import annotations

import asyncio

from delivery.listener.service import run_listener
from engine.config import get_settings
from engine.observability import configure_logging


def main() -> None:
    """Configure process-wide services and run the listener."""

    configure_logging(get_settings())
    asyncio.run(run_listener())


if __name__ == "__main__":
    main()
