from __future__ import annotations

import asyncio
from time import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from src.main import _should_bypass_cache
from src.main import app as fastapi_app
from src.security import eval_cache_bypass


class OneTimeNonceRedis:
    def __init__(self, *, fail_set: bool = False) -> None:
        self.fail_set = fail_set
        self.set_calls: list[tuple[str, str, bool, int]] = []
        self._reserved: set[str] = set()
        self._lock = asyncio.Lock()

    async def ping(self) -> bool:
        return True

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool,
        ex: int,
    ) -> bool | None:
        self.set_calls.append((key, value, nx, ex))
        if self.fail_set:
            raise RuntimeError("redis unavailable")
        async with self._lock:
            if nx and key in self._reserved:
                return None
            self._reserved.add(key)
            return True


@pytest.mark.asyncio
async def test_ask_rejects_blank_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(api_auth_token="", webhook_auth_token=""),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"user_id": "u1", "text": "   "})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_ask_requires_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(api_auth_token="secret", webhook_auth_token=""),
    )

    async def fake_process_message(message, fastapi_app) -> str:
        return "ok"

    monkeypatch.setattr("src.main.process_message", fake_process_message)
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/ask", json={"user_id": "u1", "text": "Привет"})
        provided = await client.post(
            "/ask",
            json={"user_id": "u1", "text": "Привет"},
            headers={"X-API-Key": "secret"},
        )

    assert missing.status_code == 401
    assert provided.status_code == 200
    assert provided.json()["response"] == "ok"


@pytest.mark.asyncio
async def test_webhook_requires_token_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(api_auth_token="", webhook_auth_token="secret"),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook/vk", json={"object": {"message": {"text": "x"}}})

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/webhook/vk", "/webhook/max"])
async def test_direct_stub_webhooks_are_disabled_outside_local_runtime(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(app_env="production", webhook_auth_token="secret"),
    )
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            path,
            json={"object": {"message": {"text": "x"}}},
            headers={"X-Webhook-Secret": "secret"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bypass_cache_header_allowed_for_loopback_server_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ask",
            "headers": [
                (b"host", b"bot.example.test"),
                (b"x-bypass-cache", b"true"),
            ],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8001),
        }
    )

    assert await _should_bypass_cache(request) is True


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", b"127.0.0.1:8001"), (b"x-bypass-cache", b"true")],
        [
            (b"host", b"bot.example.test"),
            (b"x-bypass-cache", b"true"),
            (b"x-forwarded-for", b"127.0.0.1"),
        ],
    ],
)
@pytest.mark.asyncio
async def test_bypass_cache_header_ignores_external_host_and_forwarded_spoof(
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[bytes, bytes]],
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ask",
            "headers": headers,
            "client": ("203.0.113.10", 12345),
            "scheme": "https",
            "server": ("bot.example.test", 443),
        }
    )

    assert await _should_bypass_cache(request) is False


@pytest.mark.asyncio
async def test_bypass_cache_header_rejects_forwarded_loopback_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ask",
            "headers": [
                (b"host", b"bot.example.test"),
                (b"x-bypass-cache", b"true"),
                (b"forwarded", b"for=203.0.113.10"),
            ],
            "client": ("127.0.0.1", 12345),
            "scheme": "https",
            "server": ("bot.example.test", 443),
        }
    )

    assert await _should_bypass_cache(request) is False


def _signed_bypass_request(
    *,
    secret: str,
    provided_signature: str | None = None,
    client_host: str = "172.20.0.4",
    forwarded_for: str | None = None,
    timestamp: str | None = None,
    nonce: str = "a" * 32,
    redis: OneTimeNonceRedis | None = None,
) -> Request:
    proof_timestamp = timestamp or str(int(time()))
    signature = provided_signature or eval_cache_bypass.signature(
        secret,
        method="POST",
        path="/ask",
        eval_run_id="ask-eval-signed",
        eval_case_id="case-001",
        timestamp=proof_timestamp,
        nonce=nonce,
        payload_sha256=eval_cache_bypass.EMPTY_PAYLOAD_SHA256,
    )
    headers = [
        (b"host", b"app-ml:8000"),
        (b"x-bypass-cache", b"1"),
        (b"x-eval-run-id", b"ask-eval-signed"),
        (b"x-eval-case-id", b"case-001"),
        (
            b"x-eval-cache-bypass-version",
            eval_cache_bypass.SCHEME.encode("ascii"),
        ),
        (b"x-eval-cache-bypass-timestamp", proof_timestamp.encode("ascii")),
        (b"x-eval-cache-bypass-nonce", nonce.encode("ascii")),
        (b"x-eval-cache-bypass-signature", signature.encode("ascii")),
    ]
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ask",
            "headers": headers,
            "client": (client_host, 12345),
            "scheme": "http",
            "server": ("app-ml", 8000),
            "app": SimpleNamespace(
                state=SimpleNamespace(redis=redis or OneTimeNonceRedis())
            ),
        }
    )


