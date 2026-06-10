from __future__ import annotations

from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from loguru import logger
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from src.channels.hde import HDEAdapter
from src.channels.max import MaxAdapter
from src.channels.vk import VKAdapter
from src.config import get_settings
from src.graph.graph import build_graph
from src.llm.client import GigaChatClient
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
    app.state.llm_client = GigaChatClient()
    app.state.embedder = Embedder()
    app.state.retriever = Retriever(app.state.qdrant, app.state.embedder)
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask")
async def ask(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    message = IncomingMessage(
        user_id=str(payload.get("user_id", "local")),
        channel=Channel(payload.get("channel", Channel.API)),
        text=str(payload.get("text", "")),
    )
    response = await process_message(message, request.app)
    return {"request_id": str(message.request_id), "response": response}


@app.post("/webhook/vk")
async def vk_webhook(request: Request) -> dict[str, bool]:
    message = vk_adapter.parse(await request.json())
    response = await process_message(message, request.app)
    await vk_adapter.send(message.user_id, response)
    return {"ok": True}


@app.post("/webhook/max")
async def max_webhook(request: Request) -> dict[str, bool]:
    message = max_adapter.parse(await request.json())
    response = await process_message(message, request.app)
    await max_adapter.send(message.user_id, response)
    return {"ok": True}


@app.post("/webhook/hde")
async def hde_webhook(request: Request) -> dict[str, bool]:
    message = hde_adapter.parse(await request.json())
    response = await process_message(message, request.app)
    await hde_adapter.send(message.user_id, response)
    return {"ok": True}


async def process_message(message: IncomingMessage, fastapi_app: FastAPI) -> str:
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

    tracer = Tracer()
    state = {
        "request_id": message.request_id,
        "channel": message.channel.value,
        "user_id": message.user_id,
        "user_id_hash": user_id_hash,
        "message": message.text,
        "message_masked": masked_text,
        "session": session,
        "trace": tracer,
        "llm_client": fastapi_app.state.llm_client,
        "retriever": fastapi_app.state.retriever,
        "reranker": fastapi_app.state.reranker,
        "cache_hit": False,
    }

    cached_response = await _check_cache(fastapi_app, masked_text, session.forum_context)
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

    if not settings.gigachat_api_key and not settings.gigachat_access_token:
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

    result = await fastapi_app.state.graph.ainvoke(state)
    response = result.get("final_response") or "Передаю обращение специалисту."
    await fastapi_app.state.sessions.append_turn(session, masked_text, response)
    result["total_latency_ms"] = int((perf_counter() - started_at) * 1000)
    await _update_memory(fastapi_app, user_id_hash, message.channel.value, result)
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
