# Действующие решения проекта

Документ фиксирует решения, которые нельзя восстанавливать по памяти чата. Если старый раздел
`docs/architecture.md` противоречит этому файлу и фактическим тестам, применяются решения ниже.

## D-001. Проект сначала заканчивается для Росмолодёжи

**Статус:** принято.  
Сначала доказывается качество VK/HDE-бота Росмолодёжи. Мультитенантный коммерческий продукт и
переиспользование для других компаний рассматриваются только после пилота.

## D-002. Grounded-only ответы

**Статус:** принято.  
Бот не использует знания модели как источник фактов. Ответ должен иметь опубликованные chunks.
Если фактов недостаточно, бот уточняет или контролируемо эскалирует.

## D-003. Приоритет источников

**Статус:** заменено D-034 для ответов нового AI-помощника 28 июля 2026.
Исторически свежий опубликованный Yonote только получал приоритет перед legacy XLSX/DOCX.
После принятия D-034 XLSX/DOCX больше не являются допустимым factual fallback нового помощника.
Они сохраняются как материал миграции и regression baseline, но не подтверждают пользовательский
ответ.

## D-004. Yonote только на чтение

**Статус:** принято.  
Интеграция читает только выбранные коллекции. Production Preview выполняет полный ручной pull,
проверяет наличие каждой коллекции и считает diff в памяти; он не меняет Yonote, tracked
`data/knowledge_base_seed.json`, Qdrant, cache или ответы бота. Production Apply запрещён
read-only UI и backend `403`.

Существующий Apply допустим только в локальном/disposable release-engineering контуре после
явного content review. Публикация в production выполняется как versioned Git-изменение seed с
ручным review, validation/regression, clean candidate build, контролируемой полной индексацией и
rollback evidence. Запись, изменение или удаление документов Yonote запрещены во всех режимах.

## D-005. LangGraph вместо автономной мультиагентности

**Статус:** принято.  
Текущий граф уже разделяет анализ, поиск, синтез и проверку. Второй автономный агент не
добавляется ради презентации: это увеличивает latency и стоимость, но не создаёт новых фактов.
LLM judge допустим как ограниченный verifier, а не как независимый источник истины.

## D-006. Каскад моделей

**Статус:** принято.  
10B используется для простых задач, Max — для сложного синтеза по найденным источникам.
Уверенный single/multi-source ответ может быть сформирован без LLM.

## D-007. Память не ограничивается пятью ходами

**Статус:** принято.  
Последние 20 пар находятся в Redis, полная маскированная история и structured context — в
PostgreSQL, старая часть сжимается в summary. Количество уточнений не является самостоятельной
причиной эскалации. Старое правило `turn_count > 5` из архитектурного документа устарело.

## D-008. Уточнение предпочтительнее угадывания

**Статус:** принято.  
Если пользователь пишет `Где мой билет?`, а событие неизвестно, бот спрашивает название. После
ответа контекст используется до явной смены события или категории.

## D-009. Ругань и off-topic не нагружают операторов

**Статус:** принято.  
Бессодержательная ругань, политика, погода и провокации получают scope-note. Если в сообщении
есть полезный вопрос, бот отвечает по существу. Safety-сценарии остаются немедленной
эскалацией.

## D-010. Метрика — закрытый полный тикет

**Статус:** принято.  
Главная конверсия — полное разрешение диалога без оператора за любое число сообщений.
First-turn auto-answer, clarification rate и multi-turn resolution публикуются отдельно и не
подменяют друг друга.

## D-011. Calibration и holdout разделяются

**Статус:** принято.  
Повторный зелёный прогон вопросов, по которым исправлялся код, является regression evidence,
но не независимым доказательством качества. Операторский тест должен содержать неиспользованный
holdout.

## D-012. HDE не используется для массовых eval

**Статус:** принято.  
Лимит HDE общий для всей системы: 300 RPM, возможен бан на 20 минут. Все большие наборы идут
через server-local `/ask`; через VK/HDE выполняются только короткие ручные smoke.

## D-013. Приватные тикеты не являются частью продукта

