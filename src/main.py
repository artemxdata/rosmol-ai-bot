from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from hmac import compare_digest
from ipaddress import ip_address
from pathlib import Path
from time import perf_counter, time
from typing import Any

import asyncpg
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from scripts.sync_yonote_kb import YonoteApiError
from src.admin import kb_index, kb_store, ui
from src.admin.yonote_sync import (
    YonoteSyncConfigError,
)
from src.admin.yonote_sync import (
    apply_sync as apply_yonote_sync,
)
from src.admin.yonote_sync import (
    preview_sync as preview_yonote_sync,
)
from src.channels.hde import HDEAdapter
from src.channels.max import MaxAdapter
from src.channels.vk import VKAdapter
from src.config import get_settings
from src.graph.context import NON_FORUM_CONTEXT_CATEGORIES, is_context_dependent_followup
from src.graph.graph import build_graph
from src.graph.nodes.analyze import is_safe_offtopic_message
from src.kb.forum_registry import detect_forum_from_text
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
from src.rag.cache import SemanticCache
from src.rag.embedder import Embedder
from src.rag.errors import MLDependencyError
from src.rag.reranker import Reranker
from src.rag.retriever import Retriever
from src.security import profanity, safety
from src.security.operator_request import is_operator_request
from src.security.pii_masker import PIIMasker
from src.security.rate_limiter import RateLimiter
from src.session.manager import SessionManager
from src.session.memory import UserMemory, hash_user_id

