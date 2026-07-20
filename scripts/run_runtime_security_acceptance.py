from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from secrets import token_hex, token_urlsafe
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.generate_production_env import (  # noqa: E402
    GENERATED_SECRET_KEYS,
    _parse_env,
    validate_env,
)

DEFAULT_RUNTIME_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_OUTPUT = Path("data/private/runtime/runtime-security-acceptance.json")
DEFAULT_LOG_CONTAINERS = ("rosmol-app-ml", "rosmol-nginx", "rosmol-edge-relay")
QUEUE_ZERO_FIELDS = (
    "inbox_backlog",
    "inbox_processing",
    "inbox_dead_letter",
    "outbox_backlog",
    "outbox_sending",
    "outbox_dead_letter",
)
REQUIRED_READY_CHECKS = (
    "config",
    "redis",
    "postgres",
    "knowledge_base",
    "ml_prewarm",
    "hde_transport",
)
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
LOG_HEADER_MARKERS = (
    "x-api-key",
    "x-webhook-secret",
    "x-admin-token",
    "authorization:",
    "cookie:",
    "set-cookie:",
    "rosmol_admin_session=",
)


@dataclass(frozen=True)
class HttpSnapshot:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class Probe:
    name: str
    passed: bool
    evidence: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


Requester = Callable[..., HttpSnapshot]
LogReader = Callable[[str, str], str]


def _build_opener() -> Any:
    return build_opener(
        ProxyHandler({}),
        HTTPCookieProcessor(CookieJar()),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )


def _request(
    opener: Any,
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> HttpSnapshot:
    body = None
    request_headers = {"User-Agent": "rosmol-runtime-security-acceptance/1"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            response_body = response.read(65_537)
            return HttpSnapshot(
                status=int(response.status),
                headers={key.casefold(): value for key, value in response.headers.items()},
                body=response_body,
            )
    except HTTPError as exc:
        try:
            response_body = exc.read(65_537)
        finally:
            exc.close()
        return HttpSnapshot(
            status=int(exc.code),
            headers={key.casefold(): value for key, value in exc.headers.items()},
            body=response_body,
        )


def _safe_request(
    probes: list[Probe],
    requester: Requester,
    opener: Any,
    name: str,
    url: str,
    expected_status: int,
    **kwargs: Any,
) -> HttpSnapshot | None:
    try:
        response = requester(opener, url, **kwargs)
    except (OSError, URLError, ValueError) as exc:
        probes.append(Probe(name, False, f"request_error={type(exc).__name__}"))
        return None
    probes.append(
        Probe(
            name,
            response.status == expected_status,
            f"status={response.status},expected={expected_status}",
        )
    )
    return response


def _ready_probe(
    probes: list[Probe],
    requester: Requester,
    opener: Any,
    *,
    name: str,
    url: str,
    expected_git_sha: str,
) -> dict[str, Any] | None:
    response = _safe_request(probes, requester, opener, f"{name}_http", url, 200)
    if response is None or response.status != 200:
        probes.append(Probe(name, False, "ready_http_failed"))
        return None
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        probes.append(Probe(name, False, "ready_json_invalid"))
        return None
    checks = payload.get("checks") if isinstance(payload, dict) else None
    queue = payload.get("hde_transport_counts") if isinstance(payload, dict) else None
    ready_ok = (
        isinstance(checks, dict)
        and isinstance(queue, dict)
        and payload.get("status") == "ready"
        and payload.get("release_git_sha") == expected_git_sha
        and all(checks.get(field) == "ok" for field in REQUIRED_READY_CHECKS)
        and all(queue.get(field) == 0 for field in QUEUE_ZERO_FIELDS)
    )
    probes.append(
        Probe(
            name,
            ready_ok,
            "release/config/ML-PII-prewarm/transport/empty-queue accepted"
            if ready_ok
            else "release/config/ML-PII-prewarm/transport/empty-queue rejected",
        )
    )
    return queue if isinstance(queue, dict) else None


def _sensitive_values(values: Mapping[str, str]) -> tuple[str, ...]:
    keys = set(GENERATED_SECRET_KEYS) | {
        "HDE_API_KEY",
        "CLOUD_RU_API_KEY",
        "POSTGRES_DSN",
        "REDIS_URL",
    }
    return tuple(
        sorted(
            {values.get(key, "") for key in keys if len(values.get(key, "")) >= 8},
            key=len,
            reverse=True,
        )
    )


def _docker_log_reader(*, use_sudo: bool) -> LogReader:
    prefix = ["sudo", "-n", "docker"] if use_sudo else ["docker"]

    def read(container: str, since: str) -> str:
        if not CONTAINER_NAME.fullmatch(container):
            raise ValueError("invalid_container_name")
        completed = subprocess.run(
            [*prefix, "logs", "--since", since, container],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env={"PATH": os.environ.get("PATH", "")},
        )
        if completed.returncode != 0:
            raise RuntimeError("container_logs_unavailable")
        return f"{completed.stdout}\n{completed.stderr}"

    return read


def run_runtime_security_acceptance(
    *,
    values: Mapping[str, str],
    expected_git_sha: str,
    runtime_base_url: str = DEFAULT_RUNTIME_BASE_URL,
    requester: Requester = _request,
    log_reader: LogReader,
    log_containers: tuple[str, ...] = DEFAULT_LOG_CONTAINERS,
) -> dict[str, Any]:
    probes: list[Probe] = []
    started_at = datetime.now(UTC)
    since = started_at.isoformat().replace("+00:00", "Z")
    opener = _build_opener()
    host = values.get("ADMIN_PUBLIC_HOST", "").strip()
    https_base = f"https://{host}"
    http_base = f"http://{host}"
    runtime_base = runtime_base_url.rstrip("/") + "/"
    public_https = https_base.rstrip("/") + "/"
    public_http = http_base.rstrip("/") + "/"
    sentinel = f"security-probe-{token_hex(16)}"
    wrong_secret = token_urlsafe(48)
    while wrong_secret in set(values.values()):
        wrong_secret = token_urlsafe(48)

    initial_queue = _ready_probe(
        probes,
        requester,
        opener,
        name="runtime_ready_before",
        url=urljoin(runtime_base, "ready"),
        expected_git_sha=expected_git_sha,
    )
    _safe_request(
        probes,
        requester,
        opener,
        "runtime_health",
        urljoin(runtime_base, "health"),
        200,
    )
    _safe_request(
        probes,
        requester,
        opener,
        "https_health",
        urljoin(public_https, "health"),
        200,
    )

    ask_payload = {"user_id": sentinel, "text": f"unauthorized {sentinel}"}
    hde_payload = {
        "chat_id": sentinel,
        "message": {"id": sentinel, "text": f"unauthorized {sentinel}"},
    }
    for path, method, payload in (
        ("ask", "POST", ask_payload),
        ("webhook/hde", "POST", hde_payload),
        ("admin/kb/login", "POST", {"token": wrong_secret}),
        ("ready", "GET", None),
    ):
        _safe_request(
            probes,
            requester,
            opener,
            f"plaintext_{path.replace('/', '_')}_blocked",
            urljoin(public_http, path),
            426,
            method=method,
            payload=payload,
        )

    for suffix in ("docs", "redoc", "openapi.json"):
        _safe_request(
            probes,
            requester,
            opener,
            f"https_{suffix.replace('.', '_')}_disabled",
            urljoin(public_https, suffix),
            404,
        )

    for auth_name, headers in (
        ("missing", {}),
        ("wrong", {"X-API-Key": wrong_secret}),
    ):
        _safe_request(
            probes,
            requester,
            opener,
            f"ask_{auth_name}_auth_rejected",
            urljoin(public_https, "ask"),
            401,
            method="POST",
            payload=ask_payload,
            headers=headers,
        )
    for auth_name, headers in (
        ("missing", {}),
        ("wrong", {"X-Webhook-Secret": wrong_secret}),
    ):
        _safe_request(
            probes,
            requester,
            opener,
            f"hde_{auth_name}_auth_rejected",
            urljoin(public_https, "webhook/hde"),
            401,
            method="POST",
            payload=hde_payload,
            headers=headers,
        )

    for channel in ("vk", "max"):
        _safe_request(
            probes,
            requester,
            opener,
            f"direct_{channel}_disabled",
            urljoin(public_https, f"webhook/{channel}"),
            404,
            method="POST",
            payload={"probe": sentinel},
            headers={"X-Webhook-Secret": wrong_secret},
        )

    for auth_name, headers in (
        ("missing", {}),
        ("wrong", {"X-Admin-Token": wrong_secret}),
    ):
        _safe_request(
            probes,
            requester,
            opener,
            f"admin_{auth_name}_auth_rejected",
            urljoin(public_https, "admin/kb/chunks"),
            401,
            headers=headers,
        )

    login = _safe_request(
        probes,
        requester,
        opener,
        "admin_https_login",
        urljoin(public_https, "admin/kb/login"),
        200,
        method="POST",
        payload={"token": values["ADMIN_AUTH_TOKEN"]},
    )
    cookie = (login.headers.get("set-cookie", "") if login else "").casefold()
    cookie_ok = all(
        marker in cookie
        for marker in (
            "rosmol_admin_session=",
            "secure",
            "httponly",
            "samesite=lax",
            "path=/admin/kb",
        )
    )
    probes.append(Probe("admin_cookie_security_flags", cookie_ok, "required flags present"))
    _safe_request(
        probes,
        requester,
        opener,
        "admin_cookie_session_accepted",
        urljoin(public_https, "admin/kb/chunks?limit=1"),
        200,
    )
    _safe_request(
        probes,
        requester,
        opener,
        "admin_logout",
        urljoin(public_https, "admin/kb/logout"),
        200,
        method="POST",
    )
    _safe_request(
        probes,
        requester,
        opener,
        "admin_session_invalidated",
        urljoin(public_https, "admin/kb/chunks?limit=1"),
        401,
    )

    final_queue = _ready_probe(
        probes,
        requester,
        opener,
        name="runtime_ready_after",
        url=urljoin(runtime_base, "ready"),
        expected_git_sha=expected_git_sha,
    )
    queue_unchanged = initial_queue is not None and initial_queue == final_queue
    probes.append(
        Probe(
            "unauthorized_probes_did_not_mutate_hde_queue",
            queue_unchanged,
            "queue unchanged" if queue_unchanged else "queue changed or unavailable",
        )
    )

    forbidden_values = (*_sensitive_values(values), sentinel)
    for container in log_containers:
        try:
            logs = log_reader(container, since)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            probes.append(
                Probe(
                    f"logs_{container}",
                    False,
                    f"log_scan_error={type(exc).__name__}",
                )
            )
            continue
        lowered = logs.casefold()
        has_sensitive_value = any(value and value in logs for value in forbidden_values)
        has_sensitive_header = any(marker in lowered for marker in LOG_HEADER_MARKERS)
        probes.append(
            Probe(
                f"logs_{container}",
                not has_sensitive_value and not has_sensitive_header,
                "no probe PII, credential value or secret header"
                if not has_sensitive_value and not has_sensitive_header
                else "sensitive runtime log material detected",
            )
        )

    return {
        "schema_version": 1,
        "kind": "safe_live_runtime_security_acceptance",
        "generated_at": datetime.now(UTC).isoformat(),
        "expected_git_sha": expected_git_sha,
        "runtime_target": runtime_base_url,
        "public_host": host,
        "passed": bool(probes) and all(probe.passed for probe in probes),
        "checks": [asdict(probe) for probe in probes],
        "limitations": [
            "No correctly authenticated /ask request is generated by this gate.",
            "No correctly authenticated HDE event or provider delivery is generated by this gate.",
            "Dedupe/order/retry/dead-letter semantics require the named offline regression gate.",
            "End-to-end delivery requires the later limited HDE smoke "
            "with dispatcher stop criteria.",
        ],
    }


def _write_private_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run non-delivering live security probes against the clean recovery runtime. "
            "Secrets are read from the private env file and are never printed."
        )
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env.production"))
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--runtime-base-url", default=DEFAULT_RUNTIME_BASE_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log-container", action="append", dest="log_containers")
    parser.add_argument("--use-sudo-docker", action="store_true")
    return parser