**Статус:** принято.  
Сырые выгрузки хранятся в `data/private/`, не коммитятся, не копируются на сервер и не входят в
Docker. В Git попадают только проверенные обезличенные кейсы и агрегаты.

## D-014. Админка должна работать через постоянный HTTPS

**Статус:** принцип принят; прежняя реализация выведена из эксплуатации 15 июля 2026.
Старая публичная админка, её TLS state, admin credentials и SSH tunnel
относятся к скомпрометированной VM и больше не используются. Новый общий HTTPS URL создаётся
только на чистой VM с новым сертификатом и перевыпущенным admin token. Plaintext login/admin API
по-прежнему запрещены; до нового security/release handoff админки нет.

## D-015. Deployment выполняется вручную пользователем

**Статус:** принято.  
Codex делает изменения, тесты, commit/push и предоставляет команды. Пользователь выполняет
clean deployment на новом сервере вручную. Старый `/opt/rosmol-ai-bot` недоверен и не копируется;
push не означает автоматический deployment.

## D-016. Изображения пока не распознаются

**Статус:** принято как ограничение пилота.  
Screenshot-only обращение не должно приводить к выдуманному ответу. До появления OCR/vision оно
контролируемо передаётся специалисту.

## D-017. Изменения качества выполняются пакетно

**Статус:** принято.  
Перед исправлением фиксируются defect, expected behavior, trace и тип ошибки. Нельзя бесконечно
подгонять routing под единичные вопросы или повторно запускать большие наборы без гипотезы.

## D-018. Текущий release candidate

**Статус:** `NO GO / SECURITY HOLD`; прежний `LIMITED GO` отозван 15 июля 2026.
Последний проверенный code release candidate — `8bca860` поверх `98de023`, operator handoff —
`850ad46`; последний pre-incident holdout state — `6acf6fb`.
Targeted follow-up прошёл `16/16` и `4/4` с заполненными eval identifiers; финальный smoke прошёл
`16/16`, полный
server-local suite выполнил все семь секций. После deployment коррекции реальный двухходовый
HDE/VK smoke подтвердил качество, сохранение контекста, разные stable
`message.id={last_post_id}`, одну trace/один ответ на inbound и delivery telemetry со статусом
`delivered`, `attempted=true`, HTTP `200` и непустым `delivered_at`. Ошибки delivery update в логах
отсутствовали; dedupe regression был зелёным. Это историческая quality baseline, а не работающий
runtime: старая VM скомпрометирована и выключена, новый gate ещё не выполнен.

## D-019. Операторский тест измеряет конверсию при замороженном кандидате

**Статус:** прерван P0-инцидентом 15 июля 2026; recovery freeze действует.
На время восстановления не меняются код, routing, prompts, thresholds и KB. Главный результат
считается по полным tickets: direct answer и успешное разрешение после уточнения являются закрытием
без оператора; одно уточнение само по себе закрытием не считается. Новые обращения не исправляются
по одному: часть сохраняется как holdout, остальные после теста разбираются одним пакетным циклом.
Исторические XLSX и regression suite используются как baseline и защита от поломок, но не как
доказательство обобщающей способности текущего RC.

Предварительная нижняя граница прерванного cohort была `2026-07-15 11:54:43+00`. Этот cohort не
используется как финальная conversion estimate и не объединяется с будущим. Новая граница будет
первым реальным HDE trace после clean rebuild и нового handoff. Feedback Наты сохраняется как
предварительный calibration input в `docs/operator_feedback_20260715.md`.

## D-020. В runtime индексируются только published records

**Статус:** принято.
Seed может содержать archived records для воспроизводимости source corrections, но Qdrant
`knowledge_base` получает только `status=published`. Полная индексация требует forum registry и
`--prune-stale`; после любого успешного изменения KB semantic response cache очищается полностью.
Для pre-incident RC `8bca860` последний подтверждённый runtime count был `2152`, а не полный seed
count `2186`. Это историческое значение; новый Qdrant создаётся и проверяется с нуля.

