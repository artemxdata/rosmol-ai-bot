from __future__ import annotations

from typing import Any

from loguru import logger

from src.channels.base import ChannelAdapter
from src.models import Channel, IncomingMessage


class VKAdapter(ChannelAdapter):
    def parse(self, payload: dict[str, Any]) -> IncomingMessage:
        message = payload.get("object", {}).get("message", payload)
        return IncomingMessage(
            user_id=str(message.get("from_id") or message.get("user_id")),
            channel=Channel.VK,
            text=str(message.get("text") or ""),
            attachments=message.get("attachments") or [],
        )

    async def send(self, user_id: str, text: str) -> None:
        logger.info("vk_send_stub", user_id=user_id, text=text[:200])
