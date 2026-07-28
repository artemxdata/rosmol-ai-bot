from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hmac import compare_digest
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from time import perf_counter, time
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from scripts.sync_yonote_kb import (
    YonoteApiError,
    YonoteDataTooLarge,
    YonoteOperationTimeout,
)
from src.admin import kb_index, kb_store, ui
from src.admin.yonote_database import (
    YonoteDatabaseExportTooLarge,
)
from src.admin.yonote_database import (
    count_database as count_yonote_database,
)
from src.admin.yonote_database import (
    export_database as export_yonote_database,
)
from src.admin.yonote_sync import (
    YonoteSyncConfigError,
)
from src.admin.yonote_sync import (
    apply_sync as apply_yonote_sync,
)
from src.admin.yonote_sync import (
    preview_sync as preview_yonote_sync,
)
from src.channels.hde import HDEAdapter, HDEPayloadError
from src.channels.hde_transport import (
    HDEStableEventRequired,
    HDETransportRepository,
    HDETransportValidationError,
)
from src.channels.hde_worker import HDETransportWorker
from src.channels.max import MaxAdapter
from src.channels.vk import VKAdapter
from src.config import get_settings
from src.graph.context import NON_FORUM_CONTEXT_CATEGORIES, is_context_dependent_followup
from src.graph.graph import build_graph
from src.graph.nodes.analyze import is_safe_offtopic_message
from src.kb.forum_registry import detect_forum_from_text, detect_forums_from_text
from src.kb.temporal import is_registration_query
from src.llm.client import CloudRuLLMClient
from src.llm.routing import estimate_routing_hint
from src.llm.usage import (
    reset_llm_usage_collection,
    start_llm_usage_collection,
    summarize_llm_usage,
)
from src.logging.db_logger import log_request
from src.logging.tracer import Tracer
from src.models import Channel, Chunk, IncomingMessage, QueryAnalysis, Session
from src.ops.reports import build_trace_report
from src.rag.cache import CachedResponse, SemanticCache
from src.rag.embedder import Embedder
from src.rag.errors import MLDependencyError
from src.rag.reranker import Reranker
from src.rag.retriever import Retriever
from src.response_contract import get_response_contract
from src.security import profanity, safety
from src.security.operator_request import is_operator_request
from src.security.pii_masker import PIIMasker, PIIMaskingUnavailable
from src.security.rate_limiter import RateLimiter
from src.session.manager import SessionManager
from src.session.memory import UserMemory, hash_user_id

_RESPONSE_CONTRACT = get_response_contract()
OPERATOR_TRANSFER_RESPONSE = _RESPONSE_CONTRACT.message(
    "operator_transfer"
).select_text()
ATTACHMENT_ONLY_RESPONSE = OPERATOR_TRANSFER_RESPONSE
ATTACHMENT_ONLY_REASON = "attachment_only"
_ATTACHMENT_FILE_RE = re.compile(
    r"\b[\w.-]+\.(?:png|jpe?g|gif|webp|heic|pdf|docx?|xlsx?)\b",
    flags=re.IGNORECASE,
)
_ATTACHMENT_WORD_RE = re.compile(
    r"\b(?:скрин(?:шот)?|фото|картинк[аиу]|изображени[ея]|вложени[ея]|"
    r"файл|прикрепил[аи]?|прикреплен[оа]?|смотри|посмотри|image|photo|attachment)\b",
    flags=re.IGNORECASE,
)
_MEANINGFUL_WORD_RE = re.compile(r"[а-яa-z0-9]{3,}", flags=re.IGNORECASE)
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _validate_runtime_security(settings)
    kb_manifest = await asyncio.to_thread(kb_store.validate_seed, Path(settings.kb_seed_path))
    published_records = int(kb_manifest.get("status_counts", {}).get("published", 0))
    if published_records <= 0:
        raise RuntimeError("KB_SEED_PATH contains no published records")
    app.state.runtime_settings = settings
    app.state.runtime_config = {
        "status": "ok",
        "runtime_role": settings.runtime_role,
        "release_git_sha": settings.release_git_sha,
    }
    app.state.kb_manifest = {
        "status": "ok",
        "published_records": published_records,
    }
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.pg_pool = await asyncpg.create_pool(settings.postgres_dsn, min_size=1, max_size=5)
    app.state.qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
    )
    app.state.memory = UserMemory(app.state.pg_pool)
    app.state.sessions = SessionManager(app.state.redis, app.state.memory)
    app.state.rate_limiter = RateLimiter(app.state.redis)
    app.state.pii_masker = PIIMasker()
    app.state.llm_client = CloudRuLLMClient()
    app.state.embedder = Embedder()
    app.state.retriever = Retriever(
        app.state.qdrant,
        app.state.embedder,
        collection_name=settings.qdrant_knowledge_collection,
    )
    app.state.reranker = Reranker()
    app.state.semantic_cache = SemanticCache(app.state.qdrant, app.state.embedder)
    app.state.graph = build_graph()
    app.state.ml_prewarm = {
        "enabled": settings.ml_prewarm_on_startup,
        "status": "disabled",
    }
    if settings.ml_prewarm_on_startup:
        await _prewarm_ml_runtime(app, settings)
    app.state.hde_transport_repository = None
    app.state.hde_transport_worker = None
    if settings.runtime_role == "ml" and settings.hde_transport_enabled:
        repository = HDETransportRepository(
            app.state.pg_pool,
            event_key_secret=settings.hde_transport_event_key_secret,
            encryption_key=settings.hde_transport_encryption_key,
        )
        worker = HDETransportWorker(
            repository=repository,
            pg_pool=app.state.pg_pool,
            app=app,
            process_message=process_message,
            send_message=hde_adapter.send,
            worker_id=f"hde-ml-{uuid4().hex[:12]}",
            lease_timeout_seconds=settings.hde_transport_lease_timeout_seconds,
            poll_interval_seconds=settings.hde_transport_poll_interval_seconds,
            recovery_interval_seconds=settings.hde_transport_recovery_interval_seconds,
            shutdown_timeout_seconds=settings.hde_transport_shutdown_timeout_seconds,
        )
        await worker.start()
        app.state.hde_transport_repository = repository
        app.state.hde_transport_worker = worker
    try:
        yield
    finally:
        worker = getattr(app.state, "hde_transport_worker", None)
        if worker is not None:
            await worker.stop()
        await app.state.llm_client.aclose()
        await app.state.redis.aclose()
        await app.state.pg_pool.close()
        await app.state.qdrant.close()


app = FastAPI(title="Rosmol AI Bot", version="0.1.0", lifespan=lifespan)
_YONOTE_SYNC_LOCK = Lock()
_ADMIN_MUTATION_LOCK = Lock()

PRODUCTION_ADMIN_KB_SEED_PATH = (
    "/app/data/private/admin-kb/knowledge_base_seed.json"
)
_T = TypeVar("_T")


