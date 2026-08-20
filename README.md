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
embedding/reranker-моделями используется отдельный compose-файл, CPU-only PyTorch 2.6+
wheel и отдельный image tag.
ML extra дополнительно фиксирует `transformers<5`, потому что текущий `FlagEmbedding`
несовместим с major-версией 5.x для bge-reranker.
Обычный `app` при этом остаётся лёгким:
ML one-shot сервисы запускаются от root только для записи в Docker named volumes с
HuggingFace/Torch cache; обычный `app` runtime остаётся под пользователем `app`.

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml build index-kb
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm ml-check
```

Проверка с загрузкой моделей bge-m3 и bge-reranker-v2-m3:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm ml-check --load-embedder
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm ml-check --load-reranker
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm ml-check --load-models
```

Индексация базы знаний в Qdrant:

```bash
readonly REVIEWED_KB_SEED_SHA256='<reviewed-lowercase-64-character-seed-sha256>'
[[ "$REVIEWED_KB_SEED_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum data/knowledge_base_seed.json | cut -d ' ' -f 1)" \
  = "$REVIEWED_KB_SEED_SHA256"
docker compose up -d qdrant
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm \
  -e "EXPECTED_KB_SEED_SHA256=$REVIEWED_KB_SEED_SHA256" index-kb
```

Обычная команда делает upsert и не удаляет существующие точки. Для release-замены seed сначала
выполни validation и сохрани контрольное количество точек, затем явно добавь `--prune-stale`:

```bash
readonly REVIEWED_RELEASE_SEED_SHA256='<reviewed-lowercase-64-character-seed-sha256>'
[[ "$REVIEWED_RELEASE_SEED_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum data/knowledge_base_seed.json | cut -d ' ' -f 1)" \
  = "$REVIEWED_RELEASE_SEED_SHA256"
python scripts/index_kb.py --validate-only --forums-registry data/forums_registry.json
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm \
  -e "EXPECTED_KB_SEED_SHA256=$REVIEWED_RELEASE_SEED_SHA256" index-kb \
  python scripts/index_kb.py --path data/knowledge_base_seed.json \
    --expected-seed-sha256 "$REVIEWED_RELEASE_SEED_SHA256" \
    --forums-registry data/forums_registry.json --prune-stale
```

`--prune-stale` удаляет из коллекции точки, которых нет среди опубликованных записей текущего
seed; `draft` и `archived` никогда не индексируются. Любое успешное изменение полной KB очищает
semantic response cache. После live-индексации перезапусти `app/app-ml`, чтобы сбросить также
process-local keyword snapshot. Не используй `--prune-stale` для частичной индексации или до
проверки кандидата. Любой реальный index run без reviewed exact SHA-256 или с изменившимися bytes
останавливается до Qdrant либо перед объявлением успеха. `EXPECTED_KB_SEED_SHA256` передаётся
только one-shot index-контейнеру и не является постоянной runtime-настройкой.

Для поиска индексируется расширенный `embedding_text`: исходный ответ плюс intent/topic/forum
и примеры пользовательских формулировок. В ответах пользователю сохраняется исходный `text_clean`.

Локальный `/ask` с реальными bge-моделями можно поднять отдельным ML runtime на порту 8001:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml up -d app-ml
curl http://localhost:8001/ready
```

`app-ml` использует тот же Qdrant/PostgreSQL/Redis. Для локального Docker Desktop он
выгружает bge-m3 embedder после retrieval (`ML_UNLOAD_EMBEDDER_AFTER_USE=true`), но держит
reranker прогретым между запросами (`ML_UNLOAD_RERANKER_AFTER_USE=false`). Это снижает
latency сложных `/ask`-запросов без одновременного удержания двух тяжёлых моделей в памяти.
Старый `ML_UNLOAD_AFTER_USE` остаётся fallback-настройкой, если раздельные флаги не заданы.
Analyzer дополнительно страхуется локальным `data/forums_registry.json`: если пользователь явно
назвал форум, RAG фильтруется по этому форуму даже при пустом `forum_normalized` от LLM.
Для multi-question запросов reranker получает компактный source-preserving набор кандидатов,
чтобы не смешивать форумы и не прогонять лишние пары `query x chunk` на CPU.

Количественный eval `/ask` для локальной проверки качества:

```bash
python eval/run_ask.py --target http://localhost:8001/ask --auto-smoke-cases --max-smoke-cases 50
```

Скрипт пишет JSON/Markdown отчёты в `reports/`, читает trace из PostgreSQL и считает pass rate,
expected chunk hit rate, escalation/cache/source-chunk rate, latency, LLM tokens и оценочную стоимость.
Для калибровки RAG-порогов дополнительно выводятся `reranker_score` и
`low_confidence_expected_chunk_hits`: если ожидаемый чанк найден, но ответ ушёл в `low_confidence`,
это сигнал к настройке `RERANKER_THRESHOLD_LOW/HIGH` на golden set, а не к ручному снижению порогов
по одному запросу.
Если `.env` содержит Docker-host `postgres`, eval автоматически пробует локальный fallback `localhost`.
Для ручного golden-set можно передать `--cases path/to/ask_eval_set.json`, а DSN trace-БД переопределить через
`--trace-dsn` или `ASK_EVAL_POSTGRES_DSN`.

Ручная проверка сложных вопросов с видимым RAG/trace-разбором:

```bash
python scripts/manual_ask.py ^
  --text "Мне 17 лет, я из другого региона и хочу поехать на форум. Пустят ли меня, кто оплачивает дорогу и где я буду жить?" ^
  --target http://localhost:8001/ask
