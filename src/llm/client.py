from __future__ import annotations

import asyncio
from time import perf_counter

from loguru import logger

from src.config import get_settings


class GigaChatClient:
    def __init__(
        self,
        api_key: str | None = None,
        verify_ssl: bool | None = None,
        scope: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self.api_key = self._normalize_secret(api_key or settings.gigachat_api_key)
        self.access_token = self._normalize_secret(settings.gigachat_access_token)
        self.verify_ssl = settings.gigachat_verify_ssl if verify_ssl is None else verify_ssl
        self.scope = scope or settings.gigachat_scope
        self.base_url = settings.gigachat_base_url or None
        self.auth_url = settings.gigachat_auth_url or None
        self.timeout = timeout

    async def generate(
        self,
        model: str,
        system: str,
        user: str,
        response_format: str = "text",
        temperature: float = 0.3,
        max_tokens: int = 1500,
    ) -> str:
        if not self.api_key and not self.access_token:
            raise RuntimeError("GIGACHAT_API_KEY or GIGACHAT_ACCESS_TOKEN is not configured")

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await self._generate_once(
                    model=model,
                    system=system,
                    user=user,
                    response_format=response_format,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                last_error = exc
                if not self._is_retryable(exc) or attempt == 2:
                    break
                await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(
            f"GigaChat request failed after retries: {self._summarize_exception(last_error)}"
        ) from last_error

    async def _generate_once(
        self,
        model: str,
        system: str,
        user: str,
        response_format: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        from gigachat import GigaChat

        payload: dict[str, object] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        started_at = perf_counter()
        async with GigaChat(
            **self._auth_kwargs(),
            base_url=self.base_url,
            auth_url=self.auth_url,
            model=model,
            verify_ssl_certs=self.verify_ssl,
            scope=self.scope,
            timeout=self.timeout,
        ) as client:
            response = await client.achat(payload)

        latency_ms = int((perf_counter() - started_at) * 1000)
        usage = getattr(response, "usage", None)
        logger.info("gigachat_response", model=model, latency_ms=latency_ms, usage=usage)
        return response.choices[0].message.content

    def _auth_kwargs(self) -> dict[str, str]:
        if self.access_token:
            return {"access_token": self.access_token}
        if self.api_key and self._looks_like_access_token(self.api_key):
            return {"access_token": self.api_key}
        return {"credentials": self.api_key}

    @staticmethod
    def _normalize_secret(secret: str | None) -> str:
        if not secret:
            return ""
        value = secret.strip().strip('"').strip("'")
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return value

    @staticmethod
    def _looks_like_access_token(value: str) -> bool:
        return value.count(".") >= 2

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        from gigachat.exceptions import (
            AuthenticationError,
            BadRequestError,
            ForbiddenError,
            NotFoundError,
            UnprocessableEntityError,
        )

        non_retryable = (
            AuthenticationError,
            BadRequestError,
            ForbiddenError,
            NotFoundError,
            UnprocessableEntityError,
        )
        return not isinstance(exc, non_retryable)

    @staticmethod
    def _summarize_exception(exc: Exception | None) -> str:
        if exc is None:
            return "unknown error"
        text = str(exc)
        for marker in ("Unauthorized", "Forbidden", "Bad Request", "Too Many Requests"):
            if marker.lower() in text.lower():
                return marker
        return text.splitlines()[0][:300]
