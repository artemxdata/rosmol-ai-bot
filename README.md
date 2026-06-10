# Rosmol AI Bot

RAG + LLM бот для ответов на вопросы о форумах Росмолодёжи, грантах, ФГАИС и технических проблемах.

## Быстрый старт

```bash
cp .env.example .env
docker compose up -d postgres redis qdrant
alembic upgrade head
python scripts/init_qdrant.py
uvicorn src.main:app --reload
```

Полный стек:

```bash
docker compose up -d
```

По умолчанию Docker-образ собирается без тяжёлых локальных ML-зависимостей
(`torch`, `FlagEmbedding`), чтобы dev-стек стартовал быстро. Для образа с локальными
embedding/reranker-моделями:

```bash
INSTALL_ML=true docker compose build app migrate init-qdrant
```

Проверки:

```bash
docker compose ps
python scripts/init_qdrant.py
pytest
```

Для GigaChat используй один из вариантов:

- `GIGACHAT_API_KEY` — OAuth authorization key/credentials для получения токена.
- `GIGACHAT_ACCESS_TOKEN` — готовый access token. Если токен просрочен, `scripts/test_gigachat.py` вернёт `Unauthorized`.

## Архитектура

Полный документ: `docs/architecture.md`.

Ключевые правила:

- LLM не отвечает без найденных опубликованных чанков.
- PII маскируется до LLM и Qdrant.
- Все промпты лежат в `src/llm/prompts.py`.
- Внешние API-вызовы асинхронные.
- Каждый запрос получает `request_id` и полный trace.
