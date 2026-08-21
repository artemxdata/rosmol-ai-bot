# Сравнение self-hosted RAG-платформ: Dify, Flowise и RAGFlow

**Дата среза:** 28 июля 2026 года

**Контекст:** возможная альтернатива собственному ядру `FastAPI + LangGraph + Qdrant + PostgreSQL + Redis` с Cloud.ru GigaChat через OpenAI-совместимый API, каскадом моделей и обязательной деперсонализацией ПДн до отправки в облако.

**Границы работы:** только анализ открытой официальной документации, репозиториев и исходного кода. Платформы не устанавливались и не запускались; серверы и production-контур не использовались.

## 1. Как читать выводы

- «Поддерживает OpenAI-compatible» означает, что платформа позволяет задать собственный `base_url`, model ID и API key. Это ещё не доказывает полную совместимость с Cloud.ru: отдельно должны совпасть Chat Completions, streaming, tool calling, ошибки, таймауты и жизненный цикл Bearer-токена.
- «Можно поставить шаг деперсонализации перед LLM» не равно «ПДн невозможно отправить в облако по другому пути». Визуальный граф защищает только правильно построенную ветку; глобальная гарантия требует обязательного шлюза или middleware, который нельзя обойти настройкой другого flow.
- «Русский поддерживается» означает отсутствие платформенного запрета и возможность выбрать multilingual embedding/rerank-модель. Качество русского поиска подтверждается только нашим calibration/holdout-набором.
- Если функция не обнаружена в изученных официальных материалах, ниже написано «не найдено», а не «технически невозможно реализовать».
- Оценки ресурсов — не результаты собственного бенчмарка. Приведены официальные минимумы либо явно обозначенные production-рекомендации вендора.

## 2. Короткий результат

По совокупности требований **Dify выглядит наиболее пригодным кандидатом для последующего ограниченного PoC**, но не готовой заменой текущего ядра. У него есть произвольный OpenAI-compatible endpoint, нативные hybrid retrieval и reranking, Qdrant как поддерживаемое хранилище, наиболее полная встроенная эксплуатационная панель и — главное отличие — официальное Input Moderation API Extension, способное заменить вход перед normal app workflow graph. При этом raw message уже принят и сохранён внутри Dify, поэтому extension не является pre-ingress compliance boundary.

Критичная оговорка одинакова для всех трёх платформ: **внутреннего workflow-узла недостаточно как единственной security boundary**. Сырой запрос уже попадает в саму платформу, может оказаться в истории, логах или трассировке, а другой граф может обойти узел. Для нашего кейса безопаснее сохранить текущую локальную FastAPI-деперсонализацию перед платформой либо обязать все cloud-bound model calls проходить через отдельный fail-closed PII gateway и закрыть прямой egress к Cloud.ru.

Предварительный порядок пригодности для данного кейса:

1. **Dify — подходит с существенными оговорками.** Наиболее сбалансирован по RAG, управляемости и документированному pre-LLM hook.
2. **RAGFlow — подходит с существенными оговорками.** Самый глубокий document-RAG, но самый тяжёлый, без Qdrant и без глобального pre-LLM hook.
3. **Flowise — подходит с существенными оговорками, скорее как orchestration/UI-слой.** Самый лёгкий вход и гибкий canvas, но штатный Qdrant-узел не воспроизводит текущий dense+sparse+rerank pipeline, а часть governance/eval-функций коммерческая.

Это **не решение о миграции**. Ни одна платформа по документации не закрывает одновременно неизбежную деперсонализацию, воспроизводимое версионирование опубликованной БЗ, существующий Qdrant hybrid pipeline и текущую fail-safe логику без дополнительного собственного слоя.

## 3. Сводная сравнительная таблица

