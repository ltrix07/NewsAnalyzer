"""Async retry helpers for transient I/O operations."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    jitter: float = 0.5,
    retryable: Callable[[BaseException], bool],
) -> T:
    """Retry an async operation on retryable failures with exponential backoff."""

    if attempts < 1:
        msg = "attempts must be at least 1"
        raise ValueError(msg)

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except BaseException as exc:
            if not retryable(exc) or attempt == attempts:
                raise

            last_error = exc
            delay = base_delay * (2 ** (attempt - 1)) * random.uniform(1 - jitter, 1 + jitter)
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error
