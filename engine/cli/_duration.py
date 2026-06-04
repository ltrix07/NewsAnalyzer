"""Shared helpers for compact duration parsing in CLI commands."""

from __future__ import annotations

import re
from datetime import timedelta

import typer

_DURATION_PATTERN = re.compile(r"^(?P<value>\d+)(?P<suffix>[hdw])$")


def parse_duration(value: str, *, param_hint: str = "--since") -> timedelta:
    """Parse a compact duration string such as ``24h``, ``7d``, or ``1w``."""

    match = _DURATION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise typer.BadParameter(
            "Expected DURATION in the form <number><h|d|w>.",
            param_hint=param_hint,
        )

    amount = int(match.group("value"))
    suffix = match.group("suffix")
    if suffix == "h":
        return timedelta(hours=amount)
    if suffix == "d":
        return timedelta(days=amount)
    return timedelta(weeks=amount)