| Ось | Dify | Flowise | RAGFlow |
|---|---|---|---|
| **Архитектура и стек** | Python API/worker, Flask, Celery; Next.js/React/TypeScript UI. Текущий quick start описывает 7 core services, 8 dependent services и 1 init task: PostgreSQL, Redis, vector DB, Nginx, SSRF proxies, Sandbox, plugin daemon и другие процессы. | TypeScript/Node.js-монорепозиторий: Express/TypeORM backend, React/Vite UI, LangChain.js/LlamaIndex-узлы. Минимально — один контейнер и SQLite; queue mode добавляет Redis/BullMQ, main и workers, а для production вендор рекомендует PostgreSQL. | Python-centric backend и DeepDoc parsing/OCR, React/TypeScript UI; в текущем репозитории есть также Go-сервисы/модули. Официальный Compose остаётся MySQL-centric и тянет MinIO, Valkey/Redis и один выбранный document engine: Elasticsearch, Infinity, OceanBase, OpenSearch либо SeekDB. PostgreSQL metadata-конфигурация есть, но Compose его не поднимает. Nginx входит в app image; отдельный DeepDoc-сервис опционален. |
| **Относительная тяжесть** | Средняя/выше средней. Официальный минимум — **2 CPU, 4 GiB RAM**, но quick start — это 15 постоянно работающих контейнеров плюс init task, а не один лёгкий сервис. Официального production sizing нет. | Самый лёгкий минимальный запуск: один образ и SQLite. Для масштабируемого production вендор рекомендует **2 main + 4 workers**, начиная с **4 vCPU/8 GB на экземпляр**, плюс внешние БД; это уже не лёгкая конфигурация. | Самый тяжёлый. Официальный минимум — **4 CPU, 16 GB RAM, 50 GB disk**. Document engine и parsing/OCR заметно увеличивают footprint. Готовые образы — x86. |
| **Произвольный OpenAI-compatible LLM** | **Да.** Официальный OpenAI-API-compatible plugin принимает API Base URL, API key и model name. | **Да.** ChatOpenAI/ChatOpenAI Custom передаёт `baseURL`, API key и при необходимости custom headers. | **Да, с нормализацией пути.** Provider `OpenAI-API-Compatible` принимает Base URL, API key и model ID; chat/embedding adapters добавляют `/v1`, если в пути нет `/vN`. |
| **Cloud.ru GigaChat: предварительная совместимость** | Структурно подходит для `base_url + Bearer`; нужен контрактный тест Chat Completions, streaming, tool calls, ошибок и обновления токена. | Структурно подходит; tool/agent flow дополнительно зависит от function/tool calling. | Структурно подходит при ожидаемой структуре OpenAI URL; автоматическое обновление нестандартного краткоживущего токена generic provider не подтверждено. |
| **Каскад двух моделей** | Можно собрать workflow с несколькими model nodes и условной маршрутизацией. Политику сложности, timeout/fallback и метрики нужно перенести самостоятельно. | Можно собрать два LLM-узла и Condition/Custom Function. Готовой политики каскада нет. | Разные Agent-компоненты могут использовать разные модели; маршрутизация строится графом. Готовую текущую fail-safe политику придётся воспроизвести. |
| **Документы и парсинг** | PDF, DOCX, XLS/XLSX, CSV, Markdown/MDX, HTML, EPUB, TXT; optional Unstructured расширяет DOC, PPT/PPTX, XML, MSG, EML. | PDF, TXT, Markdown, CSV, JSON/JSONL, DOCX, Excel, PowerPoint, EPUB, HTML/XML, код и большое число connector/loaders. | PDF, DOCX, MD/MDX, TXT, HTML, EML, JSON, CSV, XLS/XLSX, PPT/PPTX, изображения и сканы. Самая развитая layout/OCR-обработка. |
| **Чанкинг** | Standard/legacy modes General и Parent-child с delimiter, length, overlap и cleaning; современный Knowledge Pipeline добавляет расширяемую ingestion-схему и Q&A Processor. Опубликованная chunking structure фиксируется для БЗ. | Несколько splitters, chunk size/overlap, preview, metadata и ручное редактирование чанков. | Layout-aware templates, визуальный ingestion pipeline, parent-child chunks, просмотр и ручная правка результатов parsing/chunking. |
| **Embeddings и русский** | Embedding-модель выбирается; доступны провайдеры и OpenAI-compatible endpoints. `bge-m3` технически подключаем через совместимый provider, но русский holdout обязателен. | OpenAI Custom, Hugging Face, Ollama, LocalAI и другие. `bge-m3` подключаем через один из поддержанных endpoints; отдельного гарантированного «русского режима» нет. | Embedding-модель задаётся на dataset; `bge-m3` приведён в закомментированном конфигурационном примере, но не входит в slim image. Multilingual модель подходит для русского технически, однако отдельного русского benchmark RAGFlow не публикует. |
| **Смена embedding-модели** | Требует переиндексации; часть базовых параметров фиксируется при создании БЗ. | Зависит от Document Store/vector store; для корректной смены модели нужна отдельная коллекция или полная переиндексация. | После появления chunks embedding-модель нельзя просто заменить: требуется удалить chunks и переиндексировать dataset. |
| **Реранкер** | Нативная retrieval-настройка с reranking; доступны provider/plugin модели. Контракт конкретного `bge-reranker-v2-m3` endpoint надо проверить. | Готовые Cohere/Voyage rerank и RRF retriever. Отдельного штатного узла для нашего `bge-reranker-v2-m3` не найдено; нужен HTTP/custom adapter либо внешний retrieval. | Реранкер выбирается отдельно. Закомментированный template-пример называет `bge-reranker-v2`, но модель не встроена в slim image; для нашего `bge-reranker-v2-m3` нужен совместимый `/rerank`-контракт. |
| **Гибридный поиск** | **Да.** Full-text + vector, веса или reranker, Top K и score threshold. | Не как единая универсальная функция. Текущий Qdrant node выполняет dense search и metadata filtering, но не экспонирует sparse vectors/fusion. RRF объединяет результаты нескольких вариантов запроса к одному retriever; произвольный hybrid требует собственного component или внешнего retrieval API. | **Да.** Нативная комбинация keyword/full-text и dense vector; при reranker комбинируются keyword и rerank score. Настраиваются веса, threshold и Top N. |
| **Qdrant** | Поддерживается как vector backend. Совместимость с уже существующей коллекцией не следует из поддержки: Dify владеет схемой коллекции и metadata, поэтому нужно планировать чистую переиндексацию versioned seed. | Поддерживается штатным node. Drop-in reuse существующей коллекции зависит от vector/metadata schema; native dense+sparse Qdrant pipeline отсутствует. | **Не поддерживается как штатный document engine.** Потребуется Elasticsearch/Infinity/OpenSearch/другой поддержанный backend и полная переиндексация либо собственный adapter. |
| **Детерминированный шаг до LLM** | **Да, наиболее явно.** Input Moderation API Extension получает input/query и может вернуть `overridden` перед normal app workflow graph. Это происходит после ingress/persistence; есть также HTTP/Code/plugin nodes. | **Да, внутри конкретного Agentflow.** Custom Function или HTTP node можно поставить первым и передавать дальше только masked state. | **Да, внутри конкретного Agent graph.** HTTP или Python/JS Code component можно поставить между Begin и Retrieval/Agent. |
| **Глобальный обязательный pre-LLM/egress hook** | **Не подтверждён.** Moderation extension включается на уровне приложения и не является доказанным глобальным middleware для всех apps/plugins/routes. | **Не найден.** Корректность зависит от каждого flow. | **Не найден.** Корректность зависит от каждого graph и используемых переменных/history. |
| **Риск сохранения сырого ПДн внутри платформы** | **Подтверждён текущим кодом:** `Conversation.inputs` и `Message.inputs/query` создаются и коммитятся до runner/moderation; moderation trace получает исходные inputs. | Есть: raw input приходит в Flowise, chat messages и traces могут сохраняться до/вне masking node. | Есть: raw `sys.query` и chat history уже находятся в RAGFlow; переданная LLM history также попадает в INFO-log adapter. Опциональный Langfuse получает полные prompts/docs/responses после его включения. |
| **Админ-панель и аналитика** | Наиболее полный встроенный набор: apps/workflows, datasets, logs, node run history, analytics, feedback, токены/стоимость. | Dashboard, flow editor, credentials, Document Stores, conversations, feedback API и пошаговый Agentflow trace; расширенная observability часто через Langfuse/LangSmith/Phoenix/другие системы. | Dataset/chunk/retrieval-test UI, Agent canvas, chat history; отдельный admin service для пользователей и состояния зависимостей. Опциональный Langfuse даёт application/pipeline traces со spans retrieval/ranking/generation и полными prompts/documents/responses. |
| **Версионирование** | Есть история опубликованных app/workflow versions. Restore/export/run конкретной старой версии документация относит к paid plans; доступность этих операций в Community self-hosted надо подтвердить. Immutable release/diff/rollback всей БЗ не найден. | Полноценное immutable KB release/version/rollback не найдено; Record Manager и upsert history этим не являются. | Есть Agent version history/rollback. Для dataset/KB immutable release/diff/rollback не найден; backup volumes — не версия БЗ. |
| **Self-hosted и образы** | Полноценный Docker Compose; много публичных образов и plugin artifacts. | Полноценные npm/Docker/Compose варианты; минимально один основной image. | Полноценный Docker Compose и Helm; slim app image плюс несколько тяжёлых зависимостей и внешние model artifacts. |
| **Доступность зависимостей из РФ** | Официальной гарантии нет. Нужны pinned tags/digests и внутреннее зеркало всех app/dependency/plugin images и packages. | Официальной гарантии нет. Минимальный состав зеркалировать проще, но source build зависит от GitHub/npm/pnpm, production — также от PostgreSQL/Redis/Qdrant images. | Официальной гарантии нет. Документированы Huawei/Aliyun mirrors для некоторых nightly images и `HF_ENDPOINT`, но весь Compose ими не закрывается; требуется собственное полное зеркало. |
| **Лицензия и бесплатный self-hosted** | Community бесплатна, включает core features публичного репозитория, но официально ограничена **Single Workspace**. Dify Open Source License разрешает коммерческое использование, однако multi-tenant environment без письменного разрешения требует commercial license; tenant определён как workspace. При использовании frontend нельзя удалять/менять logo и copyright. | Community-часть — Apache 2.0; enterprise directory и явно помеченные файлы — коммерческие. Основные flow/RAG/API доступны, но Workspaces/RBAC, SSO и встроенные Evaluations относятся к Cloud/Enterprise. Числовых self-hosted Community-лимитов не найдено. | Основной репозиторий — Apache 2.0; числовых лимитов OSS self-hosted на users/requests/datasets не найдено. Некоторые отдельные возможности документация помечает Enterprise-only, например Resume chunking template. |
| **Итог для нашего кейса** | **Подходит с существенными оговорками; кандидат №1 для будущей проверки.** | **Подходит с существенными оговорками; скорее дополнительный orchestration/UI-слой, чем замена RAG-ядра.** | **Подходит с существенными оговорками; кандидат, если parsing сложных документов важнее footprint и reuse Qdrant.** |

