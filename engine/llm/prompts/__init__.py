"""Prompt template rendering helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_THIS_DIR = Path(__file__).resolve().parent
_ENV = Environment(
    loader=FileSystemLoader(_THIS_DIR),
    undefined=StrictUndefined,
    autoescape=False,
)


def render_prompt(template_name: str, **kwargs: Any) -> str:
    """Render a Jinja2 template from this directory by filename."""

    return _ENV.get_template(template_name).render(**kwargs)
