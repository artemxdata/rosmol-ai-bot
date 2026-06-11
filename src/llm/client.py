from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.llm.usage import record_llm_usage


class CloudRuLLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        chat_completions_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self.api_key = self._normalize_secret(api_key or settings.cloud_ru_api_key)
        self.chat_completions_url = self._normalize_url(
            chat_completions_url or settings.cloud_ru_chat_completions_url
        )
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
        if not self.api_key:
            raise RuntimeError("CLOUD_RU_API_KEY is not configured")

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
            f"Cloud.ru LLM request failed after retries: {self._summarize_exception(last_error)}"
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
        payload: dict[str, Any] = {
            "model": model,
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
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.chat_completions_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json=payload,
            )
            response.raise_for_status()

        latency_ms = int((perf_counter() - started_at) * 1000)
        data = response.json()
        usage = data.get("usage")
        record_llm_usage(model, latency_ms, usage)
        logger.info("cloud_ru_llm_response", model=model, latency_ms=latency_ms, usage=usage)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Cloud.ru LLM response has unexpected format") from exc

    @staticmethod
    def _normalize_secret(secret: str | None) -> str:
        if not secret:
            return ""
        value = secret.strip().strip('"').strip("'")
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return value

    @staticmethod
    def _normalize_url(value: str) -> str:
        normalized = value.strip().strip('"').strip("'")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("Cloud.ru chat completions URL must start with http:// or https://")
        return normalized

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {408, 409, 429, 500, 502, 503, 504}
        return isinstance(exc, httpx.TransportError)

    @staticmethod
    def _summarize_exception(exc: Exception | None) -> str:
        if exc is None:
            return "unknown error"
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            text = exc.response.text.splitlines()[0][:200]
            if status_code == 401:
                return "Unauthorized"
            if status_code == 403:
                return "Forbidden"
            return f"HTTP {status_code}: {text}"
        return str(exc).splitlines()[0][:300]