```

Batch-прогон подготовленного набора:

```bash
python scripts/manual_ask.py ^
  --file data/manual_complex_queries.json ^
  --target http://localhost:8001/ask ^
  --output reports/manual_complex_queries.json
```

Скрипт печатает в терминал ответ, `request_id`, masked text, cache hit, escalation reason,
top retrieved/reranked chunks, reranker score, LLM usage/cost и события графа по узлам.
Это инструмент для ручной инспекции качества, а не замена `golden_set.json`.
Если запрос попал в semantic cache, в отчёте будет `cache_hit=True`, а graph events/chunks могут быть пустыми;
для просмотра полного RAG-пути измени формулировку вопроса или используй новый запрос.

Глобальный smoke-прогон по всем форумам из KB:

```powershell
python eval/build_forum_smoke_set.py --per-forum 1 --output reports/forum_smoke_set.json
python eval/run_ask.py ^
  --cases reports/forum_smoke_set.json ^
  --target http://localhost:8001/ask ^
  --output reports/forum_ask_eval.json
python eval/summarize_forum_ask.py ^
  --ask-metrics reports/forum_ask_eval.json ^
  --output reports/forum_ask_summary.json
```

Итоговая Markdown-таблица будет в `reports/forum_ask_summary.md`: pass/chunk hit/latency по
каждому форуму и флаг, если RAG увидел чанки другого форума.

Сбалансированный ask-eval набор из KB seed можно подготовить без Docker:

```bash
python eval/build_ask_eval_set.py --max-cases 100
python eval/run_ask.py --cases reports/ask_eval_set.generated.json --target http://localhost:8001/ask
```

Перед полноценным golden-set прогоном проверь качество набора:

```bash
python eval/validate_golden_set.py \
  --golden data/golden_set.json \
  --kb-seed data/knowledge_base_seed.json \
  --min-cases 50
```

Валидатор проверяет JSON-массив, уникальные `id`, непустой вопрос, ожидаемые chunk IDs,
существование chunk IDs в KB seed и распределение по категориям/форумам/сложности.

Аудит покрытия и чистоты KB seed:

```powershell
python scripts/audit_kb_seed.py ^
  --path data/knowledge_base_seed.json ^
  --forums-registry data/forums_registry.json ^
  --min-forum-chunks 5 ^
  --min-forum-topics 3 ^
  --output reports/kb_audit_coverage.json ^
  --markdown reports/kb_audit_coverage.md
```

Отчёт показывает ошибки metadata, template artifacts, дубли ответов, форумы из registry без
published chunks и форумы/темы с низким покрытием.

Рекомендации по RAG-порогам из готового ask-eval отчёта:

```bash
python eval/suggest_rag_thresholds.py --metrics reports/ask_eval.json
```

Скрипт не меняет `.env`; он показывает таблицу low-threshold candidates и безопасную рекомендацию,
которую нужно подтверждать на согласованном golden set.

Офлайн-проверка grounded generation по готовому ask-eval отчёту:

```bash
python eval/run_generation.py \
  --cases reports/ask_eval.json \
  --output reports/generation_eval.json \
  --markdown reports/generation_eval.md
```

Отчёт не вызывает LLM и не копирует тексты вопросов/ответов в результат: он сохраняет только
case id, статусы проверок и агрегаты. Проверяются source context, expected chunk hit,
unknown source markers, hallucination flags от verifier и forbidden phrases.

Итоговый quality gate по готовым JSON-отчётам:

```bash
python eval/check_quality_gate.py \
  --retrieval-metrics reports/retrieval_eval.json \
  --ask-metrics reports/ask_eval.json \
  --generation-metrics reports/generation_eval.json \
  --threshold-suggestions reports/rag_threshold_suggestions.json