## 4. Критичный пункт: деперсонализация ПДн

### 4.1. Нужны три разные гарантии

| Гарантия | Что она означает | Что дают платформы |
|---|---|---|
| **A. ПДн не уходят в Cloud.ru** | Каждый внешний model/embedding/rerank call получает только masked payload. | Достижимо во всех трёх при строго построенном графе; наиболее явный штатный механизм у Dify. Но другой app/flow или неверно подключённая history может обойти правило. |
| **B. ПДн не сохраняются в локальной платформе** | Raw input отсутствует в chat history, application logs, traces, analytics и feedback. | Не гарантируется внутренним pre-LLM node: raw input уже пришёл на ingress платформы. Требуются внешняя деперсонализация до платформы, настройка retention/redaction и проверка каждого telemetry sink. |
| **C. Правило нельзя обойти конфигурацией** | Даже новый flow/plugin/model provider физически не может отправить raw payload наружу. | Глобального, неизбежного и документированного egress middleware ни у одной платформы не подтверждено. Нужны network egress allow-list и единый локальный PII/model gateway. |

### 4.2. Рекомендуемая граница доверия

Если сырой запрос не нужен даже локальному retrieval, наиболее простая безопасная схема выглядит так:

```text
HDE / VK
   │
   ▼
текущий FastAPI ingress
   │  локальная деперсонализация, fail closed
   ▼
Dify / Flowise / RAGFlow
   │  retrieval и workflow работают только с masked query/history
   ▼
локальный OpenAI-compatible PII/model gateway
   │  повторная проверка + audit без raw payload
   ▼
Cloud.ru GigaChat
```

