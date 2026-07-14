from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from src.channels.base import ChannelAdapter
from src.config import get_settings
from src.models import Channel, IncomingMessage
from src.session.memory import hash_user_id

HDE_FIELD_CATEGORY_BOT = 23
HDE_FIELD_ESCALATION_BOT = 25
HDE_FIELD_SUMMARY_BOT = 29
DEFAULT_HDE_LIMIT_RPM = 250
DEFAULT_HDE_REMAINING_RESERVE = 30
DEFAULT_HDE_BAN_SECONDS = 1200
HDE_TICKET_ID_MAX_LENGTH = 255
HDE_MESSAGE_TEXT_MAX_LENGTH = 4000


class HDEPayloadError(ValueError):
    """Safe validation error for an incoming HDE webhook payload."""


class HDEDeliveryStatus(StrEnum):
    DELIVERED = "delivered"
    NOT_CONFIGURED = "not_configured"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    ORDERING_FAILED = "ordering_failed"


@dataclass(frozen=True, slots=True)
class HDEDeliveryResult:
    status: HDEDeliveryStatus
    attempted: bool
    status_code: int | None = None
    retry_after_seconds: float | None = None
    error_code: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status == HDEDeliveryStatus.DELIVERED


class HDEOutgoingRateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._timestamps: deque[float] = deque()
        self._blocked_until = 0.0

    async def try_acquire(self, *, rpm: int) -> tuple[bool, str | None, float | None]:
        rpm = max(1, int(rpm or DEFAULT_HDE_LIMIT_RPM))
        now = monotonic()
        async with self._lock:
            if self._blocked_until > now:
                return False, "hde_rate_limit_block_active", self._blocked_until - now

            window_start = now - 60.0
            while self._timestamps and self._timestamps[0] <= window_start:
                self._timestamps.popleft()

            if len(self._timestamps) >= rpm:
                retry_after = max(0.0, 60.0 - (now - self._timestamps[0]))
                return False, "hde_local_rpm_limit_reached", retry_after

            self._timestamps.append(now)
            return True, None, None

    async def block_for(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds or 0.0))
        if seconds <= 0:
            return
        now = monotonic()
        async with self._lock:
            self._blocked_until = max(self._blocked_until, now + seconds)


