"""Registry and factory helpers for configured sources."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.models import Source as SourceModel
from engine.sources.base import Source, SourceConfig

_REGISTRY: dict[str, type[Source]] = {}


class UnknownSourceKindError(Exception):
    """Raised when no source implementation exists for a configured kind."""


def register_source(kind: str) -> Callable[[type[Source]], type[Source]]:
    """Register a source subclass under a concrete source kind."""

    def decorator(source_cls: type[Source]) -> type[Source]:
        if kind in _REGISTRY:
            msg = f"Source kind {kind!r} is already registered."
            raise ValueError(msg)

        declared_kind = getattr(source_cls, "kind", None)
        if declared_kind is not None and declared_kind != kind:
            msg = (
                f"Source class {source_cls.__name__} declares kind {declared_kind!r}, not {kind!r}."
            )
            raise ValueError(msg)

        source_cls.kind = cast(Any, kind)
        _REGISTRY[kind] = source_cls
        return source_cls

    return decorator


def build_source(config: SourceConfig) -> Source:
    """Build a concrete source instance for the provided config."""

    source_cls = _REGISTRY.get(config.kind)
    if source_cls is None:
        msg = f"No source implementation is registered for kind {config.kind!r}."
        raise UnknownSourceKindError(msg)
    return source_cls(config)


def load_sources_config(path: Path) -> list[SourceConfig]:
    """Load and validate source configuration entries from YAML."""

    with path.open("r", encoding="utf-8") as source_file:
        payload = yaml.safe_load(source_file) or {}

    if not isinstance(payload, dict):
        msg = f"Source config at {path} must be a mapping with a top-level 'sources' key."
        raise ValueError(msg)

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        msg = f"Source config at {path} must contain a top-level 'sources' list."
        raise ValueError(msg)

    configs: list[SourceConfig] = []
    seen_names: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        try:
            config = SourceConfig.model_validate(raw_source)
        except ValidationError as exc:
            msg = f"Invalid source config at {path} index {index}: {exc}"
            raise ValueError(msg) from exc

        if config.name in seen_names:
            msg = f"Duplicate source name {config.name!r} in {path}."
            raise ValueError(msg)
        seen_names.add(config.name)
        configs.append(config)

    return configs


def load_sources(path: Path) -> list[Source]:
    """Load all enabled sources from the configured YAML registry."""

    return [build_source(config) for config in load_sources_config(path) if config.enabled]


async def sync_sources_to_db(
    configs: list[SourceConfig],
    session: AsyncSession,
) -> dict[str, int]:
    """Upsert source configs into the sources table. Returns {name: id}."""

    synced_rows: dict[str, SourceModel] = {}
    for config in configs:
        existing = await session.scalar(select(SourceModel).where(SourceModel.name == config.name))
        if existing is None:
            existing = SourceModel(
                name=config.name,
                kind=config.kind,
                url=config.url,
                enabled=config.enabled,
                poll_interval_seconds=config.poll_interval_seconds,
                config=config.config,
            )
            session.add(existing)
        else:
            existing.kind = config.kind
            existing.url = config.url
            existing.enabled = config.enabled
            existing.poll_interval_seconds = config.poll_interval_seconds
            existing.config = config.config

        synced_rows[config.name] = existing

    await session.flush()
    return {name: row.id for name, row in synced_rows.items()}
