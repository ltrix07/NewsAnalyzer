"""Tests for application configuration invariants."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.config import Settings, load_country_registry


@pytest.mark.parametrize("merge_window", ["cluster_window_hours", "consolidate_window_hours"])
def test_merge_windows_must_cover_selection_window(merge_window: str) -> None:
    with pytest.raises(
        ValidationError,
        match=(
            "cluster_window_hours and consolidate_window_hours must each be at least "
            "selection_window_hours"
        ),
    ):
        Settings(selection_window_hours=72, **{merge_window: 71})


def test_country_registry_rejects_partially_filled_prompt_slots(tmp_path: Path) -> None:
    countries_path = tmp_path / "countries.yaml"
    countries_path.write_text(
        """countries:
  PT:
    labels: {en: Portugal}
    residence_prompt_slots:
      central_bank: Banco de Portugal
      currency: EUR
""",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match=r"Country PT.*financial_regulator.*migration_policy_terms",
    ):
        load_country_registry(countries_path)
