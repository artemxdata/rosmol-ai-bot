from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from scripts import run_runtime_security_acceptance as gate


def _values() -> dict[str, str]:
    return {
        "ADMIN_PUBLIC_HOST": "bot.example.test",
        "API_AUTH_TOKEN": "a" * 48,
        "WEBHOOK_AUTH_TOKEN": "w" * 48,
        "ADMIN_AUTH_TOKEN": "m" * 48,
        "HDE_API_EMAIL": "hde-api-user@example.test",
        "HDE_API_KEY": "h" * 48,
        "CLOUD_RU_API_KEY": "c" * 48,
        "YONOTE_API_TOKEN": "y" * 48,
        "POSTGRES_DSN": "postgresql://internal-value",
        "REDIS_URL": "redis://internal-value",
    }


def _recent_log_start() -> str:
    return (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ready_payload(*, queue_backlog: int = 0) -> bytes:
    checks = {name: "ok" for name in gate.REQUIRED_READY_CHECKS}
    queue = {name: 0 for name in gate.QUEUE_ZERO_FIELDS}
    queue["inbox_backlog"] = queue_backlog
    return json.dumps(
        {
            "status": "ready",
            "release_git_sha": "a" * 40,
            "checks": checks,
            "hde_transport_counts": queue,
        }
    ).encode()


class FakeRequester:
    def __init__(self, *, queue_backlog: int = 0) -> None:
        self.logged_in = False
        self.queue_backlog = queue_backlog

    def __call__(
        self,
        _opener: Any,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> gate.HttpSnapshot:
        parsed = urlsplit(url)
        path = parsed.path
        request_headers = headers or {}
        if parsed.scheme == "http" and parsed.hostname == "bot.example.test":
            return gate.HttpSnapshot(426, {}, b"")
        if parsed.hostname in {"127.0.0.1", "bot.example.test"} and path == "/ready":
            return gate.HttpSnapshot(
                200,
                {},
                _ready_payload(queue_backlog=self.queue_backlog),
            )
        if path == "/health":
            return gate.HttpSnapshot(200, {}, b'{"status":"ok"}')
        if path in {"/docs", "/redoc", "/openapi.json", "/webhook/vk", "/webhook/max"}:
            return gate.HttpSnapshot(404, {}, b"")
        if path == "/ask" or path == "/webhook/hde":
            return gate.HttpSnapshot(401, {}, b"")
        if path == "/admin/kb/login":
            assert method == "POST"
            assert payload is not None
            self.logged_in = True
            cookie = (
                "rosmol_admin_session=opaque; Path=/admin/kb; "
                "Secure; HttpOnly; SameSite=Lax"
            )
            return gate.HttpSnapshot(200, {"set-cookie": cookie}, b"")
        if path == "/admin/kb/logout":
            self.logged_in = False
            return gate.HttpSnapshot(200, {}, b"")
        if path == "/admin/kb/chunks":
            if request_headers or not self.logged_in:
                return gate.HttpSnapshot(401, {}, b"")
            return gate.HttpSnapshot(200, {}, b'{"items":[]}')
        raise AssertionError(f"unexpected request: {method} {url}")


def test_live_gate_passes_safe_probes_without_provider_delivery() -> None:
    values = _values()
    report = gate.run_runtime_security_acceptance(
        values=values,
        expected_git_sha="a" * 40,
        runtime_base_url="https://bot.example.test",
        log_since_utc=_recent_log_start(),
        requester=FakeRequester(),
        log_reader=lambda _container, _since: "status=401 path=/ask",
    )

    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])
    assert any("No correctly authenticated HDE event" in item for item in report["limitations"])
    serialized = json.dumps(report)
    assert values["ADMIN_AUTH_TOKEN"] not in serialized
    assert values["HDE_API_KEY"] not in serialized
    assert values["YONOTE_API_TOKEN"] not in serialized


def test_live_gate_fails_closed_on_queue_backlog_and_log_secret() -> None:
    values = _values()
    report = gate.run_runtime_security_acceptance(
        values=values,
        expected_git_sha="a" * 40,
        runtime_base_url="https://bot.example.test",
        log_since_utc=_recent_log_start(),
        requester=FakeRequester(queue_backlog=1),
        log_reader=lambda _container, _since: f"leaked={values['HDE_API_KEY']}",
    )

    assert report["passed"] is False
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert "runtime_ready_before" in failed
    assert "runtime_ready_after" in failed
    assert "logs_rosmol-app-ml" in failed
    assert "logs_rosmol-nginx" in failed
    assert "logs_rosmol-edge-relay" in failed
    assert values["HDE_API_KEY"] not in json.dumps(report)


def test_live_gate_fails_closed_on_yonote_token_in_logs() -> None:
    values = _values()
    report = gate.run_runtime_security_acceptance(
        values=values,
        expected_git_sha="a" * 40,
        runtime_base_url="https://bot.example.test",
        log_since_utc=_recent_log_start(),
        requester=FakeRequester(),
        log_reader=lambda _container, _since: (
            f"provider_token={values['YONOTE_API_TOKEN']}"
        ),
    )

    assert report["passed"] is False
    assert all(
        not check["passed"]
        for check in report["checks"]
        if check["name"].startswith("logs_")
    )
    assert values["YONOTE_API_TOKEN"] not in json.dumps(report)


