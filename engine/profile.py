"""Profile models and loader for user-specific filtering behavior."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field


class KeywordRules(BaseModel):
    """Regex-based keep/drop rules applied before LLM scoring."""

    keep_if_matches: list[str] = Field(default_factory=list)
    drop_if_matches: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    """Validated user profile injected into prompts and cheap filters."""

    model_config = ConfigDict(extra="forbid")

    name: str
    location: str
    citizenship: str
    languages: list[str]
    output_language: str
    interests: list[str]
    not_interested: list[str]
    keyword_rules: KeywordRules


def load_profile(name: str, root: Path) -> Profile:
    """Read config/profiles/<name>.yaml and return a validated Profile."""

    profile_path = root / f"{name}.yaml"
    with profile_path.open("r", encoding="utf-8") as profile_file:
        payload = yaml.safe_load(profile_file)

    if not isinstance(payload, dict) or "profile" not in payload:
        msg = f"Profile file {profile_path} must contain a top-level 'profile' key."
        raise RuntimeError(msg)

    return Profile.model_validate(payload["profile"])