## D-021. HDE delivery работает fail-closed и наблюдаемо

**Статус:** durable-контракт принят 20 июля 2026; runtime сейчас отсутствует.
Webhook принимает только аутентифицированное событие со стабильными provider event/ticket
identity. PII masking работает fail-closed: если Natasha не загрузилась или не прошла startup
probe на удаление имени, webhook возвращает `503` до записи. Ответ `200` отдаётся только после
commit в PostgreSQL inbox из migration `008_hde_durable_transport`; Redis lease и FastAPI
`BackgroundTasks` из delivery path удалены.

Inbox/outbox используют HMAC-псевдонимы event/ticket, pgcrypto envelope для обратимого ticket
reference и текста ответа, lease с recovery и строгий порядок сообщений одного ticket. Новый
outbox создаётся только из уже сохранённого trace response. Подтверждённая HDE-доставка одним SQL
statement атомарно фиксирует `outbox=delivered`, delivery telemetry в `request_traces` и очистку
шифротекста/маскированного payload. Отсутствующий trace или потеря lease блокируют commit.
PostgreSQL constraint принимает только точный versioned masked payload либо точный purged marker;
неизвестный JSON key или неверный тип блокирует запись до worker processing.

Автоматически повторяются только заведомо не начатые отправки и `429`; timeout, иной attempted
error и истёкший outbox lease попадают в dead letter для ручной сверки с HDE. Provider не даёт
подтверждённого idempotency key, поэтому crash после принятия ответа HDE, но до локального commit,
остаётся неустранимой ambiguous boundary: оператор сначала сверяет posts в ticket и только затем
явно reconciles как delivered либо requeues. Неверный requeue без сверки может создать дубль.
Recovery доступен только через server-local CLI без public/admin endpoint: обязательны operator,
фиксированный reason, SHA-256 private evidence и повтор job id; изменение queue и append-only
`hde_transport_audit` атомарны. Audit сохраняет диагностику dead-letter до её очистки и защищён
DB trigger от `UPDATE`/`DELETE`; routine retention его не затрагивает. Terminal delivered/processed
queue rows удаляются только по отдельно одобренному TTL и в FK-safe порядке. Retention не удаляет
trace незавершённого HDE job. Readiness не
допускает handoff при dead letter, stale lease или просроченной очереди.

## D-022. Pseudonymization использует отдельный секрет

**Статус:** принято.
В staging/production `USER_HASH_SECRET` обязателен и не может подменяться API, webhook или admin
token. В обычном deployment он не ротируется, потому что ротация разрывает pseudonym ID, session
continuity и ticket-level аналитику. Root-компрометация — обязательное исключение: старое значение
считается раскрытым и перевыпускается отдельной задачей; потеря continuity принимается, будущий
cohort начинается заново.

## D-023. После компрометации допускается только clean rebuild

**Статус:** принято 16 июля 2026.
Старая VM остаётся выключенной и не используется как источник рабочего окружения. В новый runtime
не переносятся старые OS images/disks, Docker images/volumes/cache, `.env`, certificates, SSH
keys, Redis, PostgreSQL/Qdrant backup, runtime-файлы или binaries. Новый сервер строится из
чистого vendor image, проверенного Git commit, новых секретов и заново полученных доверенных
источников; Qdrant индексируется с нуля. Созданные на старом хосте dump/snapshot можно хранить
только изолированно как недоверенные evidence, но не загружать в новый runtime.

## D-024. Feedback Наты — backlog, а не опубликованный факт

**Статус:** принято 16 июля 2026.
Три первых замечания (`Начать`, последовательность `Даты`, длинный ответ про «Машук»)
зафиксированы в `docs/operator_feedback_20260715.md` как воспроизводимые defects и будущие
regression-кейсы. Формулировки оператора про сроки, контакты и Положение требуют verdict владельца
контента; они не индексируются автоматически. Исправления начинаются только после clean rebuild и
нового release handoff.

## D-025. Ротация секретов ведётся по provider-side evidence, без значений в Git