```

Gate падает ненулевым кодом при провале core-метрик и оставляет warnings для калибровочных решений,
которые нельзя принимать без расширенного golden set.

One-command локальный quality suite, который сам запускает retrieval eval, ask eval, grounded
generation eval, threshold suggestions и quality gate:

```bash
python eval/run_quality_suite.py \
  --auto-smoke-cases \
  --max-smoke-cases 20 \
  --top-k 10 \
  --forum-smoke \
  --min-forums-total 29 \
  --target http://localhost:8001/ask \
  --output-dir reports/quality_suite_smoke \
  --no-fail
```

Для production-like retrieval диагностики используй `--top-k 10`: граф подаёт в reranker
расширенный candidate pool, а отчёт всё равно считает строгий `recall_at_5` и общий
`recall_at_10`. `recall_at_5` показывает качество раннего ранжирования, `recall_at_10`
показывает, попадает ли правильный chunk в кандидаты до reranker.
Retrieval-отчёт также пишет `mrr`, `avg_expected_rank` и гистограмму рангов, чтобы
отделять проблемы покрытия KB от проблем ранжирования.

Для полноценного golden-set запуска убери `--auto-smoke-cases` и передай `--golden`/`--ask-cases`.
Флаг `--forum-smoke` дополнительно строит по одному smoke-кейсу на каждый форум из KB,
прогоняет их через `/ask`, пишет `forum_ask_summary.*` и добавляет проверки в quality gate:
pass rate, expected chunk hit rate, число проблемных форумов и минимальное покрытие форумов.
Главный обзорный файл после запуска: `reports/quality_suite_smoke/summary.md`.

Анализ приватной выгрузки тикетов для подготовки golden set и калибровки RAG:

```bash
python scripts/analyze_ticket_dataset.py \
  --input data/private/tickets/RAG_Dataset.xlsx \
  --out-dir data/private/tickets/analysis \
  --max-golden 800 \
  --max-pairs 1000
```

Скрипт пишет только локальные приватные артефакты: `tickets_normalized.jsonl`,
`golden_set_candidates.json`, `reranker_calibration_pairs.jsonl`, `intent_taxonomy.csv`,
`top_questions.md` и `kb_gap_report.md`. Папка `data/private/` игнорируется Git.

Выгрузки HDE/тикетов являются временным приватным сырьем для анализа качества, разметки
и архитектурных решений. Они не являются частью проекта, не коммитятся, не переносятся
на сервер и не должны попадать в `data/`, Docker images или долгоживущие отчеты.
После ручной проверки в проект можно переносить только sanitized golden set: без исходных
текстов диалогов, контактов, ФИО, ID пользователей и других персональных данных.
После формирования такого набора сырые выгрузки и производные приватные файлы в
`data/private/tickets/` нужно удалить из рабочей копии.

Сборка приватного обезличенного банка сильных ответов из тикетов:

```bash
python scripts/build_ticket_answer_bank.py \
  --tickets data/private/tickets/analysis/tickets_normalized.jsonl \
  --kb-seed data/knowledge_base_seed.json \
  --output data/private/tickets/curation/ideal_answer_bank_candidates.json \
  --limit 300 \
  --min-quality-score 9
```

Скрипт выбирает содержательные неэскалационные ответы специалистов, маскирует PII, отбрасывает
личные письма/жалобы/подписи и пишет приватные артефакты `ideal_answer_bank_candidates.json`
и `ideal_answer_bank_candidates.md`. Это не `golden_set`: банк нужен для расширения RAG.
После ревизии подтверждённые записи переносятся в `knowledge_base_seed.json` как `draft`,
затем проходят редакторскую проверку, публикацию и переиндексацию.

Промоутинг answer bank в KB-совместимые черновики без изменения основной базы:

```bash
python scripts/promote_answer_bank_to_kb.py \
  --include-candidates \
  --limit 300 \
  --merged-output data/private/tickets/curation/knowledge_base_seed_with_answer_bank_drafts.json
python scripts/index_kb.py \
  --path data/private/tickets/curation/kb_draft_answer_bank.json \
  --validate-only