@pytest.mark.asyncio
async def test_bypass_cache_allows_valid_signed_internal_eval_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )

    assert await _should_bypass_cache(
        _signed_bypass_request(secret="eval-secret")
    ) is True


@pytest.mark.asyncio
async def test_signed_cache_bypass_nonce_is_single_use_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )
    redis = OneTimeNonceRedis()
    request = _signed_bypass_request(secret="eval-secret", redis=redis)

    assert await _should_bypass_cache(request) is True
    assert await _should_bypass_cache(request) is False
    assert redis.set_calls == [
        (
            f"{eval_cache_bypass.NONCE_CACHE_KEY_PREFIX}{'a' * 32}",
            "1",
            True,
            eval_cache_bypass.NONCE_TTL_SECONDS,
        ),
        (
            f"{eval_cache_bypass.NONCE_CACHE_KEY_PREFIX}{'a' * 32}",
            "1",
            True,
            eval_cache_bypass.NONCE_TTL_SECONDS,
        ),
    ]


@pytest.mark.asyncio
async def test_signed_cache_bypass_nonce_is_single_use_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )
    redis = OneTimeNonceRedis()
    request = _signed_bypass_request(secret="eval-secret", redis=redis)

    results = await asyncio.gather(
        _should_bypass_cache(request),
        _should_bypass_cache(request),
    )

    assert sorted(results) == [False, True]
    assert len(redis.set_calls) == 2


@pytest.mark.asyncio
async def test_signed_cache_bypass_authorizes_valid_loopback_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )
    redis = OneTimeNonceRedis()

    assert await _should_bypass_cache(
        _signed_bypass_request(
            secret="eval-secret",
            client_host="127.0.0.1",
            redis=redis,
        )
    ) is True
    assert len(redis.set_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["local", "production"])
async def test_bad_signed_loopback_proof_never_falls_back_to_unsigned_bypass(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env=app_env,
            api_auth_token="eval-secret",
        ),
    )
    redis = OneTimeNonceRedis()

    assert await _should_bypass_cache(
        _signed_bypass_request(
            secret="eval-secret",
            provided_signature="0" * 64,
            client_host="127.0.0.1",
            redis=redis,
        )
    ) is False
    assert redis.set_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_name", "header_value"),
    [
        ("x-eval-cache-bypass-signature", ""),
        ("x-eval-run-id", "partial-run"),
        ("x-eval-case-id", "partial-case"),
    ],
)
async def test_partial_signed_headers_never_fall_back_to_local_bypass(
    monkeypatch: pytest.MonkeyPatch,
    header_name: str,
    header_value: str,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="local",
            api_auth_token="eval-secret",
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/ask",
            "headers": [
                (b"host", b"127.0.0.1:8000"),
                (b"x-bypass-cache", b"1"),
                (header_name.encode("ascii"), header_value.encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
        }
    )

    assert await _should_bypass_cache(request) is False


@pytest.mark.asyncio
async def test_stale_signed_proof_does_not_reserve_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )
    redis = OneTimeNonceRedis()

    assert await _should_bypass_cache(
        _signed_bypass_request(
            secret="eval-secret",
            timestamp=str(int(time()) - eval_cache_bypass.MAX_CLOCK_SKEW_SECONDS - 1),
            redis=redis,
        )
    ) is False
    assert redis.set_calls == []


@pytest.mark.asyncio
async def test_signed_cache_bypass_fails_closed_when_redis_set_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )
    redis = OneTimeNonceRedis(fail_set=True)

    assert await _should_bypass_cache(
        _signed_bypass_request(secret="eval-secret", redis=redis)
    ) is False
    assert len(redis.set_calls) == 1


@pytest.mark.asyncio
async def test_bypass_cache_rejects_bad_eval_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )

    redis = OneTimeNonceRedis()

    assert await _should_bypass_cache(
        _signed_bypass_request(
            secret="eval-secret",
            provided_signature="0" * 64,
            redis=redis,
        )
    ) is False
    assert redis.set_calls == []


@pytest.mark.asyncio
async def test_bypass_cache_rejects_valid_signature_from_public_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )

    assert await _should_bypass_cache(
        _signed_bypass_request(
            secret="eval-secret",
            client_host="8.8.8.8",
        )
    ) is False


