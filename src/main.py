from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from time import perf_counter
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from src.admin import kb_store
from src.channels.hde import HDEAdapter
from src.channels.max import MaxAdapter
from src.channels.vk import VKAdapter
from src.config import get_settings
from src.graph.graph import build_graph
from src.kb.forum_registry import detect_forum_from_text
from src.llm.client import CloudRuLLMClient
from src.llm.routing import estimate_routing_hint
from src.llm.usage import (
    reset_llm_usage_collection,
    start_llm_usage_collection,
    summarize_llm_usage,
)
from src.logging.db_logger import log_request
from src.logging.tracer import Tracer
from src.models import Channel, IncomingMessage
from src.rag.cache import SemanticCache
from src.rag.embedder import Embedder
from src.rag.reranker import Reranker
from src.rag.retriever import Retriever
from src.security import profanity, safety
from src.security.pii_masker import PIIMasker
from src.security.rate_limiter import RateLimiter
from src.session.manager import SessionManager
from src.session.memory import UserMemory, hash_user_id


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
    yield
    await app.state.redis.aclose()
    await app.state.pg_pool.close()
    await app.state.qdrant.close()


app = FastAPI(title="Rosmol AI Bot", version="0.1.0", lifespan=lifespan)

vk_adapter = VKAdapter()
max_adapter = MaxAdapter()
hde_adapter = HDEAdapter()


class AskPayload(BaseModel):
    user_id: str = Field(default="local", min_length=1, max_length=200)
    channel: Channel = Channel.API
    text: str = Field(min_length=1, max_length=4000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)

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
    )
    if _should_bypass_cache(request):
        response = await process_message(message, request.app, bypass_cache=True)
    else:
        response = await process_message(message, request.app)
    return {"request_id": str(message.request_id), "response": response}


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
        return await asyncio.to_thread(
            kb_store.update_chunk,
            _kb_seed_path(),
            chunk_id,
            status=payload.status,
            text_clean=payload.text_clean,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Chunk not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
async def hde_webhook(request: Request) -> dict[str, bool]:
    _require_optional_secret(
        request,
        getattr(get_settings(), "webhook_auth_token", ""),
        "x-webhook-secret",
    )
    message = hde_adapter.parse(await request.json())
    response = await process_message(message, request.app)
    await hde_adapter.send(message.user_id, response)
    return {"ok": True}


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
    expected = (getattr(get_settings(), "admin_auth_token", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API is disabled")
    _require_optional_secret(request, expected, "x-admin-token")


def _kb_seed_path() -> Path:
    return Path(getattr(get_settings(), "kb_seed_path", "data/knowledge_base_seed.json"))


def _should_bypass_cache(request: Request) -> bool:
    requested = (request.headers.get("x-bypass-cache") or "").strip().casefold()
    if requested not in {"1", "true", "yes"}:
        return False
    return get_settings().app_env == "local"


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

    if profanity.check(message.text):
        response = (
            "Пожалуйста, воздержись от нецензурных выражений. "
            "Я помогу, если сформулировать вопрос спокойно."
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
                "escalation_reason": "profanity",
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
    routing_hint = estimate_routing_hint(masked_text)
    detected_forum = detect_forum_from_text(message.text)

    tracer = Tracer()
    state = {
        "request_id": message.request_id,
        "channel": message.channel.value,
        "user_id": message.user_id,
        "user_id_hash": user_id_hash,
        "message": message.text,
        "message_masked": masked_text,
        "routing_hint": routing_hint.model_dump(),
        "session": session,
        "trace": tracer,
        "llm_client": fastapi_app.state.llm_client,
        "embedder": getattr(fastapi_app.state, "embedder", None),
        "retriever": fastapi_app.state.retriever,
        "reranker": fastapi_app.state.reranker,
        "cache_hit": False,
    }

    cached_response = None
    if not bypass_cache:
        cached_response = await _check_cache(
            fastapi_app,
            masked_text,
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
    await fastapi_app.state.sessions.append_turn(session, masked_text, response)
    result["total_latency_ms"] = int((perf_counter() - started_at) * 1000)
    await _update_memory(fastapi_app, user_id_hash, message.channel.value, result)
    if not bypass_cache:
        await _save_cache(fastapi_app, masked_text, response, result)
    await _safe_log(fastapi_app, result)
    return response


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