def _run_with_yonote_sync_lock(
    operation: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    try:
        return operation(*args, **kwargs)
    finally:
        _YONOTE_SYNC_LOCK.release()

vk_adapter = VKAdapter()
max_adapter = MaxAdapter()
hde_adapter = HDEAdapter()

ADMIN_SESSION_COOKIE = "rosmol_admin_session"
ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def _validate_runtime_security(settings: Any) -> None:
    app_env = str(getattr(settings, "app_env", "local") or "local").strip().casefold()
    runtime_role = _setting_text(settings, "runtime_role") or "api"
    transport_enabled = bool(getattr(settings, "hde_transport_enabled", False))
    if app_env not in {"local", "test", "staging", "production"}:
        raise RuntimeError("APP_ENV must be one of: local, test, staging, production")

    errors: list[str] = []
    user_hash_secret = _setting_text(settings, "user_hash_secret")
    if app_env not in {"local", "test"} and not user_hash_secret:
        errors.append("USER_HASH_SECRET is required outside local/test environments")

    if app_env == "production":
        admin_read_only = bool(getattr(settings, "admin_read_only", False))
        admin_mutations_enabled = bool(
            getattr(settings, "admin_mutations_enabled", False)
        )
        kb_seed_path = _setting_text(settings, "kb_seed_path")
        if not admin_read_only:
            if runtime_role != "ml":
                errors.append(
                    "production admin mutations may run only in the ML runtime"
                )
            if not admin_mutations_enabled:
                errors.append(
                    "ADMIN_MUTATIONS_ENABLED must be enabled when "
                    "ADMIN_READ_ONLY is disabled"
                )
            if kb_seed_path != PRODUCTION_ADMIN_KB_SEED_PATH:
                errors.append(
                    "KB_SEED_PATH must use the isolated private admin workspace "
                    "when production admin mutations are enabled"
                )
        elif admin_mutations_enabled:
            errors.append(
                "ADMIN_MUTATIONS_ENABLED must be disabled when "
                "ADMIN_READ_ONLY is enabled"
            )
        required_settings = [
            ("RELEASE_GIT_SHA", "release_git_sha"),
            ("API_AUTH_TOKEN", "api_auth_token"),
            ("WEBHOOK_AUTH_TOKEN", "webhook_auth_token"),
            ("ADMIN_AUTH_TOKEN", "admin_auth_token"),
            ("USER_HASH_SECRET", "user_hash_secret"),
            ("QDRANT_API_KEY", "qdrant_api_key"),
        ]
        if runtime_role == "ml":
            required_settings.extend(
                [
                    ("CLOUD_RU_API_KEY", "cloud_ru_api_key"),
                    ("HDE_TRIGGER_PREFIX", "hde_trigger_prefix"),
                    ("HDE_BASE_URL", "hde_base_url"),
                    ("HDE_API_EMAIL", "hde_api_email"),
                    ("HDE_API_KEY", "hde_api_key"),
                    (
                        "HDE_TRANSPORT_EVENT_KEY_SECRET",
                        "hde_transport_event_key_secret",
                    ),
                    (
                        "HDE_TRANSPORT_ENCRYPTION_KEY",
                        "hde_transport_encryption_key",
                    ),
                ]
            )
        for env_name, attribute in required_settings:
            value = _setting_text(settings, attribute)
            if not value:
                errors.append(f"{env_name} is required in production")
            elif _looks_like_placeholder(value):
                errors.append(f"{env_name} still contains a placeholder value")

        release_git_sha = _setting_text(settings, "release_git_sha")
        if not re.fullmatch(r"[0-9a-f]{40}", release_git_sha) or release_git_sha == "0" * 40:
            errors.append("RELEASE_GIT_SHA must be a non-zero full lowercase Git SHA")

        length_checked_secrets = [
            ("API_AUTH_TOKEN", "api_auth_token"),
            ("WEBHOOK_AUTH_TOKEN", "webhook_auth_token"),
            ("ADMIN_AUTH_TOKEN", "admin_auth_token"),
            ("USER_HASH_SECRET", "user_hash_secret"),
            ("QDRANT_API_KEY", "qdrant_api_key"),
        ]
        if runtime_role == "ml":
            length_checked_secrets.extend(
                [
                    (
                        "HDE_TRANSPORT_EVENT_KEY_SECRET",
                        "hde_transport_event_key_secret",
                    ),
                    (
                        "HDE_TRANSPORT_ENCRYPTION_KEY",
                        "hde_transport_encryption_key",
                    ),
                ]
            )
        for env_name, attribute in length_checked_secrets:
            value = _setting_text(settings, attribute)
            if value and len(value) < 32:
                errors.append(f"{env_name} must contain at least 32 characters in production")

        trigger_prefix = (
            _setting_text(settings, "hde_trigger_prefix") if runtime_role == "ml" else ""
        )
        if trigger_prefix and len(trigger_prefix) < 16:
            errors.append("HDE_TRIGGER_PREFIX must contain at least 16 characters in production")

        independent_secret_attributes = [
            "api_auth_token",
            "webhook_auth_token",
            "admin_auth_token",
            "user_hash_secret",
            "qdrant_api_key",
        ]
        if runtime_role == "ml":
            independent_secret_attributes.extend(
                [
                    "hde_trigger_prefix",
                    "hde_transport_event_key_secret",
                    "hde_transport_encryption_key",
                ]
            )
        independent_secrets = [
            _setting_text(settings, attribute)
            for attribute in independent_secret_attributes
        ]
        if all(independent_secrets) and len(set(independent_secrets)) != len(
            independent_secrets
        ):
            errors.append(
                "runtime authentication and HDE transport secrets must be independent values"
            )

        postgres_dsn = _setting_text(settings, "postgres_dsn")
        if not postgres_dsn:
            errors.append("POSTGRES_DSN is required in production")
        elif _looks_like_placeholder(postgres_dsn):
            errors.append("POSTGRES_DSN still contains a placeholder value")
        elif "rosmol:rosmol@" in postgres_dsn.casefold():
            errors.append("POSTGRES_DSN must not use the development default credentials")

        redis_url = _setting_text(settings, "redis_url")
        redis_parsed = urlsplit(redis_url)
        if (
            redis_parsed.scheme not in {"redis", "rediss"}
            or redis_parsed.hostname != "redis"
            or redis_parsed.port != 6379
            or redis_parsed.username not in {None, ""}
            or not redis_parsed.password
            or len(redis_parsed.password) < 32
            or redis_parsed.path != "/0"
            or redis_parsed.query
            or redis_parsed.fragment
        ):
            errors.append(
                "REDIS_URL must use a strong password and the internal redis:6379/0 endpoint"
            )
        elif redis_parsed.password in independent_secrets:
            errors.append("Redis and runtime authentication secrets must be independent values")

        if runtime_role == "ml" and not bool(
            getattr(settings, "ml_prewarm_on_startup", False)
        ):
            errors.append("ML_PREWARM_ON_STARTUP must be enabled for the production ML runtime")
        if runtime_role == "ml" and not transport_enabled:
            errors.append("HDE_TRANSPORT_ENABLED must be enabled for the production ML runtime")
        if runtime_role == "ml":
            https_proxy = _setting_text(settings, "https_proxy")
            if https_proxy != "http://runtime-egress-proxy:3128":
                errors.append(
                    "HTTPS_PROXY must use the isolated runtime-egress-proxy in production"
                )
            cloud_endpoint = urlsplit(
                _setting_text(settings, "cloud_ru_chat_completions_url")
            )
            if (
                (cloud_endpoint.hostname or "").casefold()
                != "foundation-models.api.cloud.ru"
                or cloud_endpoint.path != "/v1/chat/completions"
            ):
                errors.append(
                    "CLOUD_RU_CHAT_COMPLETIONS_URL must match the reviewed Cloud.ru endpoint"
                )
            hde_endpoint = urlsplit(_setting_text(settings, "hde_base_url"))
            hde_host = (hde_endpoint.hostname or "").casefold()
            if (
                hde_host != "rosmolodezh.helpdeskeddy.com"
                or hde_endpoint.path not in {"", "/"}
            ):
                errors.append("HDE_BASE_URL must match the reviewed HDE tenant endpoint")
            yonote_enabled = bool(getattr(settings, "yonote_sync_enabled", False))
            yonote_token = _setting_text(settings, "yonote_api_token")
            if yonote_enabled:
                if not yonote_token:
                    errors.append(
                        "YONOTE_API_TOKEN is required when Yonote sync is enabled"
                    )
                elif _looks_like_placeholder(yonote_token):
                    errors.append("YONOTE_API_TOKEN still contains a placeholder value")
                yonote_mode = _setting_text(settings, "yonote_sync_mode")
                if yonote_mode != "manual":
                    errors.append("YONOTE_SYNC_MODE must equal manual in production")
                yonote_collection_names = _setting_text(
                    settings,
                    "yonote_collection_names",
                )
                delimiter = ";" if ";" in yonote_collection_names else "|"
                if not any(
                    item.strip() for item in yonote_collection_names.split(delimiter)
                ):
                    errors.append(
                        "YONOTE_COLLECTION_NAMES must contain at least one collection"
                    )
                yonote_endpoint = urlsplit(_setting_text(settings, "yonote_base_url"))
                yonote_host = (yonote_endpoint.hostname or "").casefold()
                if (
                    yonote_endpoint.scheme.casefold() != "https"
                    or yonote_host != "rossmol.yonote.ru"
                    or yonote_endpoint.path not in {"", "/"}
                    or yonote_endpoint.query
                    or yonote_endpoint.fragment
                ):
                    errors.append(
                        "YONOTE_BASE_URL must match the reviewed read-only Yonote endpoint"
                    )
            elif yonote_token:
                errors.append(
                    "YONOTE_API_TOKEN must be empty when Yonote sync is disabled"
                )
        if runtime_role == "api":
            forbidden_provider_settings = (
                "cloud_ru_api_key",
                "hde_trigger_prefix",
                "hde_base_url",
                "hde_api_email",
                "hde_api_key",
                "hde_bot_user_id",
                "hde_transport_event_key_secret",
                "hde_transport_encryption_key",
                "yonote_api_token",
                "yonote_sync_enabled",
            )
            if any(_setting_text(settings, name) for name in forbidden_provider_settings):
                errors.append(
                    "provider and HDE transport secrets must not be configured in the API runtime"
                )

    if transport_enabled:
        if runtime_role != "ml":
            errors.append("HDE transport may run only in the ML runtime")
        event_secret = _setting_text(settings, "hde_transport_event_key_secret")
        encryption_key = _setting_text(settings, "hde_transport_encryption_key")
        if len(event_secret) < 32:
            errors.append("HDE_TRANSPORT_EVENT_KEY_SECRET must contain at least 32 characters")
        if len(encryption_key) < 32:
            errors.append("HDE_TRANSPORT_ENCRYPTION_KEY must contain at least 32 characters")
        if event_secret and event_secret == encryption_key:
            errors.append("HDE transport event and encryption secrets must be independent")
        request_timeout = float(getattr(settings, "request_timeout_seconds", 45.0) or 45.0)
        session_wait = max(90.0, request_timeout + 30.0)
        inbox_window = session_wait + request_timeout + 30.0
        hde_timeout = float(
            getattr(settings, "hde_request_timeout_seconds", 20.0) or 20.0
        )
        minimum_lease = max(inbox_window, hde_timeout + 30.0)
        lease_timeout = float(
            getattr(settings, "hde_transport_lease_timeout_seconds", 420.0) or 0.0
        )
        recovery_interval = float(
            getattr(settings, "hde_transport_recovery_interval_seconds", 30.0) or 0.0
        )
        shutdown_timeout = float(
            getattr(settings, "hde_transport_shutdown_timeout_seconds", 420.0) or 0.0
        )
        if lease_timeout <= minimum_lease:
            errors.append(
                "HDE_TRANSPORT_LEASE_TIMEOUT_SECONDS must exceed session wait plus "
                "REQUEST_TIMEOUT_SECONDS and safety margin"
            )
        if recovery_interval * 3 > lease_timeout:
            errors.append(
                "HDE_TRANSPORT_RECOVERY_INTERVAL_SECONDS must not exceed one third of lease"
            )
        if shutdown_timeout <= minimum_lease:
            errors.append(
                "HDE_TRANSPORT_SHUTDOWN_TIMEOUT_SECONDS must exceed the maximum inbox turn window"
            )

    if app_env not in {"local", "test"}:
        _append_https_url_error(
            errors,
            "CLOUD_RU_CHAT_COMPLETIONS_URL",
            _setting_text(settings, "cloud_ru_chat_completions_url"),
            active=bool(_setting_text(settings, "cloud_ru_api_key"))
            or (app_env == "production" and runtime_role == "ml"),
        )
        hde_active = (app_env == "production" and runtime_role == "ml") or any(
            _setting_text(settings, attribute)
            for attribute in ("hde_base_url", "hde_api_email", "hde_api_key")
        )
        _append_https_url_error(
            errors,
            "HDE_BASE_URL",
            _setting_text(settings, "hde_base_url"),
            active=hde_active,
        )
        yonote_active = bool(getattr(settings, "yonote_sync_enabled", False)) or bool(
            _setting_text(settings, "yonote_api_token")
        )
        _append_https_url_error(
            errors,
            "YONOTE_BASE_URL",
            _setting_text(settings, "yonote_base_url"),
            active=yonote_active,
        )

    if errors:
        raise RuntimeError("Invalid runtime security configuration: " + "; ".join(errors))


def _setting_text(settings: Any, attribute: str) -> str:
    return str(getattr(settings, attribute, "") or "").strip()


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return any(
        marker in normalized
        for marker in ("replace-with", "example.test", "your-", "change-me", "changeme")
    )


def _append_https_url_error(
    errors: list[str],
    env_name: str,
    value: str,
    *,
    active: bool,
) -> None:
    if not active:
        return
    if not value:
        errors.append(f"{env_name} is required when its integration is active")
        return
    if _looks_like_placeholder(value):
        errors.append(f"{env_name} still contains a placeholder value")
        return
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        errors.append(f"{env_name} must use an absolute https:// URL")
        return
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        errors.append(f"{env_name} must use an absolute https:// URL")
    elif parsed.username is not None or parsed.password is not None:
        errors.append(f"{env_name} must not contain embedded credentials")


class AskPayload(BaseModel):
    user_id: str = Field(default="local", min_length=1, max_length=200)
    channel: Channel = Channel.API
    text: str = Field(min_length=1, max_length=4000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    forum_context: str | None = Field(default=None, max_length=200)

    @field_validator("user_id", "text")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


class AdminChunkUpdate(BaseModel):
    status: str | None = None
    text_clean: str | None = Field(default=None, max_length=20000)
    reindex: bool = True


class AdminLoginPayload(BaseModel):
    token: str = Field(min_length=1, max_length=10000)


class AdminQualityCheckPayload(BaseModel):
    include_latest_eval_report: bool = True


class AdminYonoteSyncPayload(BaseModel):
    limit_documents: int | None = Field(default=None, ge=1, le=500)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    checks: dict[str, str] = {}
    hde_transport_counts: dict[str, int] | None = None
    runtime_settings = getattr(request.app.state, "runtime_settings", None) or get_settings()

    try:
        _validate_runtime_security(runtime_settings)
        runtime_config = getattr(request.app.state, "runtime_config", None)
        if runtime_config is not None and runtime_config.get("status") != "ok":
            raise RuntimeError("runtime configuration was not accepted at startup")
        checks["config"] = "ok"
    except Exception as exc:
        checks["config"] = f"error: {type(exc).__name__}"

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"

    try:
        value = await request.app.state.pg_pool.fetchval("select 1")
        checks["postgres"] = "ok" if value == 1 else "error: unexpected result"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"

    try:
        collection_name = str(
            getattr(runtime_settings, "qdrant_knowledge_collection", "knowledge_base")
        )
        count_result = await request.app.state.qdrant.count(
            collection_name=collection_name,
            exact=True,
        )
        actual_count = int(count_result.count)
        kb_manifest = getattr(request.app.state, "kb_manifest", None) or {}
        expected_count = int(kb_manifest.get("published_records", 0) or 0)
        if actual_count <= 0:
            checks["knowledge_base"] = "error: collection is empty"
        elif expected_count and actual_count != expected_count:
            checks["knowledge_base"] = (
                f"error: indexed={actual_count}, expected={expected_count}"
            )
        else:
            checks["knowledge_base"] = "ok"
    except Exception as exc:
        checks["knowledge_base"] = f"error: {type(exc).__name__}"

    ml_prewarm = getattr(request.app.state, "ml_prewarm", None)
    runtime_role = str(getattr(runtime_settings, "runtime_role", "api") or "api")
    if runtime_role == "ml" and not (ml_prewarm and ml_prewarm.get("enabled")):
        checks["ml_prewarm"] = "error: disabled for ML runtime"
    elif ml_prewarm and ml_prewarm.get("enabled"):
        if ml_prewarm.get("status") == "ok":
            checks["ml_prewarm"] = "ok"
        else:
            error = ml_prewarm.get("error") or ml_prewarm.get("status") or "unknown"
            checks["ml_prewarm"] = f"error: {error}"

    transport_enabled = bool(getattr(runtime_settings, "hde_transport_enabled", False))
    if runtime_role == "ml" and transport_enabled:
        worker = getattr(request.app.state, "hde_transport_worker", None)
        repository = getattr(request.app.state, "hde_transport_repository", None)
        if worker is None or not worker.is_running:
            checks["hde_transport"] = "error: workers not running"
        elif repository is None:
            checks["hde_transport"] = "error: repository unavailable"
        else:
            try:
                queue_counts = await repository.get_queue_counts()
                hde_transport_counts = queue_counts.as_dict()
                dead_letters = (
                    queue_counts.inbox_dead_letter + queue_counts.outbox_dead_letter
                )
                queue_stale_after = float(
                    getattr(
                        runtime_settings,
                        "hde_transport_queue_stale_after_seconds",
                        900.0,
                    )
                    or 900.0
                )
                lease_timeout = float(
                    getattr(
                        runtime_settings,
                        "hde_transport_lease_timeout_seconds",
                        420.0,
                    )
                    or 420.0
                )
                stale_queue_names = [
                    name
                    for name, age, limit in (
                        (
                            "inbox_ready",
                            queue_counts.inbox_oldest_ready_age_seconds,
                            queue_stale_after,
                        ),
                        (
                            "inbox_processing",
                            queue_counts.inbox_oldest_processing_age_seconds,
                            lease_timeout,
                        ),
                        (
                            "outbox_ready",
                            queue_counts.outbox_oldest_ready_age_seconds,
                            queue_stale_after,
                        ),
                        (
                            "outbox_sending",
                            queue_counts.outbox_oldest_sending_age_seconds,
                            lease_timeout,
                        ),
                    )
                    if age is not None and age > limit
                ]
                if dead_letters:
                    checks["hde_transport"] = (
                        "error: dead_letter/HOL "
                        f"inbox={queue_counts.inbox_dead_letter}, "
                        f"outbox={queue_counts.outbox_dead_letter}"
                    )
                elif stale_queue_names:
                    checks["hde_transport"] = (
                        "error: stale queue/HOL " + ",".join(stale_queue_names)
                    )
                else:
                    checks["hde_transport"] = "ok"
            except Exception as exc:
                checks["hde_transport"] = f"error: {type(exc).__name__}"

    if any(status != "ok" for status in checks.values()):
        detail: dict[str, Any] = {"status": "degraded", "checks": checks}
        if hde_transport_counts is not None:
            detail["hde_transport_counts"] = hde_transport_counts
        raise HTTPException(status_code=503, detail=detail)
    response: dict[str, Any] = {
        "status": "ready",
        "release_git_sha": _setting_text(runtime_settings, "release_git_sha") or None,
        "checks": checks,
    }
    if hde_transport_counts is not None:
        response["hde_transport_counts"] = hde_transport_counts
    return response


@app.post("/ask")
async def ask(payload: AskPayload, request: Request) -> dict[str, Any]:
    _require_optional_secret(request, getattr(get_settings(), "api_auth_token", ""), "x-api-key")
    message = IncomingMessage(
        user_id=payload.user_id,
        channel=payload.channel,
        text=payload.text,
        attachments=payload.attachments,
        forum_context=payload.forum_context,
        eval_run_id=_optional_trace_identifier(request.headers.get("x-eval-run-id")),
        eval_case_id=_optional_trace_identifier(request.headers.get("x-eval-case-id")),
    )
    if _should_bypass_cache(request):
        response = await process_message(message, request.app, bypass_cache=True)
    else:
        response = await process_message(message, request.app)
    return {"request_id": str(message.request_id), "response": response}


def _optional_trace_identifier(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized[:200] or None


@app.get("/admin/kb", response_class=HTMLResponse)
async def admin_kb_page() -> HTMLResponse:
    _require_admin_enabled()
    settings = get_settings()
    return HTMLResponse(
        content=ui.render_admin_kb_html(
            admin_read_only=bool(getattr(settings, "admin_read_only", False)),
            yonote_sync_enabled=bool(
                getattr(settings, "yonote_sync_enabled", False)
            ),
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/kb/login")
async def admin_kb_login(
    payload: AdminLoginPayload,
    request: Request,
    response: Response,
) -> dict[str, bool]:
    expected = _require_admin_enabled()
    if not compare_digest(payload.token.strip(), expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        _make_admin_session_cookie(expected),
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        path="/admin/kb",
    )
    return {"ok": True}


@app.post("/admin/kb/logout")
async def admin_kb_logout(response: Response) -> dict[str, bool]:
    _require_admin_enabled()
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/admin/kb")
    return {"ok": True}


@app.get("/admin/kb/chunks")
async def admin_list_kb_chunks(
    request: Request,
    status: str | None = None,
    category: str | None = None,
    forum: str | None = None,
    source_type: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    _require_admin_secret(request)
    if status and status not in kb_store.VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")
    return await asyncio.to_thread(
        kb_store.list_chunks,
        _kb_seed_path(),
        status=status,
        category=category,
        forum=forum,
        source_type=source_type,
        q=q,
        limit=limit,
        offset=offset,
    )


@app.post("/admin/kb/validate")
async def admin_validate_kb_seed(request: Request) -> dict[str, Any]:
    _require_admin_secret(request)
    try:
        return await asyncio.to_thread(kb_store.validate_seed, _kb_seed_path())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/admin/kb/eval-report")
async def admin_get_kb_eval_report(request: Request) -> dict[str, Any]:
    _require_admin_secret(request)
    report_path = _admin_quality_report_path()
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Quality report not found")
    try:
        return await asyncio.to_thread(kb_store.load_quality_report, report_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/admin/kb/ops-report")
async def admin_get_ops_report(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    _require_admin_secret(request)
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Postgres pool is not initialized")
    async with pool.acquire() as conn:
        return await build_trace_report(conn, days)


@app.post("/admin/kb/quality-check")
async def admin_run_kb_quality_check(
    payload: AdminQualityCheckPayload,
    request: Request,
) -> dict[str, Any]:
    _require_admin_secret(request)
    report_path = _admin_quality_report_path()
    if not payload.include_latest_eval_report:
        return {"validation": await asyncio.to_thread(kb_store.validate_seed, _kb_seed_path())}
    try:
        return await asyncio.to_thread(
            kb_store.build_quality_check,
            _kb_seed_path(),
            report_path=report_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/admin/kb/yonote/preview")
async def admin_preview_yonote_sync(
    payload: AdminYonoteSyncPayload,
    request: Request,
) -> dict[str, Any]:
    _require_admin_secret(request)
    _require_yonote_sync_enabled()
    settings = get_settings()
    if (
        _setting_text(settings, "app_env").casefold() == "production"
        and payload.limit_documents is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="limit_documents is not allowed for production Yonote preview",
        )
    if not _YONOTE_SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Yonote sync is already running")
    try:
        return await asyncio.to_thread(
            _run_with_yonote_sync_lock,
            preview_yonote_sync,
            _kb_seed_path(),
            settings,
            limit_documents=payload.limit_documents,
        )
    except YonoteSyncConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YonoteOperationTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                "Чтение Yonote не завершилось в безопасный срок. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteDataTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                "Объём данных Yonote превышает безопасный лимит чтения. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteApiError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось прочитать данные Yonote. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/admin/kb/yonote/apply")
async def admin_apply_yonote_sync(
    payload: AdminYonoteSyncPayload,
    request: Request,
) -> dict[str, Any]:
    _require_admin_secret(request)
    _require_yonote_sync_enabled()
    _require_admin_writable()
    if not _YONOTE_SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Yonote sync is already running")
    if not _ADMIN_MUTATION_LOCK.acquire(blocking=False):
        _YONOTE_SYNC_LOCK.release()
        raise HTTPException(status_code=409, detail="Admin mutation is already running")
    try:
        return await asyncio.to_thread(
            apply_yonote_sync,
            _kb_seed_path(),
            get_settings(),
            limit_documents=payload.limit_documents,
        )
    except YonoteSyncConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YonoteOperationTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                "Чтение Yonote не завершилось в безопасный срок. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteDataTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                "Объём данных Yonote превышает безопасный лимит чтения. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteApiError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось прочитать данные Yonote. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        _ADMIN_MUTATION_LOCK.release()
        _YONOTE_SYNC_LOCK.release()


@app.post("/admin/kb/yonote/database-statistics")
async def admin_count_yonote_database(request: Request) -> dict[str, Any]:
    _require_admin_secret(request)
    _require_yonote_sync_enabled()
    if not _YONOTE_SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Yonote sync is already running")
    try:
        return await asyncio.to_thread(
            _run_with_yonote_sync_lock,
            count_yonote_database,
            get_settings(),
        )
    except YonoteSyncConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YonoteOperationTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                "Чтение Yonote не завершилось в безопасный срок. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteDataTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                "Объём данных Yonote превышает безопасный лимит чтения. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteApiError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось прочитать данные Yonote. "
                "База бота и индекс не изменялись."
            ),
        ) from exc


@app.post("/admin/kb/yonote/database-export")
async def admin_export_yonote_database(request: Request) -> Response:
    _require_admin_secret(request)
    _require_yonote_sync_enabled()
    if not _YONOTE_SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Yonote sync is already running")
    try:
        rendered = await asyncio.to_thread(
            _run_with_yonote_sync_lock,
            export_yonote_database,
            get_settings(),
        )
    except YonoteSyncConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YonoteOperationTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                "Чтение Yonote не завершилось в безопасный срок. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteDataTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                "Объём данных Yonote превышает безопасный лимит чтения. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteApiError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось прочитать данные Yonote. "
                "База бота и индекс не изменялись."
            ),
        ) from exc
    except YonoteDatabaseExportTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                "Текстовая выгрузка Yonote превышает безопасный лимит размера. "
                "База бота и индекс не изменялись."
            ),
        ) from exc

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        content=rendered,
        media_type="text/plain",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f'attachment; filename="yonote-database-{stamp}.txt"'
            ),
        },
    )