@pytest.mark.asyncio
async def test_bypass_cache_rejects_valid_signature_through_forwarded_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token="eval-secret",
        ),
    )

    assert await _should_bypass_cache(
        _signed_bypass_request(
            secret="eval-secret",
            forwarded_for="203.0.113.10",
        )
    ) is False


@pytest.mark.asyncio
async def test_cache_bypass_capability_probe_rejects_forwarded_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "eval-secret"
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token=secret,
        ),
    )
    timestamp = str(int(time()))
    nonce = "c" * 32
    signature = eval_cache_bypass.signature(
        secret,
        method="GET",
        path="/ready",
        eval_run_id="ask-eval-signed",
        eval_case_id=eval_cache_bypass.CAPABILITY_PROBE_CASE_ID,
        timestamp=timestamp,
        nonce=nonce,
        payload_sha256=eval_cache_bypass.EMPTY_PAYLOAD_SHA256,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ready",
            "headers": [
                (b"host", b"app-ml:8000"),
                (b"x-bypass-cache", b"1"),
                (b"x-eval-cache-bypass-probe", b"1"),
                (b"x-eval-run-id", b"ask-eval-signed"),
                (
                    b"x-eval-case-id",
                    eval_cache_bypass.CAPABILITY_PROBE_CASE_ID.encode("ascii"),
                ),
                (
                    b"x-eval-cache-bypass-version",
                    eval_cache_bypass.SCHEME.encode("ascii"),
                ),
                (b"x-eval-cache-bypass-timestamp", timestamp.encode("ascii")),
                (b"x-eval-cache-bypass-nonce", nonce.encode("ascii")),
                (b"x-eval-cache-bypass-signature", signature.encode("ascii")),
                (b"x-forwarded-for", b"203.0.113.10"),
            ],
            "client": ("172.20.0.4", 12345),
            "scheme": "http",
            "server": ("app-ml", 8000),
        }
    )

    assert await _should_bypass_cache(request) is False


@pytest.mark.asyncio
async def test_ready_capability_probe_reports_replayed_proof_as_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "eval-secret"
    ready_redis = OneTimeNonceRedis()

    class ReadyPostgres:
        async def fetchval(self, query: str) -> int:
            assert query == "select 1"
            return 1

    class ReadyQdrant:
        async def count(self, *, collection_name: str, exact: bool) -> SimpleNamespace:
            assert collection_name == "knowledge_base"
            assert exact is True
            return SimpleNamespace(count=1)

    settings = SimpleNamespace(
        app_env="production",
        api_auth_token=secret,
    )
    runtime_settings = SimpleNamespace(
        app_env="test",
        runtime_role="api",
        release_git_sha="test-release",
        qdrant_knowledge_collection="knowledge_base",
        hde_transport_enabled=False,
    )
    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr(
        fastapi_app.state,
        "runtime_settings",
        runtime_settings,
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "runtime_config",
        {"status": "ok"},
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "kb_manifest",
        {"published_records": 1},
        raising=False,
    )
    monkeypatch.setattr(fastapi_app.state, "redis", ready_redis, raising=False)
    monkeypatch.setattr(fastapi_app.state, "pg_pool", ReadyPostgres(), raising=False)
    monkeypatch.setattr(fastapi_app.state, "qdrant", ReadyQdrant(), raising=False)

    headers = {
        "X-API-Key": secret,
        eval_cache_bypass.HEADER_BYPASS: "1",
        eval_cache_bypass.HEADER_CAPABILITY_PROBE: "1",
        **eval_cache_bypass.build_signed_headers(
            secret,
            method="GET",
            path="/ready",
            eval_run_id="ask-eval-capability",
            eval_case_id=eval_cache_bypass.CAPABILITY_PROBE_CASE_ID,
            payload_sha256=eval_cache_bypass.EMPTY_PAYLOAD_SHA256,
        ),
    }
    transport = httpx.ASGITransport(
        app=fastapi_app,
        client=("172.20.0.4", 12345),
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://app-ml:8000",
    ) as client:
        first = await client.get("/ready", headers=headers)
        replay = await client.get("/ready", headers=headers)

    assert first.status_code == 200
    assert first.json()["eval_cache_bypass"] == {
        "scheme": eval_cache_bypass.SCHEME,
        "authorized": True,
    }
    assert replay.status_code == 200
    assert replay.json()["eval_cache_bypass"] == {
        "scheme": eval_cache_bypass.SCHEME,
        "authorized": False,
    }


