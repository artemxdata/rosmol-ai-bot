"""Minimal method-and-path allowlist proxy for Qdrant diagnostics."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM_URL = "http://qdrant:6333"
COLLECTION = "knowledge_base"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 6333
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ALLOWED_POST_PATHS = frozenset(
    {
        f"/collections/{COLLECTION}/points/query",
        f"/collections/{COLLECTION}/points/scroll",
    }
)


def _upstream_api_key() -> str:
    upstream = os.environ.get("QDRANT_UPSTREAM_URL", "").strip()
    api_key = os.environ.get("QDRANT_UPSTREAM_API_KEY", "")
    if upstream != UPSTREAM_URL:
        raise RuntimeError("read-only proxy upstream is invalid")
    if not api_key or len(api_key) > 4096 or any(
        character in api_key for character in "\r\n\0"
    ):
        raise RuntimeError("read-only proxy credential is invalid")
    return api_key


class ReadOnlyQdrantHandler(BaseHTTPRequestHandler):
    server_version = "rosmol-qdrant-readonly/1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz" and not parsed.query and not parsed.fragment:
            self._reply(200, b'{"status":"ok"}')
            return
        self._reply(405, b'{"status":"method_not_allowed"}')

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if (
            parsed.path not in ALLOWED_POST_PATHS
            or parsed.query
            or parsed.fragment
        ):
            self._reply(405, b'{"status":"method_not_allowed"}')
            return
        raw_length = self.headers.get("Content-Length", "")
        try:
            content_length = int(raw_length)
        except ValueError:
            self._reply(400, b'{"status":"invalid_content_length"}')
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._reply(413, b'{"status":"request_too_large"}')
            return
        body = self.rfile.read(content_length)
        try:
            parsed_body = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            self._reply(400, b'{"status":"invalid_json"}')
            return
        if not isinstance(parsed_body, dict):
            self._reply(400, b'{"status":"invalid_json"}')
            return
        request = urllib.request.Request(
            UPSTREAM_URL + parsed.path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "api-key": self.server.upstream_api_key,  # type: ignore[attr-defined]
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
        except (OSError, urllib.error.URLError):
            self._reply(502, b'{"status":"upstream_unavailable"}')
            return
        if len(payload) > MAX_RESPONSE_BYTES:
            self._reply(502, b'{"status":"upstream_response_too_large"}')
            return
        self._reply(status, payload)

    def _reply(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    api_key = _upstream_api_key()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ReadOnlyQdrantHandler)
    server.daemon_threads = True
    server.upstream_api_key = api_key  # type: ignore[attr-defined]
    server.serve_forever(poll_interval=0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
