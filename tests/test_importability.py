"""Smoke tests for the initial project scaffold."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_engine_modules_are_importable() -> None:
    """The initial engine scaffold should import without side effects."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    modules = [
        "delivery.__main__",
        "engine.__main__",
        "engine.config",
        "engine.observability",
    ]

    for module_name in modules:
        assert importlib.import_module(module_name) is not None
