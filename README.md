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
- `CLOUD_RU_MODEL` — совместимый alias для simple-модели; можно оставить `ai-sage/GigaChat3-10B-A1.8B`.
- `CLOUD_RU_MODEL_SIMPLE` — модель для типовых простых вопросов, по умолчанию `ai-sage/GigaChat3-10B-A1.8B`.
- `CLOUD_RU_MODEL_COMPLEX` — модель для нетиповых/сложных вопросов, по умолчанию `GigaChat/GigaChat-2-Max`.
- `CLOUD_RU_MODEL_ANALYZER` и `CLOUD_RU_MODEL_JUDGE` — опциональные переопределения для анализатора и verifier judge.
- `CLOUD_RU_MODEL_COMPLEX_INPUT_PRICE_RUB_PER_MILLION` и `CLOUD_RU_MODEL_COMPLEX_OUTPUT_PRICE_RUB_PER_MILLION` — тариф Max для trace-учёта стоимости, по умолчанию `569.34`.
- `CLOUD_RU_MODEL_SIMPLE_INPUT_PRICE_RUB_PER_MILLION` и `CLOUD_RU_MODEL_SIMPLE_OUTPUT_PRICE_RUB_PER_MILLION` — тариф simple-модели; по умолчанию `0`, пока не зафиксирован коммерческий тариф.
- `API_AUTH_TOKEN` — опциональный токен для `/ask`; если задан, клиент должен передать `X-API-Key` или `Authorization: Bearer ...`.
- `WEBHOOK_AUTH_TOKEN` — опциональный токен для `/webhook/*`; если задан, webhook должен передать `X-Webhook-Secret` или `Authorization: Bearer ...`.
- `REQUEST_TIMEOUT_SECONDS` — общий timeout выполнения графа ответа, по умолчанию `45`.

Перед Query Analyzer работает консервативный pre-routing: 10B выбирается только для явно типовых FAQ-сигналов вроде регистрации или подачи заявки. Неоднозначные, персональные, составные и неизвестные формулировки идут в complex-route и используют Max-модель, если задан `CLOUD_RU_MODEL_COMPLEX`.

Для Max на Cloud.ru: входные токены — 569.34 руб./млн, генерируемые токены — 569.34 руб./млн, контекст до 131K токенов.

OAuth flow и developers.sber.ru здесь не используются.
Проверка доступа:

```bash
python scripts/test_cloud_ru.py
python scripts/test_cloud_ru.py --complex
```

## Архитектура

Полный документ: `docs/architecture.md`.

Ключевые правила:

- LLM не отвечает без найденных опубликованных чанков.
- PII маскируется до LLM и Qdrant.
- Все промпты лежат в `src/llm/prompts.py`.
- Внешние API-вызовы асинхронные.
- Каждый запрос получает `request_id` и полный trace.