ATTACHMENT_ONLY_RESPONSE = (
    "Передаю обращение специалисту: сейчас я не могу надёжно разобрать скриншот "
    "или вложение без текстового описания."
)
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
MAX_CLARIFICATION_ATTEMPTS = 5
CLARIFICATION_EXHAUSTED_RESPONSE = (
    "Мне не удалось уточнить достаточно данных за несколько шагов. "
    "Передаю обращение специалисту, чтобы не задерживать решение."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.pg_pool = await asyncpg.create_pool(settings.postgres_dsn, min_size=1, max_size=5)
    app.state.qdrant = AsyncQdrantClient(url=settings.qdrant_url)
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
    yield
    await app.state.llm_client.aclose()
    await app.state.redis.aclose()
    await app.state.pg_pool.close()
    await app.state.qdrant.close()


app = FastAPI(title="Rosmol AI Bot", version="0.1.0", lifespan=lifespan)

vk_adapter = VKAdapter()
max_adapter = MaxAdapter()
hde_adapter = HDEAdapter()

ADMIN_SESSION_COOKIE = "rosmol_admin_session"
ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


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
        await request.app.state.qdrant.get_collections()
        checks["qdrant"] = "ok"
    except Exception as exc:
        checks["qdrant"] = f"error: {type(exc).__name__}"

    ml_prewarm = getattr(request.app.state, "ml_prewarm", None)
    if ml_prewarm and ml_prewarm.get("enabled"):
        if ml_prewarm.get("status") == "ok":
            checks["ml_prewarm"] = "ok"
        else:
            error = ml_prewarm.get("error") or ml_prewarm.get("status") or "unknown"
            checks["ml_prewarm"] = f"error: {error}"

    if any(status != "ok" for status in checks.values()):
        raise HTTPException(status_code=503, detail={"status": "degraded", "checks": checks})
    return {"status": "ready", "checks": checks}


@app.post("/ask")
async def ask(payload: AskPayload, request: Request) -> dict[str, Any]:
    _require_optional_secret(request, getattr(get_settings(), "api_auth_token", ""), "x-api-key")
    message = IncomingMessage(
        user_id=payload.user_id,
        channel=payload.channel,
        text=payload.text,
        attachments=payload.attachments,
        forum_context=payload.forum_context,
    )
    if _should_bypass_cache(request):
        response = await process_message(message, request.app, bypass_cache=True)
    else:
        response = await process_message(message, request.app)
    return {"request_id": str(message.request_id), "response": response}


@app.get("/admin/kb", response_class=HTMLResponse)
async def admin_kb_page() -> HTMLResponse:
    _require_admin_enabled()
    return HTMLResponse(content=ui.ADMIN_KB_HTML)


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
    try:
        return await asyncio.to_thread(
            preview_yonote_sync,
            _kb_seed_path(),
            get_settings(),
            limit_documents=payload.limit_documents,
        )
    except YonoteSyncConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YonoteApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/admin/kb/yonote/apply")
async def admin_apply_yonote_sync(
    payload: AdminYonoteSyncPayload,
    request: Request,
) -> dict[str, Any]:
    _require_admin_secret(request)
    try:
        return await asyncio.to_thread(
            apply_yonote_sync,
            _kb_seed_path(),
            get_settings(),
            limit_documents=payload.limit_documents,
        )
    except YonoteSyncConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YonoteApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@app.post("/admin/kb/chunks/{chunk_id}/reindex")
async def admin_reindex_kb_chunk(chunk_id: str, request: Request) -> dict[str, Any]:
    _require_admin_secret(request)
    record = await asyncio.to_thread(kb_store.get_chunk, _kb_seed_path(), chunk_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return await _admin_reindex_record(request, record)


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
async def hde_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    _require_optional_secret(
        request,
        getattr(get_settings(), "webhook_auth_token", ""),
        "x-webhook-secret",
    )
    message = hde_adapter.parse(await request.json())
    background_tasks.add_task(_process_hde_message, message, request.app)
    return {"ok": True}


async def _process_hde_message(message: IncomingMessage, fastapi_app: FastAPI) -> None:
    try:
        response = await process_message(message, fastapi_app)
    except Exception as exc:
        logger.exception(
            "hde_background_processing_failed",
            request_id=str(message.request_id),
            ticket_id=message.user_id,
            error=str(exc),
        )
        response = "Передаю обращение специалисту."

    try:
        await hde_adapter.send(message.user_id, response)
    except Exception as exc:
        logger.exception(
            "hde_background_send_failed",
            request_id=str(message.request_id),
            ticket_id=message.user_id,
            error=str(exc),
        )


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
    await asyncio.to_thread(
        fastapi_app.state.pii_masker.mask,
        "Иван Иванов спрашивает о регистрации на форум.",
    )
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

    forum = str(record.get("forum_normalized") or "").strip()
    if forum:
        try:
            await request.app.state.semantic_cache.invalidate_forum(forum)
            result["cache_invalidated_forum"] = forum
        except Exception as exc:
            logger.warning(
                "admin_cache_invalidation_failed",
                chunk_id=record.get("chunk_id"),
                forum=forum,
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
    started_at = perf_counter()
    settings = get_settings()
    user_id_hash = hash_user_id(message.channel.value, message.user_id)

    if not await fastapi_app.state.rate_limiter.check(message.user_id, message.channel.value):
        response = (
            "Слишком много сообщений за короткое время. "
            "Попробуй ещё раз через несколько минут."
        )
        await _safe_log(
            fastapi_app,
            {
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
    masked_text, pii_mapping = fastapi_app.state.pii_masker.mask(message.text)

    if _is_attachment_only_message(message):
        response = ATTACHMENT_ONLY_RESPONSE
        await _safe_log(
            fastapi_app,
            {
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
        response = "Передаю обращение специалисту."
        await _safe_log(
            fastapi_app,
            {
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

    if profanity.check(message.text) and not _has_actionable_support_context(message.text):
        response = (
            "Я не поддерживаю оскорбления и не вступаю в споры. "
            "Я отвечаю на вопросы по мероприятиям, форумам, ФГАИС «Молодёжь России» "
            "и грантам Росмолодёжи. Задай, пожалуйста, вопрос по этим темам."
        )
        await _safe_log(
            fastapi_app,
            {
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

    if pii_mapping:
        await fastapi_app.state.redis.set(
            f"pii:{message.request_id}",
            str(pii_mapping),
            ex=settings.session_ttl_seconds,
        )

    session = await fastapi_app.state.sessions.get_or_create(message.channel.value, message.user_id)

    if is_operator_request(masked_text):
        response = "Передаю обращение специалисту."
        session = await _clear_pending_clarification(fastapi_app, session)
        await fastapi_app.state.sessions.append_turn(session, masked_text, response)
        await _safe_log(
            fastapi_app,
            {
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
    detected_forum = detect_forum_from_text(graph_message)
    cache_allowed = not is_context_dependent_followup(
        graph_masked_text,
        session,
    ) and not is_safe_offtopic_message(graph_message)
    cache_allowed = cache_allowed and not is_registration_query(graph_masked_text)
    cache_allowed = cache_allowed and not pending_context_applied

    tracer = Tracer()
    state = {
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
        state.update(
            {
                "cache_hit": True,
                "final_response": cached_response,
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            }
        )
        session = await _clear_pending_clarification(
            fastapi_app,
            session,
            detected_forum=detected_forum,
        )
        await fastapi_app.state.sessions.append_turn(session, masked_text, cached_response)
        await _safe_log(fastapi_app, state)
        return cached_response

    if not settings.cloud_ru_api_key:
        response = "Передаю обращение специалисту, потому что LLM-доступ ещё не настроен."
        state.update(
            {
                "final_response": response,
                "should_escalate": True,
                "escalation_reason": "llm_not_configured",
                "total_latency_ms": int((perf_counter() - started_at) * 1000),
            }
        )
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
            "final_response": "Передаю обращение специалисту, чтобы не задерживать ответ.",
            "should_escalate": True,
            "escalation_reason": "request_timeout",
            "error": "request_timeout",
        }
    finally:
        reset_llm_usage_collection(llm_usage_token)
    result.update(summarize_llm_usage(llm_usage_events))
    response = result.get("final_response") or "Передаю обращение специалисту."
    session, response = await _update_dialog_session(
        fastapi_app,
        session,
        result,
        pending_text=graph_masked_text,
        response=response,
    )
    result["final_response"] = response
    await fastapi_app.state.sessions.append_turn(session, masked_text, response)
    result["total_latency_ms"] = int((perf_counter() - started_at) * 1000)
    await _update_memory(fastapi_app, user_id_hash, message.channel.value, result)
    if not bypass_cache and cache_allowed:
        await _save_cache(fastapi_app, masked_text, response, result)
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
    topics = [str(topic) for topic in analysis.topics if topic]
    entities = dict(session.extracted_entities)
    if analysis.category:
        entities["last_category"] = analysis.category
    if topics:
        entities["last_topics"] = topics

    if _is_actionable_clarification(analysis) and not result.get("should_escalate"):
        attempts = session.clarification_attempts + 1
        if attempts > MAX_CLARIFICATION_ATTEMPTS:
            result["should_escalate"] = True
            result["escalation_reason"] = "clarification_exhausted"
            return (
                await fastapi_app.state.sessions.update(
                    session,
                    forum_context=forum,
                    extracted_entities=entities,
                    pending_clarification=None,
                    clarification_attempts=0,
                ),
                CLARIFICATION_EXHAUSTED_RESPONSE,
            )
        updated = await fastapi_app.state.sessions.update(
            session,
            forum_context=forum,
            extracted_entities=entities,
            pending_clarification=_limit_pending_context(pending_text),
            clarification_attempts=attempts,
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
            "ошиб",
            "задолб",
            "тупит",
            "фигн",
        )
    )


async def _check_cache(fastapi_app: FastAPI, query: str, forum: str | None) -> str | None:
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
    if state.get("should_escalate") or not response:
        return
    analysis = state.get("analysis")
    forum = getattr(analysis, "forum_normalized", None) if analysis else None
    try:
        await fastapi_app.state.semantic_cache.save(query, forum, response)
    except Exception as exc:
        logger.warning("semantic_cache_save_failed", error=str(exc))


async def _update_memory(
    fastapi_app: FastAPI,
    user_id_hash: str,
    channel: str,
    state: dict[str, Any],
) -> None:
    analysis = state.get("analysis")
    if not analysis:
        return
    try:
        await fastapi_app.state.memory.upsert(
            user_id_hash=user_id_hash,
            channel=channel,
            forum=analysis.forum_normalized,
            topics=analysis.topics,
            summary=state.get("final_response"),
        )
    except Exception as exc:
        logger.warning("user_memory_update_failed", error=str(exc))


async def _safe_log(fastapi_app: FastAPI, state: dict[str, Any]) -> None:
    try:
        await log_request(fastapi_app.state.pg_pool, state)
    except Exception as exc:
        logger.warning("request_trace_log_failed", error=str(exc))
