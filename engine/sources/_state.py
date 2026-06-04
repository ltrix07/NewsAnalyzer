"""Persistent per-source fetch state for conditional HTTP requests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel


class SourceState(BaseModel):
    """Conditional request state persisted between fetch runs."""

    etag: str | None = None
    last_modified: str | None = None


def load_state(name: str, root: Path) -> SourceState:
    """Load a source state file or return an empty state if none exists."""

    state_path = root / ".state" / f"{name}.json"
    if not state_path.exists():
        return SourceState()

    return SourceState.model_validate_json(state_path.read_text(encoding="utf-8"))


def save_state(name: str, state: SourceState, root: Path) -> None:
    """Atomically persist a source state file under the raw storage root."""

    state_path = root / ".state" / f"{name}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=state_path.parent,
        delete=False,
    ) as temp_file:
        temp_file.write(state.model_dump_json(indent=2))
        temp_path = Path(temp_file.name)

    temp_path.replace(state_path)
