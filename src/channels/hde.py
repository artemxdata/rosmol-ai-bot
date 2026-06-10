from __future__ import annotations

from typing import Any

from loguru import logger

from src.channels.base import ChannelAdapter
from src.models import Channel, IncomingMessage

HDE_FIELD_CATEGORY_BOT = 23
HDE_FIELD_ESCALATION_BOT = 25
HDE_FIELD_SUMMARY_BOT = 29


class HDEAdapter(ChannelAdapter):
    def parse(self, payload: dict[str, Any]) -> IncomingMessage:
        user_id = payload.get("user_id") or payload.get("client_id") or payload.get("ticket_id")
        return IncomingMessage(
            user_id=str(user_id),
            channel=Channel.HDE,
            text=str(payload.get("text") or payload.get("message") or ""),
            attachments=payload.get("attachments") or [],
        )

    async def send(self, user_id: str, text: str) -> None:
        logger.info(
            "hde_send_stub",
            user_id=user_id,
            text=text[:200],
            fields=[HDE_FIELD_CATEGORY_BOT, HDE_FIELD_ESCALATION_BOT, HDE_FIELD_SUMMARY_BOT],
        )