**Статус:** принято 16 июля 2026.
Любой credential, который мог находиться на скомпрометированной VM, считается раскрытым. Сначала
старый credential отзывается у провайдера, затем новый создаётся непосредственно перед установкой
на clean host. Completion требует `old_revoked`, `new_created`, `installed_on_clean_host` и
`verified`; один выпуск нового ключа не считается ротацией. В Git фиксируются только UTC, label,
scope, provider status, безопасный test result и private evidence reference. Значения, suffix,
password/DSN, private keys, recovery codes, cookies, `.env` и auth headers запрещены.

Полный реестр ведётся в `docs/secret_rotation_20260716.md`. Redis legacy не имел password, Qdrant
не имел API key; для них допустим только честный статус `legacy_not_configured` до отдельного
security patch либо подтверждённой изоляции новых пустых instances.

## D-026. Provider-bearing runtime HTTPS/TCP egress проходит только через проверяемый proxy

**Статус:** принято для ограниченного recovery test-production 20 июля 2026.
`app-ml` не подключается к внешней Docker network. Единственный bridge между его internal
`runtime_egress` и внешней `egress` — закреплённый digest Canonical Squid без provider secrets и
без host port. Generated config всегда разрешает только `CONNECT:443` к точному Cloud.ru endpoint
и точному HDE tenant `rosmolodezh.helpdeskeddy.com`. При отдельно включённом ручном Yonote Preview
добавляется ровно один точный destination `rossmol.yonote.ru`; при выключенном Preview его в
allowlist нет. Неизвестный destination и plaintext HTTP запрещены. Cross-provider URL substitution
блокируется до запуска. Новый read-only Yonote token получает только `app-ml`; Squid остаётся
secretless, а `app` — без provider credentials и без egress.

Networked model prefetch выполняется только в secretless bootstrap до создания production env,
после hash/revision verification ML load проверяется offline. В production overlay
`model-prefetch` имеет `network_mode: none`, поэтому missing artifact приводит к fail-closed.
Proxy image входит в SBOM/Critical-CVE/image-secret gate; acceptance включает config parse,
allow/deny CONNECT, невозможность direct public/metadata TCP и фактическую проверку memberships.

Squid не контролирует host-mediated Docker DNS. Для узкого test-production это явный residual с
обязательными provider flow/DNS logs и stop-criteria. До широкого production traffic требуется
отдельная reviewed DNS deny/allow policy; называть текущий контроль полным egress allowlist нельзя.

## D-027. Публичный ingress отделён secretless L4 relay от TLS/webhook runtime

**Статус:** принято для ограниченного recovery test-production 20 июля 2026.
Docker network с `internal: true` не может одновременно надёжно обслуживать опубликованные host
ports, а отключение masquerade не блокирует исходящий трафик контейнера. Поэтому публичные
`80/443` принадлежат только закреплённому digest HAProxy `edge-relay`: он работает в TCP mode,
не завершает TLS, не видит HTTP payload, не получает provider credentials, certificates или
production env и соединяет внешнюю `ingress` с internal `edge`.

Nginx подключён только к `edge`, не публикует host ports и не имеет внешнего маршрута. HTTP `80`
на Nginx допускает только ACME challenge, `/health` и контролируемый `426`; TLS ciphertext на
`443` relay передаёт без расшифровки. Acceptance обязана проверить фактические memberships,
владение ports, прохождение host -> relay -> Nginx и невозможность прямого egress из Nginx.

У relay остаётся внешний маршрут, необходимый для приёма публичных соединений. Это отдельный
residual: в контейнере нет секретов и прикладного кода, image входит в Critical-CVE/secret/SBOM
gate, а любой необъяснимый relay-initiated egress в provider flow logs является stop-criterion.

## D-028. Production ML runtime не публикует host port

**Статус:** принято для recovery test-production 22 июля 2026.
На Docker Engine 29 желаемый loopback bind `app-ml` при подключении сервиса только к internal
networks сохранился в `HostConfig.PortBindings`, но фактические `NetworkSettings.Ports`, host
listener и NAT rule не появились. Это fail-closed с точки зрения внешней доступности, но делает
`127.0.0.1:8001` ложным operational contract.

