from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.models import IncomingMessage


class ChannelAdapter(ABC):
    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> IncomingMessage:
        raise NotImplementedError

    @abstractmethod
    async def send(self, user_id: str, text: str) -> None:
        raise NotImplementedError
