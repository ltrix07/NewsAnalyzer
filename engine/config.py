"""Application settings loaded from environment variables and the local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    raw_storage_path: Path = Path("raw")
    http_timeout_seconds: float = 30.0
    cluster_similarity_threshold: float = 0.82
    cluster_window_hours: int = 36
    openai_model_relevance: str = "gpt-4o-mini"
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