Отдельный host-facing HTTP relay не принимается: runtime proof показал direct public и metadata
egress из такого контейнера даже при попытке задать приоритет internal gateway. Поскольку relay
между оператором и `app-ml` видел бы plaintext API tokens, prompts и ответы, расширять ему egress
или доверять неработающему route priority запрещено.

В production overlay `app-ml` теперь имеет пустой effective `ports`; `8001` остаётся только local/dev
convention. До TLS readiness и Qdrant baseline проверяются внутри контейнера или по internal Docker
маршруту без передачи секретов. После TLS public runtime/security gate обращается к точному
`https://ADMIN_PUBLIC_HOST`, то есть проходит тот же Nginx policy и сертификат, что будущий HDE
webhook. Полный quality suite выполняется server-local one-shot по internal `data` и получает только
API auth/trace DSN; provider и HDE credentials ему не передаются. Единственный host-publishing
service — secretless L4 `edge-relay` на `80/443`: на `443` он передаёт TLS ciphertext, а на `80`
разрешены только несекретные ACME/health/`426` bootstrap-запросы. Relay не получает production
env/certificates. CI проверяет effective merged Compose, точный список published ports и internal
membership `app-ml`, а не только исходный YAML.

## D-029. Тестовый редактор KB включается отдельной capability и не меняет tracked seed

**Статус:** принято 23 июля 2026 для ограниченного test-production; по умолчанию выключено.
Обычный production сохраняет `ADMIN_READ_ONLY=true`. Для ручной проверки полного цикла
Save -> Qdrant -> RAG владелец может явно включить только в ML runtime согласованную пару
`ADMIN_READ_ONLY=false` и `ADMIN_MUTATIONS_ENABLED=true`. Любая неполная комбинация, API role или
другой путь seed блокируют startup/validation.

Writable runtime использует только
`/app/data/private/admin-kb/knowledge_base_seed.json`: это server-only working copy, не tracked
Git seed. `app-ml` монтирует каталог writable, `app` — только read-only; raw PostgreSQL/Qdrant
console наружу не публикуется. Запись seed атомарна, сохраняет private mode `0600`, а параллельные
admin mutations отклоняются. Admin auth, TLS-only routing, login rate limit, no-store headers и
отсутствие host port у `app-ml` остаются обязательными.

Yonote остаётся source-side read-only: клиент не создаёт, не изменяет и не удаляет документы.
Preview не меняет рабочую KB; Apply в test-editor mode меняет только server-local working copy.
Полный `--prune-stale` reindex намеренно не запускается публичным HTTP endpoint. После review он
выполняется отдельным server-controlled шагом с backup/hash, HDE off, cache clear, restart,
readiness/security/RAG smoke и доказуемым rollback. Рабочая копия тестового контура не становится
production source of truth автоматически; широкий publish требует reviewed versioned seed release.

## D-030. Анализатор, ChatMe и новый AI-помощник — разные контуры

**Статус:** принято 28 июля 2026.
Бот-анализатор, действующий ChatMe-бот и новый AI-помощник имеют разные назначения, evidence и
метрики. Этот репозиторий реализует только новый grounded AI-помощник; внешние показатели
анализатора и ChatMe не являются результатами его тестов.

Переход выполняется не по дате, а после сравнения на одной заранее определённой выборке реальных
содержательных тикетов: ticket-level conversion не ниже согласованного baseline, ноль
неподтверждённых фактов в проверенной выборке, устойчивая delivery и предсказуемая justified
escalation при отсутствии данных. До этого ChatMe остаётся рабочим контуром. Трафик переключается
поэтапно со stop-criteria и проверяемым возвратом; старая скомпрометированная VM никогда не является
rollback target.

## D-031. Knowledge loop управляется человеком и versioned release gate

**Статус:** принято 28 июля 2026; уточняет D-003, D-004, D-024 и D-029.
Trace, операторские исправления и данные анализатора используются для gap analysis и измерения
эффекта, но не индексируются автоматически как факты. Новое знание проходит content verdict
человека, публикацию в Yonote, полный diff, versioned seed change, validation/regression,
контролируемый reindex и проверяемый rollback.