```

Скрипт пишет только приватные review-артефакты `kb_draft_answer_bank.json/.md`.
Все записи остаются `status=draft`; перенос в `data/knowledge_base_seed.json` выполняется
только после ручной проверки актуальности и отсутствия персональных данных.

Для локального sandbox-теста можно проиндексировать приватный merged-кандидат в отдельную
Qdrant-коллекцию и временно направить туда `/ask` через `QDRANT_KNOWLEDGE_COLLECTION`.
Дефолт остаётся `knowledge_base`; этот режим не предназначен для production.

```bash
python scripts/promote_answer_bank_to_kb.py \
  --include-candidates \
  --publish-for-sandbox \
  --limit 300 \
  --output data/private/tickets/curation/kb_sandbox_answer_bank.json \
  --merged-output data/private/tickets/curation/knowledge_base_seed_answer_bank_sandbox.json
readonly REVIEWED_SANDBOX_SEED_SHA256='<reviewed-lowercase-64-character-seed-sha256>'
[[ "$REVIEWED_SANDBOX_SEED_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum data/private/tickets/curation/knowledge_base_seed_answer_bank_sandbox.json | cut -d ' ' -f 1)" \
  = "$REVIEWED_SANDBOX_SEED_SHA256"
python scripts/init_qdrant.py --knowledge-collection knowledge_base_answer_bank_sandbox
python scripts/index_kb.py \
  --collection knowledge_base_answer_bank_sandbox \
  --path data/private/tickets/curation/knowledge_base_seed_answer_bank_sandbox.json \
  --expected-seed-sha256 "$REVIEWED_SANDBOX_SEED_SHA256"
```

Подготовка приватных eval-наборов из ticket analysis:

```bash
python scripts/prepare_ticket_eval_sets.py \
  --candidates data/private/tickets/analysis/golden_set_candidates.json \
  --kb-seed data/knowledge_base_seed.json \
  --out-dir data/private/tickets/eval
```

Скрипт создаёт `ticket_ask_eval_candidates.json`, `ticket_ask_eval_smoke.json`,
`ticket_retrieval_golden_candidates.json`, `ticket_manual_review_sample.csv` и отчёт.
Автоматические chunk labels считаются weak labels и требуют ручной проверки перед переносом
в публичный `data/golden_set.json`.

Калибровка reranker на ticket pairs запускается в ML runtime:

```bash
python scripts/calibrate_reranker_pairs.py \
  --pairs data/private/tickets/analysis/reranker_calibration_pairs.jsonl \
  --output data/private/tickets/eval/reranker_calibration_report.json \
  --limit 200
```

Этот скрипт использует `FlagEmbedding`/`bge-reranker-v2-m3`; в обычной лёгкой `.venv`
он честно завершится с диагностикой отсутствующей ML-зависимости.

Если ML-зависимости подняты только в Docker, приватные ticket pairs нужно передавать в контейнер
через stdin. Так сырая выгрузка и производные приватные пары не попадают в Docker image и не
записываются в файловую систему контейнера:

```powershell
Get-Content data\private\tickets\analysis\reranker_calibration_pairs.jsonl -TotalCount 20 |
  docker compose -f docker-compose.yml -f docker-compose.ml.yml run --rm -T --no-deps app-ml python scripts/calibrate_reranker_pairs.py --pairs - --output - --limit 20
```

При `--output -` скрипт печатает только агрегированный JSON-отчёт без `worst_positive_cases`,
исходных текстов тикетов и `source_ticket_id`. Если в отчёте есть `quality_warnings`
(`low_positive_at_1_rate_review_pairs_or_reranker`, `many_hard_negatives_beat_positive`,
`recommended_threshold_has_low_precision`), рекомендованный threshold нельзя переносить в `.env`
без ручной проверки пар или повторной калибровки на подтверждённом golden set.

Для короткого smoke-теста индексации можно временно переопределить команду:

```bash
readonly REVIEWED_SMOKE_SEED_SHA256='<reviewed-lowercase-64-character-seed-sha256>'
[[ "$REVIEWED_SMOKE_SEED_SHA256" =~ ^[0-9a-f]{64}$ ]]
test "$(sha256sum data/knowledge_base_seed.json | cut -d ' ' -f 1)" \
  = "$REVIEWED_SMOKE_SEED_SHA256"
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml run --rm \
  -e "EXPECTED_KB_SEED_SHA256=$REVIEWED_SMOKE_SEED_SHA256" index-kb sh -eu -c \
  'python scripts/init_qdrant.py && python scripts/index_kb.py \
   --expected-seed-sha256 "$EXPECTED_KB_SEED_SHA256" --limit 20'
```

Если в уже существующую Qdrant-коллекцию нужно добавить ASCII filter keys без полного пересчёта embeddings:

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile ml exec app-ml python scripts/backfill_qdrant_filter_keys.py
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