@app.get("/admin/kb/chunks/{chunk_id}")
async def admin_get_kb_chunk(chunk_id: str, request: Request) -> dict[str, Any]:
    _require_admin_secret(request)
    record = await asyncio.to_thread(kb_store.get_chunk, _kb_seed_path(), chunk_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return record


@app.patch("/admin/kb/chunks/{chunk_id}")
async def admin_update_kb_chunk(
    chunk_id: str,
    payload: AdminChunkUpdate,
    request: Request,
) -> dict[str, Any]:
    _require_admin_secret(request)
    _require_admin_writable()
    if not _ADMIN_MUTATION_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Admin mutation is already running")
    try:
        updated = await asyncio.to_thread(
            kb_store.update_chunk,
            _kb_seed_path(),
            chunk_id,
            status=payload.status,
            text_clean=payload.text_clean,
        )
        reindex_result = None
        if payload.reindex:
            try:
                reindex_result = await _admin_reindex_record(request, updated)
            except HTTPException as exc:
                reindex_result = _admin_reindex_error(updated, exc)
            except Exception as exc:
                reindex_result = _admin_reindex_unexpected_error(updated, exc)
        return {"record": updated, "reindex": reindex_result}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Chunk not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        _ADMIN_MUTATION_LOCK.release()


@app.post("/admin/kb/chunks/{chunk_id}/reindex")
async def admin_reindex_kb_chunk(chunk_id: str, request: Request) -> dict[str, Any]:
    _require_admin_secret(request)
    _require_admin_writable()
    if not _ADMIN_MUTATION_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Admin mutation is already running")
    try:
        record = await asyncio.to_thread(kb_store.get_chunk, _kb_seed_path(), chunk_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Chunk not found")
        return await _admin_reindex_record(request, record)
    finally:
        _ADMIN_MUTATION_LOCK.release()


@app.get("/admin/kb/chunks/{chunk_id}/eval-cases")
async def admin_get_kb_chunk_eval_cases(
    chunk_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require_admin_secret(request)
    if await asyncio.to_thread(kb_store.get_chunk, _kb_seed_path(), chunk_id) is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return await asyncio.to_thread(
        kb_store.find_related_eval_cases,
        _admin_eval_cases_dir(),
        chunk_id,
        limit=limit,
    )


@app.post("/webhook/vk")
async def vk_webhook(request: Request) -> dict[str, bool]:
    _require_direct_channel_webhook_enabled()
    _require_optional_secret(
        request,
        getattr(get_settings(), "webhook_auth_token", ""),
        "x-webhook-secret",
    )
    message = vk_adapter.parse(await request.json())
    response = await process_message(message, request.app)
    await vk_adapter.send(message.user_id, response)
    return {"ok": True}


@app.post("/webhook/max")
async def max_webhook(request: Request) -> dict[str, bool]:
    _require_direct_channel_webhook_enabled()
    _require_optional_secret(
        request,
        getattr(get_settings(), "webhook_auth_token", ""),
        "x-webhook-secret",
    )
    message = max_adapter.parse(await request.json())
    response = await process_message(message, request.app)
    await max_adapter.send(message.user_id, response)
    return {"ok": True}


@app.post("/webhook/hde")
async def hde_webhook(request: Request) -> dict[str, bool]:
    _require_optional_secret(
        request,
        getattr(get_settings(), "webhook_auth_token", ""),
        "x-webhook-secret",
    )
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid HDE payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid HDE payload")
    try:
        message = hde_adapter.parse(payload)
    except HDEPayloadError as exc:
        logger.warning("hde_payload_rejected", reason=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    repository = getattr(request.app.state, "hde_transport_repository", None)
    if repository is None:
        logger.error("hde_transport_unavailable", reason="repository_not_started")
        raise HTTPException(status_code=503, detail="HDE transport unavailable")
    try:
        masked_text, _ = request.app.state.pii_masker.mask(message.text)
        masked_forum_context: str | None = None
        if message.forum_context:
            masked_forum_context, _ = request.app.state.pii_masker.mask(
                message.forum_context
            )
    except PIIMaskingUnavailable as exc:
        logger.error(
            "hde_pii_masking_unavailable",
            request_id=str(message.request_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="PII masking unavailable") from exc
    try:
        receipt = await repository.enqueue_inbox(
            message,
            masked_text=masked_text,
            masked_forum_context=masked_forum_context,
        )
    except (HDEStableEventRequired, HDETransportValidationError) as exc:
        logger.warning(
            "hde_durable_payload_rejected",
            request_id=str(message.request_id),
            reason=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "hde_durable_enqueue_failed",
            request_id=str(message.request_id),
            ticket_id_hash=hash_user_id(message.channel.value, message.user_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="HDE transport unavailable") from exc
    logger.info(
        "hde_durable_event_committed",
        request_id=str(receipt.request_id),
        event_key=receipt.event_key,
        duplicate=not receipt.created,
    )
    return {"ok": True}


async def _prewarm_ml_runtime(fastapi_app: FastAPI, settings: Any) -> None:
    started_at = perf_counter()
    fastapi_app.state.ml_prewarm = {
        "enabled": True,
        "status": "warming",
    }
    try:
        await asyncio.wait_for(
            _run_ml_prewarm(fastapi_app),
            timeout=float(getattr(settings, "ml_prewarm_timeout_seconds", 120.0)),
        )
    except Exception as exc:
        latency_ms = int((perf_counter() - started_at) * 1000)
        fastapi_app.state.ml_prewarm = {
            "enabled": True,
            "status": "error",
            "error": type(exc).__name__,
            "latency_ms": latency_ms,
        }
        logger.warning("ml_prewarm_failed", error=str(exc), latency_ms=latency_ms)
        return

    latency_ms = int((perf_counter() - started_at) * 1000)
    fastapi_app.state.ml_prewarm = {
        "enabled": True,
        "status": "ok",
        "latency_ms": latency_ms,
    }
    logger.info("ml_prewarm_ok", latency_ms=latency_ms)


async def _run_ml_prewarm(fastapi_app: FastAPI) -> None:
    query = "регистрация на форум"
    masked_probe, pii_mapping = await asyncio.to_thread(
        fastapi_app.state.pii_masker.mask,
        "Иван Иванов спрашивает о регистрации на форум.",
    )
    if not pii_mapping.get("name") or "Иван Иванов" in masked_probe:
        raise PIIMaskingUnavailable("pii_ner_prewarm_probe_failed")
    await asyncio.to_thread(fastapi_app.state.embedder.encode, query)
    warm_chunk = Chunk(
        chunk_id="ml_prewarm",
        text="Регистрация на форум Росмолодёжи.",
        metadata={"chunk_id": "ml_prewarm"},
        score=1.0,
    )
    await asyncio.to_thread(fastapi_app.state.reranker.rerank, query, [warm_chunk], 1)


async def _admin_reindex_record(request: Request, record: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    try:
        result = await kb_index.upsert_chunk(
            request.app.state.qdrant,
            request.app.state.embedder,
            collection_name=settings.qdrant_knowledge_collection,
            record_payload=record,
        )
    except MLDependencyError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Для немедленного обновления индекса нужен ML-сервис. "
                "Открой админку через app-ml или запусти отдельную индексацию."
            ),
        ) from exc

    source_type = str(record.get("source_type") or "").strip()
    invalidate_keyword_cache = getattr(
        request.app.state.retriever,
        "invalidate_keyword_cache",
        None,
    )
    if callable(invalidate_keyword_cache):
        try:
            invalidate_keyword_cache(source_type or None)
            result["keyword_cache_invalidated_source"] = source_type or "all"
        except Exception as exc:
            logger.warning(
                "admin_keyword_cache_invalidation_failed",
                chunk_id=record.get("chunk_id"),
                source_type=source_type or None,
                error=str(exc),
            )
            result["keyword_cache_invalidation_warning"] = type(exc).__name__

    forum = str(record.get("forum_normalized") or "").strip()
    try:
        await request.app.state.semantic_cache.invalidate_forum(forum or None)
        result["cache_invalidated_forum"] = forum or "global"
    except Exception as exc:
        logger.warning(
            "admin_cache_invalidation_failed",
            chunk_id=record.get("chunk_id"),
            forum=forum or None,
            error=str(exc),
        )
        result["cache_invalidation_warning"] = type(exc).__name__
    return result


def _admin_reindex_error(record: dict[str, Any], exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    logger.warning(
        "admin_reindex_after_save_failed",
        chunk_id=record.get("chunk_id"),
        status_code=exc.status_code,
        detail=detail,
    )
    return {
        "ok": False,
        "chunk_id": record.get("chunk_id"),
        "status_code": exc.status_code,
        "error": detail,
    }


def _admin_reindex_unexpected_error(record: dict[str, Any], exc: Exception) -> dict[str, Any]:
    logger.exception(
        "admin_reindex_after_save_unexpected_failed",
        chunk_id=record.get("chunk_id"),
        error_type=type(exc).__name__,
    )
    return {
        "ok": False,
        "chunk_id": record.get("chunk_id"),
        "status_code": 500,
        "error": f"Ошибка обновления индекса: {type(exc).__name__}",
    }


def _require_optional_secret(
    request: Request,
    expected_secret: str | None,
    header_name: str,
) -> None:
    expected = (expected_secret or "").strip()
    if not expected:
        return

    provided = (request.headers.get(header_name) or "").strip()
    if not provided:
        authorization = (request.headers.get("authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()

    if not provided or not compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _require_direct_channel_webhook_enabled() -> None:
    app_env = str(getattr(get_settings(), "app_env", "local") or "local").strip().casefold()
    if app_env not in {"local", "test"}:
        raise HTTPException(status_code=404, detail="Not Found")


def _require_admin_secret(request: Request) -> None:
    expected = _require_admin_enabled()
    if _has_valid_admin_session(request, expected):
        return
    _require_optional_secret(request, expected, "x-admin-token")


def _require_admin_enabled() -> str:
    expected = (getattr(get_settings(), "admin_auth_token", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API is disabled")
    return expected


def _require_admin_writable() -> None:
    settings = get_settings()
    admin_read_only = bool(getattr(settings, "admin_read_only", False))
    app_env = _setting_text(settings, "app_env").casefold() or "local"
    mutation_capability_missing = (
        app_env == "production"
        and not bool(getattr(settings, "admin_mutations_enabled", False))
    )
    if admin_read_only or mutation_capability_missing:
        raise HTTPException(
            status_code=403,
            detail="Admin mutations are disabled in this runtime",
        )


def _require_yonote_sync_enabled() -> None:
    settings = get_settings()
    if not bool(getattr(settings, "yonote_sync_enabled", False)):
        raise HTTPException(status_code=503, detail="Yonote sync is disabled")
    if not _setting_text(settings, "yonote_api_token"):
        raise HTTPException(status_code=503, detail="YONOTE_API_TOKEN is not configured")


def _make_admin_session_cookie(secret: str) -> str:
    issued_at = str(int(time()))
    signature = _admin_session_signature(secret, issued_at)
    return f"{issued_at}.{signature}"


def _has_valid_admin_session(request: Request, secret: str) -> bool:
    raw_cookie = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
    if not raw_cookie or "." not in raw_cookie:
        return False

    issued_at, provided_signature = raw_cookie.split(".", 1)
    try:
        issued_at_int = int(issued_at)
    except ValueError:
        return False

    if issued_at_int < int(time()) - ADMIN_SESSION_TTL_SECONDS:
        return False

    expected_signature = _admin_session_signature(secret, issued_at)
    return compare_digest(provided_signature, expected_signature)


def _admin_session_signature(secret: str, issued_at: str) -> str:
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=issued_at.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _is_https_request(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _kb_seed_path() -> Path:
    return Path(getattr(get_settings(), "kb_seed_path", "data/knowledge_base_seed.json"))


def _admin_eval_cases_dir() -> Path:
    return Path(getattr(get_settings(), "admin_eval_cases_dir", "eval/cases"))


def _admin_quality_report_path() -> Path:
    default = "reports/presentation_quality/presentation_quality_report.json"
    return Path(getattr(get_settings(), "admin_quality_report_path", default))


def _should_bypass_cache(request: Request) -> bool:
    requested = (request.headers.get("x-bypass-cache") or "").strip().casefold()
    if requested not in {"1", "true", "yes"}:
        return False
    return get_settings().app_env == "local" or _is_loopback_request(request)


def _is_loopback_request(request: Request) -> bool:
    host = (request.url.hostname or "").strip().casefold()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if not request.client:
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return False


async def process_message(
    message: IncomingMessage,
    fastapi_app: FastAPI,
    *,
    bypass_cache: bool = False,
) -> str:
    serializer = getattr(fastapi_app.state.sessions, "serialized", None)
    if serializer is None:
        return await _process_message_unlocked(
            message,
            fastapi_app,
            bypass_cache=bypass_cache,
        )
    async with serializer(message.channel.value, message.user_id):
        return await _process_message_unlocked(
            message,
            fastapi_app,
            bypass_cache=bypass_cache,
        )


async def _process_message_unlocked(
    message: IncomingMessage,
    fastapi_app: FastAPI,
    *,
    bypass_cache: bool = False,
) -> str:
    started_at = perf_counter()
    settings = get_settings()
    user_id_hash = hash_user_id(message.channel.value, message.user_id)
    trace_identifiers = {
        "upstream_event_id": message.upstream_event_id,
        "upstream_event_id_source": message.upstream_event_id_source,
        "eval_run_id": message.eval_run_id,
        "eval_case_id": message.eval_case_id,
    }

    if not await fastapi_app.state.rate_limiter.check(message.user_id, message.channel.value):
        response = (
            "Слишком много сообщений за короткое время. "
            "Попробуй ещё раз через несколько минут."
        )
        await _safe_log(
            fastapi_app,
            {
                **trace_identifiers,
                "request_id": message.request_id,
                "channel": message.channel.value,
                "user_id_hash": user_id_hash,
                "message_masked": "[rate_limited_not_processed]",
                "final_response": response,
                "was_escalated": False,
                "escalation_reason": "rate_limited",
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        return response

    is_safe, safety_reason = safety.check(message.text)
    masked_text, _ = fastapi_app.state.pii_masker.mask(message.text)
    operator_requested = is_operator_request(message.text)
    session = await fastapi_app.state.sessions.get_or_create(
        message.channel.value,
        message.user_id,
    )

    if _is_attachment_only_message(message):
        response = ATTACHMENT_ONLY_RESPONSE
        await fastapi_app.state.sessions.append_turn(
            session,
            masked_text or "[attachment_only]",
            response,
        )
        await _safe_log(
            fastapi_app,
            {
                **trace_identifiers,
                "request_id": message.request_id,
                "channel": message.channel.value,
                "user_id_hash": user_id_hash,
                "message_masked": masked_text or "[attachment_only]",
                "final_response": response,
                "should_escalate": True,
                "escalation_reason": ATTACHMENT_ONLY_REASON,
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        return response

    if not is_safe:
        response = OPERATOR_TRANSFER_RESPONSE
        await fastapi_app.state.sessions.append_turn(session, masked_text, response)
        await _safe_log(
            fastapi_app,
            {
                **trace_identifiers,
                "request_id": message.request_id,
                "channel": message.channel.value,
                "user_id_hash": user_id_hash,
                "message_masked": masked_text,
                "final_response": response,
                "should_escalate": True,
                "escalation_reason": safety_reason,
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        return response

    if (
        profanity.check(message.text)
        and not _has_actionable_support_context(message.text)
        and not operator_requested
    ):
        response = (
            "Я не поддерживаю оскорбления и не вступаю в споры. "
            "Я отвечаю на вопросы по мероприятиям, форумам, ФГАИС «Молодёжь России» "
            "и грантам Росмолодёжи. Задай, пожалуйста, вопрос по этим темам."
        )
        await fastapi_app.state.sessions.append_turn(session, masked_text, response)
        await _safe_log(
            fastapi_app,
            {
                **trace_identifiers,
                "request_id": message.request_id,
                "channel": message.channel.value,
                "user_id_hash": user_id_hash,
                "message_masked": masked_text,
                "final_response": response,
                "should_escalate": False,
                "escalation_reason": None,
                "interaction_reason": "profanity",
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        return response

    if operator_requested:
        response = OPERATOR_TRANSFER_RESPONSE
        session = await _clear_pending_clarification(fastapi_app, session)
        await fastapi_app.state.sessions.append_turn(session, masked_text, response)
        await _safe_log(
            fastapi_app,
            {
                **trace_identifiers,
                "request_id": message.request_id,
                "channel": message.channel.value,
                "user_id_hash": user_id_hash,
                "message_masked": masked_text,
                "final_response": response,
                "should_escalate": True,
                "escalation_reason": "operator_requested",
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            },
        )
        return response

    explicit_forum_context = detect_forum_from_text(message.forum_context or "")
    if explicit_forum_context and session.forum_context != explicit_forum_context:
        session = await fastapi_app.state.sessions.update(
            session,
            forum_context=explicit_forum_context,
        )
    graph_message, graph_masked_text, pending_context_applied = (
        _with_pending_clarification_context(
            message.text,
            masked_text,
            session,
        )
    )
    graph_message, graph_masked_text = _with_explicit_forum_context(
        graph_message,
        graph_masked_text,
        explicit_forum_context,
    )
    routing_hint = estimate_routing_hint(graph_masked_text)
    detected_forums = detect_forums_from_text(graph_message)
    detected_forum = detected_forums[0] if len(detected_forums) == 1 else None
    cache_allowed = not is_context_dependent_followup(
        graph_masked_text,
        session,
    ) and not is_safe_offtopic_message(graph_message)
    cache_allowed = cache_allowed and not is_registration_query(graph_masked_text)
    cache_allowed = cache_allowed and not pending_context_applied
    cache_allowed = cache_allowed and len(detected_forums) <= 1

    tracer = Tracer()
    state = {
        **trace_identifiers,
        "request_id": message.request_id,
        "channel": message.channel.value,
        "user_id": message.user_id,
        "user_id_hash": user_id_hash,
        "message": graph_message,
        "message_masked": graph_masked_text,
        "routing_hint": routing_hint.model_dump(),
        "session": session,
        "trace": tracer,
        "llm_client": fastapi_app.state.llm_client,
        "embedder": getattr(fastapi_app.state, "embedder", None),
        "retriever": fastapi_app.state.retriever,
        "reranker": fastapi_app.state.reranker,
        "cache_hit": False,
        "cache_allowed": cache_allowed,
    }

    cached_response = None
    if not bypass_cache and cache_allowed:
        cached_response = await _check_cache(
            fastapi_app,
            graph_masked_text,
            detected_forum or session.forum_context,
        )
    if cached_response:
        response = cached_response.response
        state.update(
            {
                "cache_hit": True,
                "analysis": cached_response.analysis,
                "cited_sources": cached_response.cited_sources,
                "generator_model": cached_response.generator_model,
                "verifier_triggered": cached_response.verifier_triggered,
                "cache_disposition": cached_response.disposition,
                "final_response": response,
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            }
        )
        session, response = await _update_dialog_session(
            fastapi_app,
            session,
            state,
            pending_text=graph_masked_text,
            response=response,
        )
        state["final_response"] = response
        session = await fastapi_app.state.sessions.append_turn(session, masked_text, response)
        await _update_memory(
            fastapi_app,
            user_id_hash,
            message.channel.value,
            state,
            session=session,
        )
        await _safe_log(fastapi_app, state)
        return response

    if not settings.cloud_ru_api_key:
        response = OPERATOR_TRANSFER_RESPONSE
        state.update(
            {
                "final_response": response,
                "should_escalate": True,
                "escalation_reason": "llm_not_configured",
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            }
        )
        await fastapi_app.state.sessions.append_turn(session, masked_text, response)
        await _safe_log(fastapi_app, state)
        return response

    llm_usage_events, llm_usage_token = start_llm_usage_collection()
    try:
        result = await asyncio.wait_for(
            fastapi_app.state.graph.ainvoke(state),
            timeout=float(getattr(settings, "request_timeout_seconds", 45.0)),
        )
    except TimeoutError:
        result = {
            **state,
            "final_response": OPERATOR_TRANSFER_RESPONSE,
            "should_escalate": True,
            "escalation_reason": "request_timeout",
            "error": "request_timeout",
        }
    finally:
        reset_llm_usage_collection(llm_usage_token)
    result.update(summarize_llm_usage(llm_usage_events))
    response = result.get("final_response") or OPERATOR_TRANSFER_RESPONSE
    session, response = await _update_dialog_session(
        fastapi_app,
        session,
        result,
        pending_text=graph_masked_text,
        response=response,
    )
    result["final_response"] = response
    session = await fastapi_app.state.sessions.append_turn(session, masked_text, response)
    result["total_latency_ms"] = int((perf_counter() - started_at) * 1000)
    await _update_memory(
        fastapi_app,
        user_id_hash,
        message.channel.value,
        result,
        session=session,
    )
    if not bypass_cache and cache_allowed:
        await _save_cache(fastapi_app, graph_masked_text, response, result)
    await _safe_log(fastapi_app, result)
    return response


def _with_explicit_forum_context(
    message: str,
    masked_text: str,
    forum_context: str | None,
) -> tuple[str, str]:
    if not forum_context or detect_forum_from_text(message):
        return message, masked_text
    return f"{forum_context}: {message}", f"{forum_context}: {masked_text}"


def _with_pending_clarification_context(
    message: str,
    masked_text: str,
    session: Session,
) -> tuple[str, str, bool]:
    pending = str(session.pending_clarification or "").strip()
    if not pending or not _looks_like_clarification_reply(message):
        return message, masked_text, False
    return (
        f"{pending}\nУточнение пользователя: {message}",
        f"{pending}\nУточнение пользователя: {masked_text}",
        True,
    )


def _looks_like_clarification_reply(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if detect_forum_from_text(text):
        return True
    words = re.findall(r"[а-яёa-z0-9-]+", text.casefold(), flags=re.IGNORECASE)
    normalized = " ".join(words)
    if normalized in {
        "привет",
        "здравствуйте",
        "добрый день",
        "добрый вечер",
        "как дела",
        "спасибо",
        "благодарю",
        "пока",
        "до свидания",
        "до встречи",
    }:
        return False
    if normalized.startswith(
        (
            "это ",
            "речь о ",
            "речь про ",
            "про форум ",
            "про мероприятие ",
            "про грант ",
            "в фгаис ",
        )
    ):
        return True
    if _looks_like_explicit_domain_switch(normalized):
        return False
    return len(words) <= 6


def _looks_like_explicit_domain_switch(normalized: str) -> bool:
    if not any(
        marker in normalized
        for marker in (
            "грант",
            "грантов",
            "грантового отч",
            "грантового соглаш",
            "фгаис",
            "госуслуг",
            "личный кабинет",
            "профил",
        )
    ):
        return False
    return any(
        marker in normalized
        for marker in ("как ", "какой ", "какие ", "где ", "почему ", "что делать")
    )


async def _update_dialog_session(
    fastapi_app: FastAPI,
    session: Session,
    result: dict[str, Any],
    *,
    pending_text: str,
    response: str,
) -> tuple[Session, str]:
    analysis = result.get("analysis")
    if not isinstance(analysis, QueryAnalysis):
        if result.get("should_escalate"):
            return await _clear_pending_clarification(fastapi_app, session), response
        return session, response

    forum = analysis.forum_normalized
    if forum is None and analysis.category not in NON_FORUM_CONTEXT_CATEGORIES:
        forum = session.forum_context
    topics: list[str] = []
    for topic in [
        *analysis.topics,
        *(question.topic for question in analysis.questions),
    ]:
        normalized_topic = str(topic or "").strip()
        if normalized_topic and normalized_topic not in topics:
            topics.append(normalized_topic)
    entities = dict(session.extracted_entities)
    if analysis.category:
        entities["last_category"] = analysis.category
    if topics:
        entities["last_topics"] = topics
    for key, value in analysis.extracted_params.items():
        if value not in (None, "", [], {}):
            entities[str(key)] = value
    if forum:
        entities["forum_context"] = forum
    elif analysis.category in NON_FORUM_CONTEXT_CATEGORIES:
        entities.pop("forum_context", None)

    if _is_actionable_clarification(analysis) and not result.get("should_escalate"):
        attempts = session.clarification_attempts + 1
        clarification_history = list(session.clarification_history)
        clarification = str(analysis.clarification_question or response).strip()
        if clarification and (
            not clarification_history or clarification_history[-1] != clarification
        ):
            clarification_history.append(clarification)
        updated = await fastapi_app.state.sessions.update(
            session,
            forum_context=forum,
            extracted_entities=entities,
            pending_clarification=_limit_pending_context(pending_text),
            clarification_attempts=attempts,
            clarification_history=clarification_history[-50:],
        )
        return updated, response

    updated = await fastapi_app.state.sessions.update(
        session,
        forum_context=forum,
        extracted_entities=entities,
        pending_clarification=None,
        clarification_attempts=0,
    )
    return updated, response


def _is_actionable_clarification(analysis: QueryAnalysis) -> bool:
    if analysis.is_offtopic or not analysis.needs_clarification:
        return False
    prompt = str(analysis.clarification_question or "").casefold()
    return "уточни" in prompt or "что именно хочешь оценить" in prompt


def _limit_pending_context(text: str, limit: int = 4000) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[-limit:]


async def _clear_pending_clarification(
    fastapi_app: FastAPI,
    session: Session,
    *,
    detected_forum: str | None = None,
) -> Session:
    if (
        not session.pending_clarification
        and session.clarification_attempts == 0
        and (not detected_forum or session.forum_context == detected_forum)
    ):
        return session
    return await fastapi_app.state.sessions.update(
        session,
        forum_context=detected_forum or session.forum_context,
        pending_clarification=None,
        clarification_attempts=0,
    )


def _is_attachment_only_message(message: IncomingMessage) -> bool:
    text = str(message.text or "").strip()
    attachments = message.attachments or []
    if attachments and not text:
        return True
    if not text:
        return False

    normalized = text.casefold().replace("ё", "е")
    contains_attachment_marker = bool(
        _ATTACHMENT_FILE_RE.search(normalized) or _ATTACHMENT_WORD_RE.search(normalized)
    )
    if not contains_attachment_marker:
        return False

    cleaned = _ATTACHMENT_FILE_RE.sub(" ", normalized)
    cleaned = _ATTACHMENT_WORD_RE.sub(" ", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    meaningful_words = _MEANINGFUL_WORD_RE.findall(cleaned)
    if attachments:
        return len(meaningful_words) <= 2
    return len(meaningful_words) == 0


def _has_actionable_support_context(text: str) -> bool:
    normalized = str(text or "").casefold().replace("ё", "е")
    has_scope = bool(detect_forum_from_text(text)) or any(
        marker in normalized
        for marker in (
            "форум",
            "мероприят",
            "фестивал",
            "грант",
            "фгаис",
            "молодежь россии",
            "росмолод",
            "заявк",
            "кабинет",
            "профил",
        )
    )
    if not has_scope:
        return False
    return any(
        marker in normalized
        for marker in (
            "как ",
            "где ",
            "когда ",
            "куда ",
            "можно ли",
            "нужно ли",
            "что нужно",
            "что делать",
            "подскаж",
            "расскаж",
            "помог",
            "хочу попасть",
            "хочу участвовать",
            "хочу поучаствовать",
            "хочу подать",
            "зарегистр",
            "регистрац",
            "подать заяв",
            "не работает",
            "не получается",
            "не могу",
            "не груз",
            "не откры",
            "не пуска",
            "не приш",
            "не вижу",
            "пропал",
            "вылет",
            "завис",
            "висит",
            "ошиб",
            "задолб",
            "тупит",
            "фигн",
        )
    )


async def _check_cache(
    fastapi_app: FastAPI,
    query: str,
    forum: str | None,
) -> CachedResponse | None:
    try:
        return await fastapi_app.state.semantic_cache.check(query, forum)
    except Exception as exc:
        logger.warning("semantic_cache_check_failed", error=str(exc))
        return None


async def _save_cache(
    fastapi_app: FastAPI,
    query: str,
    response: str,
    state: dict[str, Any],
) -> None:
    if (
        state.get("should_escalate")
        or state.get("partial_source_missing_coverage")
        or not response
    ):
        return
    analysis = state.get("analysis")
    if not isinstance(analysis, QueryAnalysis) or analysis.needs_clarification:
        return
    cited_sources = [str(item) for item in state.get("cited_sources") or [] if item]
    if not cited_sources:
        return
    if not _has_only_yonote_cited_sources(state, cited_sources):
        return
    if _has_temporally_bounded_cited_source(state, cited_sources):
        return
    verification = state.get("verification")
    if verification is not None and getattr(verification, "has_hallucination", False):
        return
    cached_response = CachedResponse(
        response=response,
        forum_normalized=analysis.forum_normalized,
        analysis=analysis,
        cited_sources=cited_sources,
        factual_source_type=_RESPONSE_CONTRACT.fact_policy.source_type,
        generator_model=str(state.get("generator_model") or "") or None,
        verifier_triggered=bool(state.get("verifier_triggered")),
    )
    try:
        await fastapi_app.state.semantic_cache.save(query, cached_response)
    except Exception as exc:
        logger.warning("semantic_cache_save_failed", error=str(exc))


def _has_only_yonote_cited_sources(
    state: dict[str, Any],
    cited_sources: list[str],
) -> bool:
    cited_ids = set(cited_sources)
    source_types: dict[str, str] = {}
    for chunk in state.get("reranked_chunks") or []:
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        if chunk_id not in cited_ids:
            continue
        metadata = getattr(chunk, "metadata", {})
        if not isinstance(metadata, dict):
            return False
        source_types[chunk_id] = str(metadata.get("source_type") or "").strip().casefold()
    return set(source_types) == cited_ids and all(
        source_type == _RESPONSE_CONTRACT.fact_policy.source_type
        for source_type in source_types.values()
    )


def _has_temporally_bounded_cited_source(
    state: dict[str, Any],
    cited_sources: list[str],
) -> bool:
    cited_ids = set(cited_sources)
    for chunk in state.get("reranked_chunks") or []:
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        if chunk_id not in cited_ids:
            continue
        metadata = getattr(chunk, "metadata", {})
        if not isinstance(metadata, dict):
            continue
        if any(str(metadata.get(field) or "").strip() for field in ("valid_from", "valid_to")):
            return True
    return False


async def _update_memory(
    fastapi_app: FastAPI,
    user_id_hash: str,
    channel: str,
    state: dict[str, Any],
    *,
    session: Session,
) -> None:
    analysis = state.get("analysis")
    if not analysis:
        return
    try:
        await fastapi_app.state.memory.upsert(
            user_id_hash=user_id_hash,
            channel=channel,
            forum=session.forum_context,
            topics=_memory_topics(session, analysis),
            structured_context={
                "forum_context": session.forum_context,
                "entities": session.extracted_entities,
                "clarification_history": session.clarification_history[-50:],
                "pending_clarification": session.pending_clarification,
                "clarification_attempts": session.clarification_attempts,
            },
        )
    except Exception as exc:
        logger.warning("user_memory_update_failed", error=str(exc))


def _memory_topics(session: Session, analysis: QueryAnalysis) -> list[str]:
    topics = session.extracted_entities.get("last_topics")
    if isinstance(topics, list):
        return [str(topic) for topic in topics if topic]
    return [str(topic) for topic in analysis.topics if topic]


async def _safe_log(fastapi_app: FastAPI, state: dict[str, Any]) -> None:
    try:
        await log_request(fastapi_app.state.pg_pool, state)
    except Exception as exc:
        logger.warning("request_trace_log_failed", error=str(exc))