Yonote является целевым human-controlled content authority, а production отвечает по проверенному
release snapshot. Live Yonote остаётся read-only и не меняет production KB напрямую. Будущий
«managed update в одно действие» может только оркестрировать все обязательные gate; он не означает
автономную запись в Yonote, прямой production Apply или обход review.

## D-032. Build-vs-buy остаётся измеряемым ADR, а не решением о миграции

**Статус:** принято 28 июля 2026.
Текущее LangGraph-ядро остаётся действующей реализацией. Возможные self-hosted
Dify/Flowise/RAGFlow сравниваются с ним на одной KB и одной обезличенной выборке по качеству,
управляемости retrieval, PII-before-egress, HDE/VK integration, trace/eval/holdout, data residency,
rollback и полной стоимости сопровождения.

Переход на платформу запрещён без отдельного ADR, regression и release gate. Доменные компоненты
PII masking, HDE/VK adapters и model cascade сохраняются переносимыми независимо от ядра.

## D-033. Исходящие коммуникации не входят в release scope AI-помощника

**Статус:** принято 28 июля 2026.
MAX-рассылки и другие исходящие кампании являются отдельной инициативой. До реализации нужны
проверенные provider rules и отдельный privacy/legal design: явное согласие и отзыв, сегментация,
retention, opt-out и audit trail. Оценки тарифов, лимитов и правовой модели из публичной дорожной
карты не считаются технически подтверждёнными и не разрешают production-запуск.

## D-034. Legacy NLU задаёт copy-контракт, а факты подтверждает только Yonote snapshot

**Статус:** принято 28 июля 2026; заменяет factual fallback D-003 и уточняет D-002, D-006,
D-008, D-009 и D-031.

Старый NLU используется как согласованный контракт сервисных формулировок и структуры ответа.
Точные реплики, допустимые варианты, шаблоны уточнений, эмодзи и 13 внутренних
`response_profile` версионированы в `data/response_contract_v1.json`. Допустима только
механическая нормализация старого текста: удаление HTML/артефактов, исправление пробелов и
обращение на `ты`. Старые даты, условия, ссылки и другая factual copy не переносятся.

Фактический ответ нового помощника может подтверждаться только опубликованным
`source_type=yonote` из проверенного versioned release snapshot. Live Yonote не является
runtime dependency. XLSX, DOCX, operator answer bank и источник без provenance отбрасываются во
всех retrieval/rerank/generation/verification ветках. Если Yonote-покрытия нет, бот задаёт одно
конкретное уточнение или выполняет контролируемую эскалацию.

Динамический ответ строится в порядке `прямой ответ -> необходимые детали -> одна ссылка или
следующий шаг`. Лимит простого ответа — 450 символов, составного — 900. Механическое обрезание
запрещено. Короткий релевантный Yonote-фрагмент может быть возвращён без LLM; длинный,
многотемный или multi-source материал проходит grounded synthesis. Ошибка или нарушение
контракта LLM не разрешает выдавать полный исходный chunk. Эмодзи допустимы только в
версионированных сервисных репликах.

Текущие safety, profanity-routing и controlled escalation имеют приоритет над legacy NLU.
Техническая причина эскалации остаётся в trace; пользователь получает только согласованную
реплику перевода на оператора. Внешние API и HDE/VK payload по-прежнему передают только
`response_text`.

## D-035. Релевантность аспекта предшествует оптимизации фактической свежести

**Статус:** принято 28 июля 2026; уточняет D-010, D-011, D-013 и D-034.

Системный product defect определяется не только недостоверным фактом, но и ответом на соседний
аспект. Вопрос о датах не может закрываться сведениями о трансфере, регистрации, кураторах,
чатах или проживании даже тогда, когда такой chunk имеет высокий retrieval/rerank score.

