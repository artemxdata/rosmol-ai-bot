from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from src.channels.base import ChannelAdapter
from src.config import get_settings
from src.models import Channel, IncomingMessage

HDE_FIELD_CATEGORY_BOT = 23
HDE_FIELD_ESCALATION_BOT = 25
HDE_FIELD_SUMMARY_BOT = 29


class HDEAdapter(ChannelAdapter):
    def __init__(self, trigger_prefix: str | None = None) -> None:
        self._trigger_prefix = trigger_prefix

    def parse(self, payload: dict[str, Any]) -> IncomingMessage:
        visitor = payload.get("visitor") if isinstance(payload.get("visitor"), dict) else {}
        message = payload.get("message")
        message_payload = message if isinstance(message, dict) else {}

        chat_id = _first_non_empty(payload.get("chat_id"), payload.get("ticket_id"))
        visitor_id = _first_non_empty(
            visitor.get("id"),
            payload.get("user_id"),
            payload.get("client_id"),
        )
        user_id = _first_non_empty(chat_id, visitor_id, "unknown")
        text = _first_non_empty(
            message_payload.get("text"),
            payload.get("text"),
            message if isinstance(message, str) else None,
            "",
        )

        return IncomingMessage(
            user_id=str(user_id),
            channel=Channel.HDE,
            text=self._strip_trigger_prefix(str(text)),
            attachments=_normalize_attachments(
                payload.get("attachments") or message_payload.get("attachments") or []
            ),
        )

    async def send(self, user_id: str, text: str) -> None:
        settings = get_settings()
        base_url = str(getattr(settings, "hde_base_url", "") or "").strip()
        api_email = str(getattr(settings, "hde_api_email", "") or "").strip()
        api_key = str(getattr(settings, "hde_api_key", "") or "").strip()
        bot_user_id = str(getattr(settings, "hde_bot_user_id", "") or "").strip()
        timeout = float(getattr(settings, "hde_request_timeout_seconds", 20.0) or 20.0)

        if not base_url or not api_email or not api_key:
            logger.warning(
                "hde_send_skipped_not_configured",
                ticket_id=user_id,
                has_base_url=bool(base_url),
                has_api_email=bool(api_email),
                has_api_key=bool(api_key),
            )
            return

        payload = {"text": text}
        if bot_user_id:
            payload["user_id"] = bot_user_id

        url = _build_hde_posts_url(base_url, user_id)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                data=payload,
                auth=httpx.BasicAuth(api_email, api_key),
            )
            response.raise_for_status()

        logger.info(
            "hde_send_ok",
            ticket_id=user_id,
            status_code=response.status_code,
            fields=[HDE_FIELD_CATEGORY_BOT, HDE_FIELD_ESCALATION_BOT, HDE_FIELD_SUMMARY_BOT],
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
