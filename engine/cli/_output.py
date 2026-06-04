"""Shared helpers for safe CLI output formatting."""

from __future__ import annotations

import sys
import textwrap

import typer

DIGEST_LABELS = {
    "ru": {
        "why": (
            "\u041f\u043e\u0447\u0435\u043c\u0443 \u044d\u0442\u043e "
            "\u0432\u0430\u0436\u043d\u043e:"
        ),
        "sources": "\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0438:",
    },
    "uk": {
        "why": "\u0427\u043e\u043c\u0443 \u0446\u0435 \u0432\u0430\u0436\u043b\u0438\u0432\u043e:",
        "sources": "\u0414\u0436\u0435\u0440\u0435\u043b\u0430:",
    },
    "pl": {"why": "Dlaczego to wa\u017cne:", "sources": "\u0179r\u00f3d\u0142a:"},
    "en": {"why": "Why it matters:", "sources": "Sources:"},
}


def safe_echo(text: str = "") -> None:
    """Write one line without crashing on a narrow Windows console encoding."""

    encoding = sys.stdout.encoding or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding)
    typer.echo(safe_text)


def wrap_text(text: str, *, width: int = 100, indent: str = "") -> list[str]:
    """Wrap one block of text into display-ready lines."""

    if not text.strip():
        return [indent.rstrip()]

    return textwrap.fill(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ).splitlines()