@pytest.mark.asyncio
async def test_ask_applies_signed_bypass_once_and_rejects_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "eval-secret"
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token=secret,
        ),
    )
    bypass_values: list[bool] = []
    nonce_redis = OneTimeNonceRedis()
    monkeypatch.setattr(
        fastapi_app.state,
        "redis",
        nonce_redis,
        raising=False,
    )

    async def fake_process_message(
        message,
        app,
        *,
        bypass_cache: bool = False,
    ) -> str:
        bypass_values.append(bypass_cache)
        return "ok"

    monkeypatch.setattr("src.main.process_message", fake_process_message)
    timestamp = str(int(time()))
    nonce = "b" * 32
    payload = {
        "user_id": "signed-eval-user",
        "channel": "api",
        "text": "Привет",
    }
    canonical_payload = {
        **payload,
        "attachments": [],
        "forum_context": None,
    }
    provided_signature = eval_cache_bypass.signature(
        secret,
        method="POST",
        path="/ask",
        eval_run_id="ask-eval-signed",
        eval_case_id="case-001",
        timestamp=timestamp,
        nonce=nonce,
        payload_sha256=eval_cache_bypass.canonical_payload_sha256(
            canonical_payload
        ),
    )
    headers = {
        "X-API-Key": secret,
        "X-Bypass-Cache": "1",
        "X-Eval-Run-Id": "ask-eval-signed",
        "X-Eval-Case-Id": "case-001",
        "X-Eval-Cache-Bypass-Version": eval_cache_bypass.SCHEME,
        "X-Eval-Cache-Bypass-Timestamp": timestamp,
        "X-Eval-Cache-Bypass-Nonce": nonce,
        "X-Eval-Cache-Bypass-Signature": provided_signature,
    }
    transport = httpx.ASGITransport(
        app=fastapi_app,
        client=("172.20.0.4", 12345),
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://app-ml:8000",
    ) as client:
        first = await client.post("/ask", json=payload, headers=headers)
        replay = await client.post("/ask", json=payload, headers=headers)

    assert first.status_code == 200
    assert first.json()["response"] == "ok"
    assert replay.status_code == 403
    assert bypass_values == [True]


@pytest.mark.asyncio
async def test_ask_rejects_unauthorized_cache_bypass_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "eval-secret"
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token=secret,
        ),
    )
    processed = False

    async def fake_process_message(message, app, **kwargs) -> str:
        nonlocal processed
        processed = True
        return "must not run"

    monkeypatch.setattr("src.main.process_message", fake_process_message)
    transport = httpx.ASGITransport(
        app=fastapi_app,
        client=("172.20.0.4", 12345),
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://app-ml:8000",
    ) as client:
        response = await client.post(
            "/ask",
            json={"user_id": "eval-user", "text": "Привет"},
            headers={
                "X-API-Key": secret,
                "X-Bypass-Cache": "1",
                "X-Eval-Run-Id": "ask-eval-invalid",
                "X-Eval-Case-Id": "case-001",
                "X-Eval-Cache-Bypass-Version": eval_cache_bypass.SCHEME,
                "X-Eval-Cache-Bypass-Timestamp": str(int(time())),
                "X-Eval-Cache-Bypass-Nonce": "a" * 32,
                "X-Eval-Cache-Bypass-Signature": "0" * 64,
            },
        )

    assert response.status_code == 403
    assert processed is False


@pytest.mark.asyncio
async def test_ask_rejects_signed_bypass_when_nonce_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "eval-secret"
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            api_auth_token=secret,
        ),
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "redis",
        OneTimeNonceRedis(fail_set=True),
        raising=False,
    )
    processed = False

    async def fake_process_message(message, app, **kwargs) -> str:
        nonlocal processed
        processed = True
        return "must not run"

    monkeypatch.setattr("src.main.process_message", fake_process_message)
    payload = {"user_id": "eval-user", "text": "Привет"}
    canonical_payload = {
        **payload,
        "channel": "api",
        "attachments": [],
        "forum_context": None,
    }
    headers = {
        "X-API-Key": secret,
        eval_cache_bypass.HEADER_BYPASS: "1",
        **eval_cache_bypass.build_signed_headers(
            secret,
            method="POST",
            path="/ask",
            eval_run_id="ask-eval-redis-failure",
            eval_case_id="case-001",
            payload_sha256=eval_cache_bypass.canonical_payload_sha256(
                canonical_payload
            ),
        ),
    }
    transport = httpx.ASGITransport(
        app=fastapi_app,
        client=("172.20.0.4", 12345),
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://app-ml:8000",
    ) as client:
        response = await client.post("/ask", json=payload, headers=headers)

    assert response.status_code == 403
    assert processed is False
