from __future__ import annotations

from typing import Any

from src.config import get_settings
from src.models import Chunk, Question, Session

QUERY_ANALYZER_SYSTEM = """
Ты Query Analyzer бота Росмолодёжи. Разбери сообщение пользователя в JSON.
Определи форум, нормализованное название форума, категорию, темы, составные вопросы,
сложность simple/complex, нужна ли уточняющая реплика или эскалация.

Приоритеты: технические проблемы > мероприятия/форумы > рекомендации > гранты > общее.
Не отвечай на вопрос пользователя. Верни только валидный JSON без markdown.
""".strip()

RESPONSE_GENERATOR_SYSTEM = """
Ты AI-бот Росмолодёжи. Отвечай только на основе переданных источников.
Если в источниках нет факта, не додумывай. Кратко объясни, что информации нет,
и предложи передать обращение специалисту.
Сохраняй дружелюбный деловой тон. Не раскрывай служебные метки.
""".strip()

LLM_JUDGE_SYSTEM = """
Ты проверяешь ответ бота на галлюцинации. Сравни ответ только с источниками.
Верни JSON: {"has_hallucination": bool, "confidence": float, "details": string}.
""".strip()


def build_analyzer_user(
    message: str,
    session: Session | None,
    memory: Any | None,
    routing_hint: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        [
            f"Версия промпта: {get_settings().prompt_version}",
            f"Сообщение: {message}",
            f"Предварительная маршрутизация: {routing_hint or {}}",
            f"Сессия: {session.model_dump_json() if session else '{}'}",
            f"Долгосрочная память: {memory.model_dump_json() if memory else '{}'}",
        ]
    )


def build_generator_user(
    questions: list[Question],
    chunks: list[Chunk],
    session: Session | None,
    params: dict[str, Any] | None = None,
) -> str:
    sources = []
    for idx, chunk in enumerate(chunks, start=1):
        sources.append(
            f"[src:{chunk.chunk_id}] Источник {idx}\n"
            f"Метаданные: {chunk.metadata}\n"
            f"Текст: {chunk.text}"
        )
    return "\n\n".join(
        [
            f"Вопросы: {[question.model_dump() for question in questions]}",
            f"Параметры пользователя: {params or {}}",
            f"Сессия: {session.model_dump() if session else {}}",
            "Источники:",
            "\n\n".join(sources),
        ]
    )


def build_judge_user(response: str, sources: list[Chunk]) -> str:
    return "\n\n".join(
        [
            f"Ответ бота:\n{response}",
            "Источники:",
            "\n\n".join(f"[src:{chunk.chunk_id}] {chunk.text}" for chunk in sources),
        ]
    )