class HDEAdapter(ChannelAdapter):
    def __init__(
        self,
        trigger_prefix: str | None = None,
        *,
        rate_limiter: HDEOutgoingRateLimiter | None = None,
    ) -> None:
        self._trigger_prefix = trigger_prefix
        self._rate_limiter = rate_limiter or HDEOutgoingRateLimiter()

    def parse(self, payload: dict[str, Any]) -> IncomingMessage:
        visitor: dict[str, Any] = (
            payload.get("visitor") if isinstance(payload.get("visitor"), dict) else {}
        )
        visitor_fields: dict[str, Any] = (
            visitor.get("fields") if isinstance(visitor.get("fields"), dict) else {}
        )
        message = payload.get("message")
        message_payload: dict[str, Any] = message if isinstance(message, dict) else {}

        ticket_id = _required_bounded_string(
            _first_non_empty(payload.get("chat_id"), payload.get("ticket_id")),
            field_name="ticket_id",
            limit=HDE_TICKET_ID_MAX_LENGTH,
        )
        text = _first_non_empty(
            message_payload.get("text"),
            payload.get("text"),
            message if isinstance(message, str) else None,
            "",
        )
        normalized_text = self._strip_trigger_prefix(str(text))
        if len(normalized_text) > HDE_MESSAGE_TEXT_MAX_LENGTH:
            raise HDEPayloadError("message_text_too_long")

        upstream_event_id, upstream_event_id_source = _extract_upstream_event_id(payload)
        incoming = IncomingMessage(
            user_id=ticket_id,
            channel=Channel.HDE,
            text=normalized_text,
            attachments=_normalize_attachments(
                payload.get("attachments") or message_payload.get("attachments") or []
            ),
            forum_context=_first_non_empty(
                payload.get("forum_context"),
                payload.get("source_forum"),
                message_payload.get("forum_context"),
                visitor_fields.get("forum_context"),
            )
            or None,
            upstream_event_id=upstream_event_id,
            upstream_event_id_source=upstream_event_id_source,
            eval_run_id=_optional_string(payload.get("eval_run_id"), limit=200),
            eval_case_id=_optional_string(payload.get("eval_case_id"), limit=200),
        )
        if incoming.upstream_event_id is None:
            incoming.upstream_event_id = str(incoming.request_id)
            incoming.upstream_event_id_source = "request_id_fallback"
        return incoming

    async def send(self, user_id: str, text: str) -> HDEDeliveryResult:
        settings = get_settings()
        ticket_id_hash = hash_user_id(Channel.HDE.value, user_id)
        base_url = str(getattr(settings, "hde_base_url", "") or "").strip()
        api_email = str(getattr(settings, "hde_api_email", "") or "").strip()
        api_key = str(getattr(settings, "hde_api_key", "") or "").strip()
        bot_user_id = str(getattr(settings, "hde_bot_user_id", "") or "").strip()
        timeout = float(getattr(settings, "hde_request_timeout_seconds", 20.0) or 20.0)
        rpm = int(getattr(settings, "hde_rate_limit_rpm", DEFAULT_HDE_LIMIT_RPM) or 0)
        remaining_reserve = int(
            getattr(
                settings,
                "hde_rate_limit_remaining_reserve",
                DEFAULT_HDE_REMAINING_RESERVE,
            )
            or 0
        )
        ban_seconds = int(
            getattr(settings, "hde_rate_limit_ban_seconds", DEFAULT_HDE_BAN_SECONDS)
            or DEFAULT_HDE_BAN_SECONDS
        )

        if not base_url or not api_email or not api_key:
            logger.warning(
                "hde_send_skipped_not_configured",
                ticket_id_hash=ticket_id_hash,
                has_base_url=bool(base_url),
                has_api_email=bool(api_email),
                has_api_key=bool(api_key),
            )
            return HDEDeliveryResult(
                HDEDeliveryStatus.NOT_CONFIGURED,
                attempted=False,
                error_code="hde_not_configured",
            )

        allowed, reason, retry_after = await self._rate_limiter.try_acquire(rpm=rpm)
        if not allowed:
            logger.warning(
                "hde_send_skipped_rate_limited",
                ticket_id_hash=ticket_id_hash,
                reason=reason,
                retry_after_seconds=round(retry_after or 0.0, 2),
                configured_rpm=rpm,
            )
            return HDEDeliveryResult(
                HDEDeliveryStatus.RATE_LIMITED,
                attempted=False,
                retry_after_seconds=retry_after,
                error_code=reason,
            )

        payload = {"text": text}
        if bot_user_id:
            payload["user_id"] = bot_user_id

        url = _build_hde_posts_url(base_url, user_id)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    data=payload,
                    auth=httpx.BasicAuth(api_email, api_key),
                )
        except httpx.TimeoutException:
            logger.warning(
                "hde_send_timeout",
                ticket_id_hash=ticket_id_hash,
                timeout_seconds=timeout,
            )
            return HDEDeliveryResult(
                HDEDeliveryStatus.TIMEOUT,
                attempted=True,
                error_code="hde_timeout",
            )
        except httpx.RequestError as exc:
            logger.warning(
                "hde_send_network_error",
                ticket_id_hash=ticket_id_hash,
                error_type=type(exc).__name__,
            )
            return HDEDeliveryResult(
                HDEDeliveryStatus.NETWORK_ERROR,
                attempted=True,
                error_code=type(exc).__name__,
            )

        await _apply_hde_response_rate_limits(
            response,
            self._rate_limiter,
            remaining_reserve=remaining_reserve,
            ban_seconds=ban_seconds,
            ticket_id_hash=ticket_id_hash,
        )
        if _is_hde_rate_limit_response(response):
            logger.warning(
                "hde_send_skipped_hde_rate_limit_response",
                ticket_id_hash=ticket_id_hash,
                status_code=response.status_code,
                hde_rate_limit=_int_header(response, "x-rate-limit"),
                hde_rate_limit_remaining=_int_header(response, "x-rate-limit-remaining"),
            )
            return HDEDeliveryResult(
                HDEDeliveryStatus.RATE_LIMITED,
                attempted=True,
                status_code=response.status_code,
                retry_after_seconds=_retry_after_seconds(response, default=ban_seconds),
                error_code="hde_remote_rate_limit",
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            logger.warning(
                "hde_send_http_error",
                ticket_id_hash=ticket_id_hash,
                status_code=response.status_code,
            )
            return HDEDeliveryResult(
                HDEDeliveryStatus.HTTP_ERROR,
                attempted=True,
                status_code=response.status_code,
                error_code=f"http_{response.status_code}",
            )

        logger.info(
            "hde_send_ok",
            ticket_id_hash=ticket_id_hash,
            status_code=response.status_code,
            fields=[HDE_FIELD_CATEGORY_BOT, HDE_FIELD_ESCALATION_BOT, HDE_FIELD_SUMMARY_BOT],
        )
        return HDEDeliveryResult(
            HDEDeliveryStatus.DELIVERED,
            attempted=True,
            status_code=response.status_code,
        )

    def _strip_trigger_prefix(self, text: str) -> str:
        stripped = text.strip()
        prefix = (
            self._trigger_prefix
            if self._trigger_prefix is not None
            else getattr(get_settings(), "hde_trigger_prefix", "")
        )
        prefix = str(prefix or "").strip()
        if prefix and stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
        return stripped


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def _optional_string(value: Any, *, limit: int | None = None) -> str | None:
    normalized = "" if value is None else str(value).strip()
    if limit is not None:
        normalized = normalized[:limit]
    return normalized or None


def _required_bounded_string(value: Any, *, field_name: str, limit: int) -> str:
    normalized = "" if value is None else str(value).strip()
    if not normalized:
        raise HDEPayloadError(f"{field_name}_required")
    if len(normalized) > limit:
        raise HDEPayloadError(f"{field_name}_too_long")
    return normalized


def _extract_upstream_event_id(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    message: dict[str, Any] = (
        payload.get("message") if isinstance(payload.get("message"), dict) else {}
    )
    event: dict[str, Any] = (
        payload.get("event") if isinstance(payload.get("event"), dict) else {}
    )
    data: dict[str, Any] = (
        payload.get("data") if isinstance(payload.get("data"), dict) else {}
    )
    data_message: dict[str, Any] = (
        data.get("message") if isinstance(data.get("message"), dict) else {}
    )
    candidates = (
        (message.get("id"), "message.id"),
        (message.get("message_id"), "message.message_id"),
        (message.get("post_id"), "message.post_id"),
        (data_message.get("id"), "data.message.id"),
        (data_message.get("message_id"), "data.message.message_id"),
        (event.get("id"), "event.id"),
        (event.get("event_id"), "event.event_id"),
        (payload.get("event_id"), "event_id"),
        (payload.get("message_id"), "message_id"),
        (payload.get("post_id"), "post_id"),
    )
    for value, source in candidates:
        normalized = _optional_string(value)
        if normalized:
            if len(normalized) > HDE_TICKET_ID_MAX_LENGTH:
                raise HDEPayloadError("upstream_event_id_too_long")
            return normalized, source
    return None, None


def _normalize_attachments(raw_attachments: Any) -> list[dict[str, Any]]:
    if not raw_attachments:
        return []
    if isinstance(raw_attachments, list):
        return [item for item in raw_attachments if isinstance(item, dict)]
    if isinstance(raw_attachments, dict):
        return [raw_attachments]
    return []


def _build_hde_posts_url(base_url: str, ticket_id: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("HDE base URL is not configured")
    if normalized.endswith("/api/v2"):
        api_base = normalized
    else:
        api_base = f"{normalized}/api/v2"
    return f"{api_base}/tickets/{quote(str(ticket_id).strip(), safe='')}/posts/"


async def _apply_hde_response_rate_limits(
    response: httpx.Response,
    limiter: HDEOutgoingRateLimiter,
    *,
    remaining_reserve: int,
    ban_seconds: int,
    ticket_id_hash: str,
) -> None:
    if _is_hde_rate_limit_response(response):
        await limiter.block_for(_retry_after_seconds(response, default=ban_seconds))
        return

    remaining = _int_header(response, "x-rate-limit-remaining")
    if remaining is None:
        return
    if remaining <= max(0, remaining_reserve):
        await limiter.block_for(60.0)
        logger.warning(
            "hde_send_rate_limit_remaining_low",
            ticket_id_hash=ticket_id_hash,
            hde_rate_limit=_int_header(response, "x-rate-limit"),
            hde_rate_limit_remaining=remaining,
            reserve=remaining_reserve,
        )


def _is_hde_rate_limit_response(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    try:
        payload = response.json()
    except (AttributeError, ValueError):
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict):
            continue
        code = str(error.get("code") or "").casefold()
        title = str(error.get("title") or "").casefold()
        details = str(error.get("details") or "").casefold()
        if code == "e-401" and ("ban" in title or "api limit" in details):
            return True
    return False


def _retry_after_seconds(response: httpx.Response, *, default: int) -> float:
    retry_after = _int_header(response, "retry-after")
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    return float(default)


def _int_header(response: httpx.Response, name: str) -> int | None:
    headers = getattr(response, "headers", {})
    value = headers.get(name) if hasattr(headers, "get") else None
    if value is None and hasattr(headers, "items"):
        expected = name.casefold()
        for key, header_value in headers.items():
            if str(key).casefold() == expected:
                value = header_value
                break
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None
