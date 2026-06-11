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

Для LLM используется Cloud.ru Evolution Foundation Models, OpenAI-compatible endpoint:

- `CLOUD_RU_API_KEY` — API-ключ Cloud.ru, отправляется как `Authorization: Bearer ...`.
- `CLOUD_RU_CHAT_COMPLETIONS_URL` — по умолчанию `https://foundation-models.api.cloud.ru/v1/chat/completions`.
- `CLOUD_RU_MODEL` — по умолчанию `ai-sage/GigaChat3-10B-A1.8B`.

OAuth flow и developers.sber.ru здесь не используются.
Проверка доступа:

```bash
python scripts/test_cloud_ru.py
```

## Архитектура

Полный документ: `docs/architecture.md`.

Ключевые правила:

- LLM не отвечает без найденных опубликованных чанков.
- PII маскируется до LLM и Qdrant.
- Все промпты лежат в `src/llm/prompts.py`.
- Внешние API-вызовы асинхронные.
- Каждый запрос получает `request_id` и полный trace.
