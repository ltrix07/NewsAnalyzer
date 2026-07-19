"""Tests for application settings invariants."""

import pytest
from pydantic import ValidationError

from engine.config import Settings


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