Однако раннее маскирование может ухудшить поиск, если фамилия, номер заявки или другое удаляемое значение нужно для локального retrieval. Если текущую семантику надо сохранить, raw query допустимо обрабатывать **только внутри доверенного локального контура**, а границу поставить непосредственно перед внешними вызовами:

```text
HDE / VK
   │
   ▼
FastAPI + платформа + локальные embeddings/retrieval/rerank
   │  raw разрешён только в доверенной памяти с контролируемыми logs/retention
   ▼
локальный fail-closed PII/model gateway
   │  маскирует query, history, retrieved context и tool outputs
   ▼
Cloud.ru GigaChat
```

Во втором варианте контейнерам платформы запрещается прямой egress к Cloud.ru: все model providers получают только URL локального gateway. Если embeddings или reranker тоже облачные, они обязаны идти через ту же границу. Input Moderation Dify можно использовать как дополнительный ранний слой, но он не заменяет egress enforcement.

### 4.3. Оценка платформенных механизмов

#### Dify

[Moderation API Extension](https://docs.dify.ai/en/self-host/use-dify/workspace/api-extension/moderation-api-extension) документирует событие `app.moderation.input`: внешний локальный endpoint проверяет входные поля и может вернуть действие `overridden` с заменёнными значениями. В normal Chatflow runner заменённые `query/inputs` подставляются перед запуском workflow graph. Это ближе всего к требуемому deterministic hook и лучше, чем tool, который LLM должна сначала решить вызвать.

Ограничения:

- extension настраивается на приложение, а не доказанно перехватывает каждый возможный model call во всём инстансе;
- текущий код сначала создаёт и коммитит raw `Conversation.inputs` и `Message.inputs/query`, а moderation trace получает исходные inputs; то есть это **pre-graph, но не pre-persistence** hook;
- надо контрактно подтвердить, что masked query используется в retrieval, query embedding, reranking, memory и всех model nodes;
- endpoint должен быть локальным, fail closed и не логировать raw payload;
- обработка содержимого загруженных файлов этим extension-контрактом отдельно не подтверждена;
- app без extension, debug/single-node/resume path либо plugin с собственным egress может находиться вне доказанной границы и должен быть запрещён или отдельно проверен.

Источники реализации: [normal app runner с override](https://github.com/langgenius/dify/blob/d94314627f2c0cacb20620ca973f1b9b6e46b4b9/api/core/app/apps/advanced_chat/app_runner.py), [создание и commit raw message/conversation](https://github.com/langgenius/dify/blob/d94314627f2c0cacb20620ca973f1b9b6e46b4b9/api/core/app/apps/message_based_app_generator.py), [moderation trace исходных inputs](https://github.com/langgenius/dify/blob/d94314627f2c0cacb20620ca973f1b9b6e46b4b9/api/core/moderation/input_moderation.py).

**Практический вывод:** Dify может реализовать раннее маскирование перед normal graph лучше конкурентов, но не предотвращает локальное сохранение raw input. Если raw query нужен локальному retrieval, перед LLM следует ставить отдельный HTTP/Code-шаг и обязательный model gateway; текущую внешнюю FastAPI-границу пока убирать нельзя.

#### Flowise

[Agentflow V2](https://docs.flowiseai.com/using-flowise/agentflowv2) даёт `Custom Function`, HTTP node и общий Flow State. `Start` всегда остаётся первым структурным узлом, а PII HTTP/Custom Function можно сделать первым операционным шагом: `Start → local PII HTTP → Retrieval → LLM`. LLM input при этом надо явно привязать к masked output/state и запретить дальнейшим узлам читать исходный `$flow.input`.

Ограничения:

- нет найденного глобального hook для всех chatflows/agentflows и providers;
- raw input может сохраниться как chat message;
- LLM node способен добавлять `prependedChatHistory` независимо от флага `Enable Memory`; нужны `Start.Ephemeral Memory=true` либо гарантированно обезличенная history;
- LLM-based Condition и Agent нельзя ставить до masking: они сами вызывают модель;
- analytics включается для flow целиком, а не ставится «после» masking как узел; внешнюю observability надо отключить либо отдельно доказать redaction всех callback payloads.

**Практический вывод:** подходит для одного контролируемого графа, но security guarantee остаётся нашей внешней ответственностью.

#### RAGFlow

Agent graph предоставляет [HTTP component](https://ragflow.io/docs/http_request_component) и [Code component](https://ragflow.io/docs/code_component). Безопаснее вызвать существующий Python-деперсонализатор через локальный HTTP и передавать его output в Retrieval и Agent вместо `sys.query`. Code component тянет отдельный Sandbox/gVisor и для этой функции не даёт преимущества.

Ограничения:

- глобальный pre-LLM middleware не найден;
- Agent по умолчанию может использовать raw `sys.query` и message window/history;
- chat adapter пишет переданную LLM history в INFO-log: при корректном masked route там будет очищенная история, при обходной ветке возникает одновременно logging- и cloud-риск;
- tool для masking небезопасен: первый LLM call нужен до выбора tool;
- Langfuse tracing может отправить полные prompts, retrieved documents и ответы, поэтому tracing также должен видеть только masked payload;
- любой Rewrite/Agent/LLM-компонент до masking нарушает границу.

**Практический вывод:** технически реализуемо в одном graph, но нужен внешний обязательный PII gateway или сохранение текущего FastAPI ingress.

## 5. Подключение Cloud.ru GigaChat

| Проверка | Dify | Flowise | RAGFlow |
|---|---|---|---|
| Собственный `base_url` | Да, `endpoint_url` в OpenAI-compatible plugin. Для LLM README ожидает URL с `/v1`, а для embeddings/rerank — origin без `/v1`, потому что plugin добавляет версию сам. | Да, `Base Path`/`configuration.baseURL`. | Да, с ожидаемой OpenAI-структурой пути: adapter добавит `/v1`, если в URL нет сегмента `/vN`. |
| API key как Bearer | OpenAI SDK/provider использует API key как Bearer. | ChatOpenAI/OpenAI SDK использует API key как Bearer; можно задать дополнительные headers. | OpenAI/AsyncOpenAI client получает `api_key` и `base_url`. |
| Произвольный model ID | Да. | Да, в том числе ChatOpenAI Custom. | Да. |
| Несколько моделей | Да, отдельные credentials/model nodes. | Да, отдельные model nodes. | Да, выбор модели по компонентам. |
| Автообновление нестандартного краткоживущего токена | Для generic plugin не подтверждено. | Для generic ChatOpenAI node не подтверждено; можно поставить собственный proxy/provider node. | Для generic provider не подтверждено. |
| Embeddings через тот же generic API | Поддерживается соответствующим model type, если endpoint совпадает с ожидаемым контрактом. | OpenAI Embeddings поддерживает custom base URL; возможны Ollama/LocalAI/HF. | Поддерживаются OpenAI-compatible embedding endpoints. |
| Rerank через Chat Completions API | Нет: нужен отдельный совместимый rerank provider/endpoint. | Нет: нужен Cohere/Voyage-compatible или custom HTTP node. | Нет: нужен отдельный `/rerank`-контракт. |

Для Dify это ещё и отдельный plugin artifact. В offline/РФ-контуре его надо заранее зеркалировать или устанавливать локальным `.difypkg`, а не рассчитывать на доступность Marketplace во время запуска.

До любой миграционной оценки нужен небольшой **provider contract suite**, а не только успешный одиночный ответ:

1. обычный Chat Completions request;
2. streaming и отмена запроса;
3. timeout, `429`, `5xx`, malformed response;
4. system/user roles, Unicode и длинный русский контекст;
5. tool calling, если используется Agent;
6. refresh/rotation Bearer-токена без утечки в logs;
7. раздельные chat, embeddings и rerank endpoints;
8. гарантия, что в outbound trace нет исходных ПДн ни в query, ни в history, retrieved context и tool outputs.

## 6. База знаний, русский поиск и воспроизводимость

### Dify

Сильные стороны:

- standard/legacy General и Parent-child, а также расширяемый Knowledge Pipeline и Q&A Processor;
- vector, full-text и hybrid retrieval;
- weights либо reranker, Top K и score threshold;
- Qdrant входит в поддерживаемые vector stores;
- dataset UI и retrieval-настройки интегрированы с приложениями.

Ограничения:

- опубликованная chunking structure фиксируется для БЗ;
- «Qdrant поддерживается» не означает совместимость с текущей collection schema;
- first-class immutable KB release с approval/diff/rollback не найден;
- качество `bge-m3` и `bge-reranker-v2-m3` на русском надо подтвердить на тех же наборах, что используются для текущего ядра;
- для frozen published seed воспроизводимее индексировать через собственный versioned pipeline, а не разрешать неаудируемые ручные правки.

Источники: [standard chunking](https://docs.dify.ai/en/cloud/use-dify/knowledge/create-knowledge/chunking-and-cleaning-text), [Knowledge Pipeline и Q&A Processor](https://github.com/langgenius/dify/discussions/26138), [indexing/retrieval](https://docs.dify.ai/en/cloud/use-dify/knowledge/create-knowledge/setting-indexing-methods), [поддержанные форматы в extractor](https://github.com/langgenius/dify/blob/main/api/core/rag/extractor/extract_processor.py).

### Flowise

Сильные стороны:

- большой набор loaders/splitters и connectors;
- Document Stores дают preview, обработку и upsert;
- Qdrant, PostgreSQL и Redis можно подключить штатно;
- embedding provider практически не навязан;
- HTTP node, собственный component или внешний retrieval API позволяют оставить существующий Python retrieval отдельным сервисом; штатный Custom Retriever лишь меняет формат уже полученного контекста.

Ограничения:

- [текущий Qdrant node](https://github.com/FlowiseAI/Flowise/blob/main/packages/components/nodes/vectorstores/Qdrant/Qdrant.ts) экспонирует dense vector search, Top K и metadata filter, но не sparse vectors и fusion;
- готового узла под текущий `bge-reranker-v2-m3` не найдено;
- RRF объединяет выдачи одного retriever по нескольким LLM-generated вариантам запроса, но это не готовая замена текущему source-aware dense+sparse+rerank pipeline;
- ручное редактирование chunks и отсутствие immutable KB releases требуют внешнего versioned source-of-truth.

Источники: [Document Stores](https://docs.flowiseai.com/using-flowise/document-stores), [document loaders](https://docs.flowiseai.com/integrations/langchain/document-loaders), [text splitters](https://docs.flowiseai.com/integrations/langchain/text-splitters), [embeddings](https://docs.flowiseai.com/integrations/langchain/embeddings), [retrievers](https://docs.flowiseai.com/integrations/langchain/retrievers).

### RAGFlow

Сильные стороны:

- наиболее богатый parsing сложных документов, таблиц, изображений и сканов;
- layout-aware templates, OCR, manual inspection и retrieval test;
- визуальный ingestion pipeline и parent-child chunking;
- нативная комбинация keyword/full-text и dense vector;
- отдельные embedding и rerank models; BGE-модели приведены как закомментированный конфигурационный пример и должны разворачиваться отдельно.

Ограничения:

- Qdrant не входит в поддержанные document engines;
- embedding model на наполненном dataset не заменяется без удаления chunks и переиндексации;
- русский dense retrieval возможен с `bge-m3`, но качество keyword/full-text части на русской морфологии не подтверждено отдельным benchmark;
- immutable dataset release/rollback не найден;
- ручные правки chunks удобны для исследования, но без внешнего release pipeline снижают воспроизводимость.

Источники: [настройка knowledge base](https://ragflow.io/docs/configure_knowledge_base), [retrieval test и hybrid scoring](https://ragflow.io/docs/run_retrieval_test), [parent-child chunks](https://ragflow.io/docs/configure_child_chunking_strategy), [официальная карточка BGE-M3](https://huggingface.co/BAAI/bge-m3).

## 7. Управляемость и эксплуатационный контур

| Возможность | Dify Community/self-hosted | Flowise Community/self-hosted | RAGFlow OSS/self-hosted |
|---|---|---|---|
| Web admin/editor | Да. | Да. | Да. |
| Просмотр диалогов | Да. | Да. | Да. |
| Node-level execution trace | Встроенная run history для workflow. | Встроенная пошаговая трассировка Agentflow V2. | Agent runtime logs; опциональный Langfuse даёт application/pipeline spans retrieval, ranking и generation. |
| Prompt/retrieval trace | Да, через logs/run history; retention и redaction требуют настройки. | Да, часть встроенно, расширенно через внешние observability tools. | Через Langfuse, включая полные prompts/docs/responses. |
| Feedback | Да. | Да, UI/API. | История диалогов есть; сопоставимый с Dify встроенный feedback API в изученных материалах не подтверждён. |
| Tokens/cost analytics | Встроенные application analytics. | Зависит от flow и подключённой observability. | В основном через tracing/собственные метрики. |
| Workflow/Agent versions | История опубликованных версий есть; restore/export/run конкретной старой версии отмечены как paid-plan функции, поэтому Community self-hosted coverage надо подтвердить. | Полноценная встроенная version history в изученной документации не подтверждена; нужен export/Git. | Да, Agent version history/rollback. |
| Immutable KB releases | Не найдено. | Не найдено. | Не найдено. |
| RBAC/workspaces в бесплатной редакции | Community — Single Workspace; multiple workspaces относятся к Enterprise, а multi-tenant сценарий дополнительно ограничен лицензией. | Workspaces/RBAC — Cloud/Enterprise. | Admin/user management есть; точную enterprise-границу отдельных функций надо сверять перед эксплуатацией. |
| Встроенные evaluations | Есть dataset/app observability, но это не замена нашему независимому holdout. | Официальные Evaluations — Cloud/Enterprise. | Retrieval tests есть; независимый end-to-end holdout остаётся внешним. |

Ни один dashboard нельзя считать доказательством качества сам по себе. Для сравнения с текущим ядром нужны одинаковые:

- versioned published seed;
- calibration и независимый holdout;
- правила эскалации и safety cases;
- метрики source coverage/hallucination/justified escalation;
- latency p50/p95 и полная стоимость, включая embedding/rerank/БД;
- обезличенные traces с одинаковой схемой.

## 8. Развёртывание и supply-chain

### 8.1. Состав и требования

| Платформа | Минимальный/официальный ориентир | Основной состав и production-оговорка |
|---|---|---|
| Dify | Docker Compose; минимум 2 CPU и 4 GiB RAM. | Уже в quick start: 7 core + 8 dependent services и 1 init task. В production дополнительно нужны HA/масштабирование, внешние stateful services при необходимости, backups, monitoring и собственный capacity sizing. |
| Flowise | Минимально один `flowiseai/flowise` и SQLite; официальный минимальный hardware threshold не заявлен. | Рекомендация для scalable queue deployment: 2 main и 4 workers от 4 vCPU/8 GB каждый, PostgreSQL, Redis/BullMQ, vector DB. |
| RAGFlow | Минимум 4 CPU, 16 GB RAM и 50 GB disk; Docker 24+/Compose 2.26.1+; x86 images. | MySQL, MinIO, Valkey/Redis, Elasticsearch/Infinity, OCR/parsing, model endpoints; для Code component ещё Sandbox и gVisor. |

Источники: [Dify Docker Compose quick start](https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose), [Flowise production deployment](https://docs.flowiseai.com/configuration/running-in-production), [RAGFlow README](https://github.com/infiniflow/ragflow/blob/main/README.md).

### 8.2. Доступность из РФ

Официальные проекты не гарантируют доступность GitHub, Docker Hub, npm/PyPI/Hugging Face и внешних plugin registries из конкретного российского дата-центра. Поэтому корректный вывод — не «скачается» или «не скачается», а **до clean deployment нужен повторяемый offline/mirrored supply chain**:

1. зафиксировать release tags и image digests, не использовать `latest`/`nightly`;
2. импортировать все app и dependency images во внутренний registry;
3. сохранить SBOM/список образов и проверить signatures/hashes, где доступны;
4. зеркалировать model weights, tokenizer files, plugins и package artifacts;
5. выполнять rebuild только из trusted commit и чистого vendor image;
6. проверять, что runtime не пытается динамически скачать плагины/модели из внешней сети;
7. хранить новые credentials только в новом secret store, не в Compose-файлах.

Сравнительно:

- **Flowise** проще всего зеркалировать в минимальной конфигурации, но production queue и внешние model/vector services сокращают разницу.
- **Dify** требует больше app/dependency/plugin artifacts; OpenAI-compatible provider тоже надо сохранить как локальный `.difypkg` либо зеркалированный artifact.
- **RAGFlow** требует больше всего тяжёлых образов и model artifacts. Его документация предлагает Huawei/Aliyun mirrors для части nightly image и `HF_ENDPOINT`, но это не заменяет собственное полное зеркало.

## 9. Лицензии и бесплатные редакции

### Dify

[Официальная pricing-страница](https://dify.ai/pricing) указывает для бесплатной Community edition все core features публичного репозитория и **Single Workspace**. [Dify Open Source License](https://github.com/langgenius/dify/blob/main/LICENSE) основана на Apache 2.0, но содержит дополнительные условия:

- коммерческое использование в целом разрешено;
- для multi-tenant environment без письменного разрешения нужна commercial license, причём tenant определён как workspace;
- при использовании frontend нельзя удалять или изменять Dify logo/copyright;
- branding-ограничение не применяется к headless-использованию без frontend.

Поэтому Dify нельзя описывать просто как «Apache 2.0 без оговорок» либо как полностью запрещённый для коммерческих проектов. Помимо Single Workspace, числовых runtime-лимитов Community self-hosted на requests/datasets в изученных материалах не обнаружено; реальную workspace/tenant-модель надо юридически проверить до production.

### Flowise

[LICENSE.md](https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md) разделяет Community-код под Apache 2.0 и enterprise-код/явно помеченные файлы под коммерческими условиями. Основной visual flow, API, loaders, vector stores и Document Stores доступны self-hosted. Workspaces/RBAC, SSO и встроенные Evaluations относятся к Cloud/Enterprise. Числовых ограничений на self-hosted Community flows, requests или KB не найдено.

### RAGFlow

Основной репозиторий распространяется по [Apache License 2.0](https://github.com/infiniflow/ragflow/blob/main/LICENSE). Документированных числовых OSS self-hosted лимитов на users, requests или datasets не найдено. При этом отдельные функции могут маркироваться Enterprise-only — например, Resume chunking template, поэтому конкретную edition matrix надо зафиксировать перед PoC.

## 10. Вывод по каждой платформе

### Dify — подходит с существенными оговорками

Сильные стороны: наиболее зрелая комбинация визуальных workflows, нативного hybrid/rerank RAG, Qdrant, произвольного OpenAI-compatible provider, встроенной аналитики и официального Input Moderation API Extension. Для одной БЗ, русского multilingual retrieval и Cloud.ru функциональная база достаточна.

Слабые стороны: состав сервисов заметный, Community ограничена Single Workspace, лицензия имеет дополнительные условия, существующую Qdrant schema нельзя считать совместимой, immutable KB releases не найдено. Raw query сохраняется до moderation extension, поэтому деперсонализацию нельзя переносить только в него без внешнего fail-closed boundary и egress policy.

### Flowise — подходит с существенными оговорками, но не как прямая замена ядра

Сильные стороны: лёгкий минимальный self-hosted вариант, гибкий visual orchestration, custom Base Path/headers, Qdrant и удобные Custom Function/HTTP nodes. Хорошо подходит для быстрого моделирования flows либо как UI/orchestration поверх существующего retrieval API.

Слабые стороны: штатный Qdrant node не реализует текущий dense+sparse hybrid, готового `bge-reranker-v2-m3` node не найдено, нет глобального PII hook и immutable KB releases; RBAC/workspaces/evaluations ограничены коммерческими редакциями. Полная замена текущего production RAG потребует сохранить значительную часть Python-ядра снаружи.

### RAGFlow — подходит с существенными оговорками

Сильные стороны: лучший из трёх document-centric RAG — OCR/layout parsing, управляемые chunks, retrieval testing, native hybrid и reranking; OpenAI-compatible provider и BGE-модели хорошо совпадают с целевым направлением.

Слабые стороны: самый тяжёлый runtime, Qdrant не поддерживается как document engine, требуется новая индексация, глобального PII hook и KB release/versioning не найдено. Выбирать его разумно, только если качество parsing сложных PDF/таблиц на нашем корпусе даст измеримое преимущество, оправдывающее инфраструктурную цену.

## 11. Итоговая сводка

**Наиболее пригодным под текущие требования выглядит Dify**, потому что он единственный из трёх сочетает:

- явный arbitrary OpenAI-compatible `base_url`;
- документированный per-app input-moderation hook с заменой значения перед normal workflow graph;
- нативный hybrid retrieval и reranking;
- Qdrant как поддерживаемый backend;
- наиболее полный встроенный admin/log/analytics-контур.

Но преимущество Dify пока означает только **«лучший кандидат на измеримый PoC»**, а не «готовая миграция». Главные незакрытые разрывы:

1. нет доказанной глобальной и обязательной PII/egress boundary;
2. нет подтверждённого immutable release/rollback всей БЗ;
3. Cloud.ru совместимость проверена только по конфигурационному контракту, не runtime-тестом;
4. текущие source-aware retrieval, deterministic direct answers, verification, escalation и two-model fail-safe придётся воспроизвести и сравнить;
5. поддержка Qdrant не гарантирует reuse существующих collections;
6. лицензия Dify требует отдельной проверки под реальную workspace/tenant-модель.

Если позднее будет отдельно согласован PoC, корректный следующий шаг — не установка «для посмотреть», а одинаковый сравнительный прогон **текущего ядра и Dify** на копии одного versioned published seed и одном обезличенном calibration/holdout-протоколе. RAGFlow имеет смысл добавить как document-parsing challenger, а Flowise — как вариант orchestration поверх существующего retrieval API. До такого эксперимента менять архитектуру или принимать решение о миграции оснований нет.

## 12. Основные официальные источники

### Dify

- [Репозиторий Dify](https://github.com/langgenius/dify)
- [Docker Compose quick start и требования](https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose)
- [OpenAI-API-compatible plugin](https://marketplace.dify.ai/plugin/langgenius/openai_api_compatible)
- [README и URL-контракты OpenAI-API-compatible plugin](https://github.com/langgenius/dify-official-plugins/blob/main/models/openai_api_compatible/README.md)
- [Исходный код OpenAI-API-compatible plugin](https://github.com/langgenius/dify-official-plugins/tree/main/models/openai_api_compatible)
- [Input Moderation API Extension](https://docs.dify.ai/en/self-host/use-dify/workspace/api-extension/moderation-api-extension)
- [Raw message persistence до runner/moderation](https://github.com/langgenius/dify/blob/d94314627f2c0cacb20620ca973f1b9b6e46b4b9/api/core/app/apps/message_based_app_generator.py)
- [Chunking and cleaning](https://docs.dify.ai/en/cloud/use-dify/knowledge/create-knowledge/chunking-and-cleaning-text)
- [Knowledge Pipeline и Q&A Processor](https://github.com/langgenius/dify/discussions/26138)
- [Indexing and retrieval](https://docs.dify.ai/en/cloud/use-dify/knowledge/create-knowledge/setting-indexing-methods)
- [Analytics](https://docs.dify.ai/en/cloud/use-dify/monitor/analysis)
- [Logs](https://docs.dify.ai/en/cloud/use-dify/monitor/logs)
- [Workflow version control](https://docs.dify.ai/en/cloud/use-dify/build/version-control)
- [Community pricing и Single Workspace](https://dify.ai/pricing)
- [Лицензия Dify](https://github.com/langgenius/dify/blob/main/LICENSE)

### Flowise

- [Репозиторий Flowise](https://github.com/FlowiseAI/Flowise)
- [Официальный минимальный Docker Compose](https://github.com/FlowiseAI/Flowise/blob/main/docker/docker-compose.yml)
- [Databases](https://docs.flowiseai.com/configuration/databases)
- [Running in Production](https://docs.flowiseai.com/configuration/running-in-production)
- [ChatOpenAI с custom Base Path](https://docs.flowiseai.com/integrations/langchain/chat-models/azure-chatopenai)
- [Исходный код ChatOpenAI node](https://github.com/FlowiseAI/Flowise/blob/main/packages/components/nodes/chatmodels/ChatOpenAI/ChatOpenAI.ts)
- [Agentflow V2](https://docs.flowiseai.com/using-flowise/agentflowv2)
- [Исходный код Custom Function](https://github.com/FlowiseAI/Flowise/blob/main/packages/components/nodes/agentflow/CustomFunction/CustomFunction.ts)
- [Document Stores](https://docs.flowiseai.com/using-flowise/document-stores)
- [Document Loaders](https://docs.flowiseai.com/integrations/langchain/document-loaders)
- [Text Splitters](https://docs.flowiseai.com/integrations/langchain/text-splitters)
- [Embeddings](https://docs.flowiseai.com/integrations/langchain/embeddings)
- [Retrievers](https://docs.flowiseai.com/integrations/langchain/retrievers)
- [Qdrant node source](https://github.com/FlowiseAI/Flowise/blob/main/packages/components/nodes/vectorstores/Qdrant/Qdrant.ts)
- [Analytics and tracing](https://docs.flowiseai.com/using-flowise/analytics)
- [Workspaces/RBAC edition boundary](https://docs.flowiseai.com/using-flowise/workspaces)
- [Evaluations edition boundary](https://docs.flowiseai.com/using-flowise/evaluations)
- [Лицензия Flowise](https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md)

### RAGFlow

- [Репозиторий и системные требования RAGFlow](https://github.com/infiniflow/ragflow)
- [Docker deployment details](https://github.com/infiniflow/ragflow/blob/main/docker/README.md)
- [OpenAI-compatible и другие модели](https://ragflow.io/docs/supported_models)
- [Реализация OpenAI-compatible chat adapter](https://github.com/infiniflow/ragflow/blob/main/rag/llm/chat_model.py)
- [Model configuration template](https://github.com/infiniflow/ragflow/blob/main/docker/service_conf.yaml.template)
- [Knowledge base configuration](https://ragflow.io/docs/configure_knowledge_base)
- [Retrieval test и hybrid scoring](https://ragflow.io/docs/run_retrieval_test)
- [Parent-child chunking](https://ragflow.io/docs/configure_child_chunking_strategy)
- [Agent components](https://ragflow.io/docs/category/components)
- [HTTP component](https://ragflow.io/docs/http_request_component)
- [Code component](https://ragflow.io/docs/code_component)
- [Admin service](https://ragflow.io/docs/admin_service)
- [Langfuse tracing](https://ragflow.io/docs/tracing)
- [Лицензия RAGFlow](https://github.com/infiniflow/ragflow/blob/main/LICENSE)
