from __future__ import annotations

import json
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
        "HDE_API_KEY": "h" * 48,
        "CLOUD_RU_API_KEY": "c" * 48,
        "POSTGRES_DSN": "postgresql://internal-value",
        "REDIS_URL": "redis://internal-value",
    }


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
        if parsed.hostname == "127.0.0.1" and path == "/ready":
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
        requester=FakeRequester(),
        log_reader=lambda _container, _since: "status=401 path=/ask",
    )

    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])
    assert any("No correctly authenticated HDE event" in item for item in report["limitations"])
    serialized = json.dumps(report)
    assert values["ADMIN_AUTH_TOKEN"] not in serialized
    assert values["HDE_API_KEY"] not in serialized


def test_live_gate_fails_closed_on_queue_backlog_and_log_secret() -> None:
    values = _values()
    report = gate.run_runtime_security_acceptance(
        values=values,
        expected_git_sha="a" * 40,
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


def test_private_report_is_no_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "runtime" / "gate.json"
    gate._write_private_report(output, {"passed": True})

    assert json.loads(output.read_text(encoding="utf-8")) == {"passed": True}
    with pytest.raises(FileExistsError):
        gate._write_private_report(output, {"passed": False})


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("http://127.0.0.1:8001", True),
        ("http://localhost:18001/", True),
        ("https://127.0.0.1:8001", False),
        ("http://public.example.org:8001", False),
        ("http://user:secret@127.0.0.1:8001", False),
        ("http://127.0.0.1", False),
    ),
)
def test_runtime_target_is_explicit_loopback_only(url: str, expected: bool) -> None:
    assert gate._valid_loopback_base_url(url) is expected


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
