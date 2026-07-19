"""Application settings loaded from environment variables and the local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared across the engine."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str | None = None
    openai_api_key: str | None = None
    profile_name: str = "volodymyr"
    profile_root: Path = Path("config/profiles")
    log_level: str = "INFO"
    app_env: Literal["dev", "prod"] = "dev"
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    ui_language: Literal["ru", "en"] = "ru"
    # TODO(stage-1): derive this from the user's residence-country configuration.
    default_timezone: str = "Europe/Warsaw"
    raw_storage_path: Path = Path("raw")
    http_timeout_seconds: float = 30.0
    cluster_similarity_threshold: float = 0.82
    cluster_window_hours: int = 72
    # Skip stale per-user work after long outages; missed history is intentionally not backfilled.
    selection_window_hours: int = 72
    consolidate_enabled: bool = True
    consolidate_window_hours: int = 72
    consolidate_candidate_min_similarity: float = 0.50
    consolidate_max_neighbors: int = 5
    thread_updates_enabled: bool = True
    thread_window_hours: int = 72
    thread_min_similarity: float = 0.50
    thread_max_candidates: int = 3
    link_tracking_enabled: bool = False
    batched_delivery_enabled: bool = False
    batch_reveal_page_size: int = 5
    batch_nudge_after_days: int = 3
    redirect_base_url: str | None = None
    openai_model_relevance: str = "gpt-4o-mini"
    relevance_v4_enabled: bool = False
    openai_model_consolidate: str = "gpt-4o-mini"
    openai_model_verify: str = "gpt-4o"
    openai_model_summarize: str = "gpt-4o"
    telegram_long_poll_seconds: int = 25
    discussion_model: str = "gpt-4o-mini"
    tavily_api_key: str | None = None
    tavily_max_results: int = 6
    tavily_search_depth: Literal["basic", "advanced"] = "advanced"
    research_model: str = "gpt-4o"
    research_daily_cap: int = 20
    research_pending_ttl_minutes: int = 15
    # Feedback-driven taste re-ranking at delivery (ROADMAP 1в, stage 2).
    taste_ranking_enabled: bool = True
    taste_weight: float = 1.0
    significance_weight: float = 0.5
    taste_min_labels_per_class: int = 3

    @model_validator(mode="after")
    def validate_merge_windows_cover_selection_window(self) -> Settings:
        """Prevent selectable events from falling outside both merge windows."""

        if min(self.cluster_window_hours, self.consolidate_window_hours) < (
            self.selection_window_hours
        ):
            msg = (
                "cluster_window_hours and consolidate_window_hours must each be at least "
                "selection_window_hours"
            )
            raise ValueError(msg)
        return self

    def require_database_url(self) -> str:
        """Return the configured database URL or raise a clear runtime error."""

        if self.database_url is None:
            msg = "DATABASE_URL is not configured"
            raise RuntimeError(msg)
        return self.database_url

    def require_telegram_token(self) -> str:
        """Return the Telegram Bot API token or raise a clear runtime error."""

        if self.telegram_bot_token is None:
            msg = "TELEGRAM_BOT_TOKEN is not configured"
            raise RuntimeError(msg)
        return self.telegram_bot_token

    def require_telegram_chat_id(self) -> int:
        """Return the Telegram chat id or raise a clear runtime error."""

        if self.telegram_chat_id is None:
            msg = "TELEGRAM_CHAT_ID is not configured"
            raise RuntimeError(msg)
        return self.telegram_chat_id

    def require_redirect_base_url(self) -> str:
        """Return the public redirect origin when link tracking is enabled."""

        if self.redirect_base_url is None:
            msg = "REDIRECT_BASE_URL is not configured while link tracking is enabled"
            raise RuntimeError(msg)
        return self.redirect_base_url.removesuffix("/")

    def require_tavily_key(self) -> str:
        """Return the Tavily API key or raise a clear runtime error."""

        if self.tavily_api_key is None:
            msg = "TAVILY_API_KEY is not configured"
            raise RuntimeError(msg)
        return self.tavily_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()
