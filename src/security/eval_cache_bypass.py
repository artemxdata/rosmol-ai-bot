from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import time
from typing import Any
from uuid import uuid4

SCHEME = "hmac-sha256-v1"
MAX_CLOCK_SKEW_SECONDS = 90
NONCE_TTL_SECONDS = 190
CAPABILITY_PROBE_CASE_ID = "__cache_bypass_capability__"
NONCE_CACHE_KEY_PREFIX = "security:eval-cache-bypass:nonce:"

HEADER_BYPASS = "X-Bypass-Cache"
HEADER_CAPABILITY_PROBE = "X-Eval-Cache-Bypass-Probe"
HEADER_VERSION = "X-Eval-Cache-Bypass-Version"
HEADER_TIMESTAMP = "X-Eval-Cache-Bypass-Timestamp"
HEADER_NONCE = "X-Eval-Cache-Bypass-Nonce"
HEADER_SIGNATURE = "X-Eval-Cache-Bypass-Signature"
HEADER_RUN_ID = "X-Eval-Run-Id"
HEADER_CASE_ID = "X-Eval-Case-Id"

EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class ValidatedProof:
    """A cryptographically valid proof that has not yet passed replay protection."""

    eval_run_id: str
    eval_case_id: str
    timestamp: str
    nonce: str

    @property
    def nonce_cache_key(self) -> str:
        return f"{NONCE_CACHE_KEY_PREFIX}{self.nonce}"


def canonical_payload_sha256(payload: Mapping[str, Any] | None) -> str:
    if payload is None:
        return EMPTY_PAYLOAD_SHA256
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_ask_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(payload["user_id"]).strip(),
        "channel": str(payload.get("channel") or "api"),
        "text": str(payload["text"]).strip(),
        "attachments": payload.get("attachments") or [],
        "forum_context": payload.get("forum_context") or None,
    }


def signature(
    secret: str,
    *,
    method: str,
    path: str,
    eval_run_id: str,
    eval_case_id: str,
    timestamp: str,
    nonce: str,
    payload_sha256: str,
) -> str:
    message = "\n".join(
        (
            SCHEME,
            method.upper(),
            path,
            eval_run_id,
            eval_case_id,
            timestamp,
            nonce,
            payload_sha256,
        )
    )
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_headers(
    secret: str,
    *,
    method: str,
    path: str,
    eval_run_id: str,
    eval_case_id: str,
    payload_sha256: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    normalized_secret = secret.strip()
    if not normalized_secret:
        raise ValueError("cache bypass signing secret is required")
    proof_timestamp = timestamp or str(int(time()))
    proof_nonce = nonce or uuid4().hex
    return {
        HEADER_RUN_ID: eval_run_id,
        HEADER_CASE_ID: eval_case_id,
        HEADER_VERSION: SCHEME,
        HEADER_TIMESTAMP: proof_timestamp,
        HEADER_NONCE: proof_nonce,
        HEADER_SIGNATURE: signature(
            normalized_secret,
            method=method,
            path=path,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
            timestamp=proof_timestamp,
            nonce=proof_nonce,
            payload_sha256=payload_sha256,
        ),
    }


def is_well_formed_proof(
    *,
    timestamp: str,
    nonce: str,
    provided_signature: str,
    payload_sha256: str,
) -> bool:
    return (
        timestamp.isascii()
        and timestamp.isdecimal()
        and 1 <= len(timestamp) <= 12
        and _NONCE_RE.fullmatch(nonce) is not None
        and _SHA256_RE.fullmatch(provided_signature) is not None
        and _SHA256_RE.fullmatch(payload_sha256) is not None
    )


def validate_signed_proof(
    secret: str,
    *,
    method: str,
    path: str,
    version: str,
    eval_run_id: str,
    eval_case_id: str,
    timestamp: str,
    nonce: str,
    provided_signature: str,
    payload_sha256: str,
    current_time: int | None = None,
) -> ValidatedProof | None:
    """Validate a signed bypass proof without changing external state."""

    normalized_secret = secret.strip()
    if (
        not normalized_secret
        or version != SCHEME
        or not eval_run_id
        or len(eval_run_id) > 200
        or not eval_case_id
        or len(eval_case_id) > 200
        or not is_well_formed_proof(
            timestamp=timestamp,
            nonce=nonce,
            provided_signature=provided_signature,
            payload_sha256=payload_sha256,
        )
    ):
        return None

    proof_time = int(timestamp)
    now = int(time()) if current_time is None else int(current_time)
    if abs(now - proof_time) > MAX_CLOCK_SKEW_SECONDS:
        return None

    expected_signature = signature(
        normalized_secret,
        method=method,
        path=path,
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        timestamp=timestamp,
        nonce=nonce,
        payload_sha256=payload_sha256,
    )
    if not hmac.compare_digest(provided_signature, expected_signature):
        return None

    return ValidatedProof(
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        timestamp=timestamp,
        nonce=nonce,
    )


async def authorize_once(redis: Any, proof: ValidatedProof) -> bool:
    """Atomically consume a valid proof nonce; replay/store failure denies access."""

    try:
        reserved = await redis.set(
            proof.nonce_cache_key,
            "1",
            nx=True,
            ex=NONCE_TTL_SECONDS,
        )
    except Exception:
        return False
    return bool(reserved)