def test_live_gate_cannot_omit_mandatory_log_containers() -> None:
    scanned: list[str] = []

    report = gate.run_runtime_security_acceptance(
        values=_values(),
        expected_git_sha="a" * 40,
        runtime_base_url="https://bot.example.test",
        log_since_utc=_recent_log_start(),
        requester=FakeRequester(),
        log_reader=lambda container, _since: scanned.append(container) or "safe",
        log_containers=(),
    )

    assert report["passed"] is True
    assert scanned == list(gate.DEFAULT_LOG_CONTAINERS)
    required = next(
        check for check in report["checks"] if check["name"] == "required_log_coverage"
    )
    assert required["passed"] is True


def test_live_gate_detects_identity_and_negative_probe_secret_in_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_secret = "negative-probe-secret-" * 4
    monkeypatch.setattr(gate, "token_urlsafe", lambda _size: wrong_secret)
    values = _values()

    report = gate.run_runtime_security_acceptance(
        values=values,
        expected_git_sha="a" * 40,
        runtime_base_url="https://bot.example.test",
        log_since_utc=_recent_log_start(),
        requester=FakeRequester(),
        log_reader=lambda _container, _since: (
            f"identity={values['HDE_API_EMAIL']} wrong={wrong_secret}"
        ),
    )

    assert report["passed"] is False
    assert all(
        not check["passed"]
        for check in report["checks"]
        if check["name"].startswith("logs_")
    )


def test_private_report_is_no_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "runtime" / "gate.json"
    gate._write_private_report(output, {"passed": True})

    assert json.loads(output.read_text(encoding="utf-8")) == {"passed": True}
    with pytest.raises(FileExistsError):
        gate._write_private_report(output, {"passed": False})


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("https://bot.example.test", True),
        ("https://bot.example.test:443/", True),
        ("HTTPS://BOT.EXAMPLE.TEST", True),
        ("http://bot.example.test", False),
        ("https://other.example.test", False),
        ("https://bot.example.test:8443", False),
        ("https://user:secret@bot.example.test", False),
        ("https://bot.example.test/ask", False),
        ("https://bot.example.test?query=1", False),
        ("http://127.0.0.1:8001", False),
        ("http://localhost:18001/", False),
    ),
)
def test_runtime_target_accepts_only_exact_production_https(
    url: str,
    expected: bool,
) -> None:
    assert gate._valid_runtime_base_url(
        url,
        public_host="bot.example.test",
    ) is expected


def test_log_scan_start_is_recent_strict_utc() -> None:
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (datetime.now(UTC) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert gate._valid_log_since_utc(recent)
    assert not gate._valid_log_since_utc(future)
    assert not gate._valid_log_since_utc("2000-01-01T00:00:00Z")
    assert not gate._valid_log_since_utc("2026-07-22 12:00:00")


def test_log_scan_start_is_mandatory_for_cli_and_direct_calls() -> None:
    parser_args = [
        "--expected-git-sha",
        "a" * 40,
        "--expected-public-ipv4",
        "203.0.113.10",
        "--runtime-base-url",
        "https://bot.example.test",
    ]
    with pytest.raises(SystemExit):
        gate._build_parser().parse_args(parser_args)

    with pytest.raises(ValueError, match="invalid_log_scan_start"):
        gate.run_runtime_security_acceptance(
            values=_values(),
            expected_git_sha="a" * 40,
            runtime_base_url="https://bot.example.test",
            log_since_utc="",
            requester=FakeRequester(),
            log_reader=lambda _container, _since: "safe",
        )


def test_dns_pin_rejects_any_additional_ipv4_or_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answers(*ips: str) -> list[tuple[Any, ...]]:
        return [
            (
                gate.socket.AF_INET6 if ":" in ip else gate.socket.AF_INET,
                gate.socket.SOCK_STREAM,
                6,
                "",
                (ip, 443, 0, 0) if ":" in ip else (ip, 443),
            )
            for ip in ips
        ]

    monkeypatch.setattr(
        gate.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: answers("203.0.113.10"),
    )
    assert gate._host_resolves_only_to_reviewed_ipv4(
        "bot.example.test",
        "203.0.113.10",
    )

    monkeypatch.setattr(
        gate.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: answers("203.0.113.10", "2001:db8::10"),
    )
    assert not gate._host_resolves_only_to_reviewed_ipv4(
        "bot.example.test",
        "203.0.113.10",
    )

    monkeypatch.setattr(
        gate.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: answers("203.0.113.10", "203.0.113.11"),
    )
    assert not gate._host_resolves_only_to_reviewed_ipv4(
        "bot.example.test",
        "203.0.113.10",
    )


def test_runbook_separates_offline_invariants_from_safe_live_probes() -> None:
    runbook = Path("docs/recovery_test_production_runbook_20260720.md").read_text(
        encoding="utf-8"
    )

    assert "scripts/run_runtime_security_acceptance.py" in runbook
    assert "Gate 4A" in runbook
    assert "Gate 4B" in runbook
    assert "не отправляет корректно авторизованный `/ask`" in runbook
    assert "CORRECTION_GIT_SHA" in runbook
    assert "без `--env-file`" in runbook
    assert runbook.count('--expected-public-ipv4 "$EXPECTED_PUBLIC_IPV4"') == 2
