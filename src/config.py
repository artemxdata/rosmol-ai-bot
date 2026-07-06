from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"
    api_auth_token: str = ""
    webhook_auth_token: str = ""
    admin_auth_token: str = ""
    hde_trigger_prefix: str = ""
    hde_base_url: str = ""
    hde_api_email: str = ""
    hde_api_key: str = ""
    hde_bot_user_id: str = ""
    hde_request_timeout_seconds: float = Field(default=20.0, ge=1)
    hde_rate_limit_rpm: int = Field(default=250, ge=1, le=300)
    hde_rate_limit_remaining_reserve: int = Field(default=30, ge=0, le=300)
    hde_rate_limit_ban_seconds: int = Field(default=1200, ge=60)
    kb_seed_path: str = "data/knowledge_base_seed.json"
    admin_quality_report_path: str = "reports/presentation_quality/presentation_quality_report.json"
    yonote_api_token: str = ""
    yonote_base_url: str = "https://rossmol.yonote.ru"
    yonote_sync_enabled: bool = False
    yonote_sync_mode: str = "manual"
    yonote_collection_names: str = (
        "Росмолодёжь: общее, структура, направления;Росмолодёжь: мероприятия"
    )
    yonote_request_timeout_seconds: float = Field(default=30.0, ge=1)

    cloud_ru_api_key: str = ""
    cloud_ru_chat_completions_url: str = (
        "https://foundation-models.api.cloud.ru/v1/chat/completions"
    )
    cloud_ru_model: str = ""
    cloud_ru_model_simple: str = "ai-sage/GigaChat3-10B-A1.8B"
    cloud_ru_model_complex: str = "GigaChat/GigaChat-2-Max"
    cloud_ru_model_analyzer: str = ""
    cloud_ru_model_judge: str = ""
    cloud_ru_model_simple_input_price_rub_per_million: float = Field(default=0.0, ge=0)
    cloud_ru_model_simple_output_price_rub_per_million: float = Field(default=0.0, ge=0)
    cloud_ru_model_complex_input_price_rub_per_million: float = Field(default=569.34, ge=0)
    cloud_ru_model_complex_output_price_rub_per_million: float = Field(default=569.34, ge=0)
    cloud_ru_request_timeout_seconds: float = Field(default=12.0, ge=1)
    cloud_ru_max_retries: int = Field(default=2, ge=1, le=5)

    postgres_dsn: str = "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_knowledge_collection: str = "knowledge_base"

    reranker_threshold_low: float = Field(default=0.4, ge=0, le=1)
    reranker_threshold_high: float = Field(default=0.7, ge=0, le=1)
    cache_similarity_threshold: float = Field(default=0.95, ge=0, le=1)
    cache_ttl_hours: int = Field(default=24, ge=1)
    prompt_version: str = "v1.0"

    session_ttl_seconds: int = Field(default=1800, ge=60)
    rate_limit_messages: int = Field(default=20, ge=1)
    rate_limit_window_seconds: int = Field(default=300, ge=10)
    memory_ttl_days: int = Field(default=30, ge=1)
    request_timeout_seconds: float = Field(default=45.0, ge=1)
    ml_unload_after_use: bool = False
    ml_unload_embedder_after_use: bool | None = None
    ml_unload_reranker_after_use: bool | None = None
    ml_prewarm_on_startup: bool = False
    ml_prewarm_timeout_seconds: float = Field(default=120.0, ge=1)
    retrieval_strict_forum_stop_min_chunks: int = Field(default=3, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