def _valid_loopback_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.username is None
        and parsed.password is None
        and port is not None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    expected_git_sha = args.expected_git_sha.strip()
    if not FULL_GIT_SHA.fullmatch(expected_git_sha) or expected_git_sha == "0" * 40:
        print("ERROR: expected Git SHA must be a non-zero full lowercase SHA.", file=sys.stderr)
        return 2
    if not _valid_loopback_base_url(args.runtime_base_url):
        print("ERROR: runtime base URL must be an explicit loopback HTTP URL.", file=sys.stderr)
        return 2
    if args.env_file.is_symlink():
        print("ERROR: production env must not be a symlink.", file=sys.stderr)
        return 2
    env_errors = validate_env(args.env_file)
    if env_errors:
        print("ERROR: production env validation failed.", file=sys.stderr)
        for error in env_errors:
            print(f"- {error}", file=sys.stderr)
        return 2
    values, parse_errors = _parse_env(args.env_file.read_text(encoding="utf-8"))
    if parse_errors:
        print("ERROR: production env parsing failed.", file=sys.stderr)
        return 2
    if values.get("RELEASE_GIT_SHA") != expected_git_sha:
        print("ERROR: production env release SHA mismatch.", file=sys.stderr)
        return 2
    required_auth = ("API_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN", "ADMIN_AUTH_TOKEN")
    if any(len(values.get(key, "")) < 32 for key in required_auth):
        print("ERROR: required runtime authentication material is unavailable.", file=sys.stderr)
        return 2
    if len({values[key] for key in required_auth}) != len(required_auth):
        print("ERROR: runtime authentication values must be distinct.", file=sys.stderr)
        return 2

    log_containers = tuple(args.log_containers or DEFAULT_LOG_CONTAINERS)
    report = run_runtime_security_acceptance(
        values=values,
        expected_git_sha=expected_git_sha,
        runtime_base_url=args.runtime_base_url,
        log_reader=_docker_log_reader(use_sudo=args.use_sudo_docker),
        log_containers=log_containers,
    )
    try:
        _write_private_report(args.output, report)
    except FileExistsError:
        print(f"ERROR: refusing to overwrite existing report: {args.output}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: runtime security report write failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    status = "passed" if report["passed"] else "failed"
    print(f"runtime_security_acceptance={status} report={args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
