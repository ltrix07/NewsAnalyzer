"""Tests for loading and validating user profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.profile import Profile, load_profile


def test_load_profile_reads_bundled_volodymyr_profile() -> None:
    profile = load_profile("volodymyr", Path("config/profiles"))

    assert profile.name == "volodymyr"
    assert profile.location == "PL (Warsaw)"
    assert "ru" in profile.languages
    assert profile.output_language == "ru"
    assert profile.keyword_rules.keep_if_matches


def test_profile_model_rejects_unknown_fields() -> None:
    payload = {
        "name": "volodymyr",
        "location": "PL (Warsaw)",
        "citizenship": "UA",
        "languages": ["ru"],
        "output_language": "ru",
        "interests": ["Macro"],
        "not_interested": ["Sports"],
        "keyword_rules": {"keep_if_matches": [], "drop_if_matches": []},
        "unexpected": True,
    }

    with pytest.raises(ValidationError):
        Profile.model_validate(payload)