Запрос пользователя является первичным evidence для `response_profile`; вторичный LLM-анализ
не может заменить явно выраженный аспект. Retrieval и rerank обязаны учитывать пересечение
`entity + requested_aspect`. Composer отвечает только по запрошенным аспектам, а verifier
отклоняет off-aspect результат и разрешает одно повторное формирование, после чего выполняется
уточнение или контролируемая эскалация.

Весь `data/private`, включая `RAG_Dataset.xlsx`, разрешён как локальный product/eval corpus для
частотной карты, calibration, validation и подготовки sealed holdout. Это не меняет D-013:
сырые тикеты не входят в runtime, Docker или Git, а ответы операторов не становятся factual KB.
Continuation-строки сначала объединяются в целые тикеты; дубликаты и шаблонные семьи не делятся
между split. Любая автоматическая разметка остаётся `weak_unreviewed` до человеческого verdict.

Заявление о 50–60% closure допускается только по вручную проверенному sealed holdout минимум из
400 полных тикетов. Дополнительный gate: off-aspect answer rate не выше 1%, для критических пар
— 0; aspect precision не ниже 98%, aspect recall не ниже 90%, critical unsupported facts — 0.

## D-036. Платные eval запускаются только с ограниченным бюджетом и одноразовым согласованием

**Статус:** принято 3 августа 2026; дополняет D-010, D-011 и D-035.

Цикл качества следует принципу offline first: до обращения к платной модели должны пройти
локальные unit/regression-тесты, статические проверки и доступные mock/replay-проверки. Обычный
targeted live eval ограничивается максимум 10 кейсами и расчётным budget 100 рублей на запуск;
case cap действует независимо от корректности внутренней оценки стоимости.
Суммарный бюджет обычных live eval за скользящие 24 часа не может превышать 300 рублей.

До первого `/ask` каждый live eval атомарно резервируется в едином постоянном
`eval-cost-ledger-v1`. Ledger машинно контролирует одноразовое использование approval ID,
сумму routine reservations не более 300 рублей за скользящие 24 часа и не более одного полного
eval за скользящие 24 часа и на release candidate. Отсутствующий, повреждённый или недоступный
для атомарной записи ledger блокирует запуск fail-closed. Резервируется заявленный расчётный
budget: этот механизм не получает и не подтверждает счёт провайдера.

Любой запуск с лимитом выше 100 рублей, а также полный Product80 независимо от прогнозируемой
стоимости, требует отдельного одноразового согласования владельца. Согласование фиксирует точный
runtime SHA, набор и его версию, прогноз стоимости и расчётный верхний budget; переносить его на
другой runtime, набор или повторный запуск нельзя. Полный eval допускается не более одного раза
на один release candidate и не более одного раза за скользящие 24 часа. Начатый, прерванный или
неуспешный полный запуск расходует это разрешение; автоматический повтор запрещён.

Live eval без заданного бюджета или в unbounded-режиме запрещён. `llm_estimated_cost_rub` является
оценкой runner, а не данными биллинга провайдера. Заранее обнаруженная неполная конфигурация
тарифов блокирует запуск до `/ask`; если дефект виден только в trace, runner останавливается сразу
после первого неоценённого кейса и не отправляет следующий. Такой запуск считается расходом и
неуспешным evidence. Если budget достигнут, когда остаются кейсы, следующий `/ask` не отправляется,
а прогон считается незавершённым. Это расчётный stop-limit, а не provider hard cap: один уже
начатый запрос может превысить остаток, поэтому case cap и ручная сверка биллинга обязательны.

После каждого live eval владелец бюджета вручную сверяет `llm_estimated_cost_rub` с фактическим
provider billing за точное окно и сохраняет evidence: `eval_run_id`, runtime SHA, набор/версию,
UTC-окно, approval ID при его наличии, оценку runner, фактическую сумму, расхождение и owner
verdict. Автоматическая provider-billing интеграция не заявлена. Допуск расхождения — 10% по
модулю; превышение означает `STOP` для следующих платных eval до исправления pricing/атрибуции и
нового согласования владельца. Неполученный или ещё не сформированный счёт не считается успешной
сверкой.
