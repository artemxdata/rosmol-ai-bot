from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    gigachat_api_key: str = ""
    gigachat_access_token: str = ""
    gigachat_scope: str = "GIGACHAT_API_CORP"
    gigachat_verify_ssl: bool = False
    gigachat_base_url: str = ""
    gigachat_auth_url: str = ""

    postgres_dsn: str = "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    reranker_threshold_low: float = Field(default=0.4, ge=0, le=1)
    reranker_threshold_high: float = Field(default=0.7, ge=0, le=1)
    cache_similarity_threshold: float = Field(default=0.95, ge=0, le=1)
    cache_ttl_hours: int = Field(default=24, ge=1)
    prompt_version: str = "v1.0"

    session_ttl_seconds: int = Field(default=1800, ge=60)
    rate_limit_messages: int = Field(default=20, ge=1)
    rate_limit_window_seconds: int = Field(default=300, ge=10)
    memory_ttl_days: int = Field(default=30, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
