# Human Gold и поэтапное измерение качества

Статус: действует с 4 августа 2026 года. Этот процесс работает локально и не меняет runtime,
Yonote, versioned KB seed, Qdrant, HDE/VK или production-конфигурацию.

## Зачем нужен отдельный GoldTicket

Сырые тикеты и ответы операторов показывают реальный трафик, язык и поведение, но не являются
готовым эталоном. Ответ оператора разрешено использовать как evidence сценария и стиля; любой
факт должен подтверждаться опубликованным Yonote-чанком из зафиксированного KB snapshot.

`GoldTicket v1` измеряет полный тикет, а не отдельную строку. Для каждого проверяемого шага он
фиксирует:

- подтверждённые роли и историю диалога;
- ожидаемое действие `answer / clarify / escalate / scope_note`;
- сущности, аспекты и ограничения возраста, смены, роли, региона, статуса и времени;
- answerability `full / partial / none`;
- qrels с оценкой `0..3`, где `3` требует точного source span;
- обязательные claims, polarity, modality и critical-флаг;
- независимую human-review и privacy provenance;
- точный KB snapshot и канонический SHA записи.

Required claim без grade-3 Yonote support, критичный claim без второго reviewer, неизвестное поле
или изменение после sealing закрываются fail-closed.

## Gold150 sanity v2

Первый набор — calibration sanity, а не независимый holdout и не доказательство конверсии:

- 150 уникальных full-ticket duplicate components;
- 100 traffic-кейсов по распределению `response profile × route`;
- 50 risk-кейсов: multi-turn, time-sensitive, critical profile, operator route и role-review;
- автоматические labels используются только как подсказки для sampling;
- операторские ответы не используются как factual truth;
- все ticket-level результаты остаются в `data/private/`.

Детерминированная сборка:

```powershell
.venv\Scripts\python.exe scripts\build_gold_ticket_dataset.py `
  --cases data\private\tickets\product_baseline_20260729_roles_v1\product_calibration_cases.jsonl `
  --conversations data\private\tickets\product_baseline_20260729_roles_v1\product_calibration_conversations.json `
  --normalized-tickets data\private\tickets\product_baseline_20260729_roles_v1\tickets_normalized.jsonl `
  --artifact-manifest data\private\tickets\product_baseline_20260729_roles_v1\artifact_manifest.json `
  --config eval\datasets\gold150_sanity_v2.json `
  --kb-seed data\knowledge_base_seed.json `
  --output-dir data\private\eval\gold150_sanity_v2
```

Команда не вызывает модели и выводит только агрегаты. Каталог новой версии публикуется через
отдельный staging-каталог и атомарный rename. Перезапись существующей версии запрещена даже с
`--overwrite`: для исправления нужен новый immutable version. Selection manifest не содержит
текстов; review queue содержит только private best-effort deidentified turns и поэтому всё равно
считается `pii_possible` до human privacy verdict.

## Private Dataset Registry

Локальный registry хранится в `data/private/_registry/datasets.json`, игнорируется Git и Docker.
Он проверяет version, privacy/export class, lineage, hashes, review state, hold и retention.

```powershell
.venv\Scripts\python.exe scripts\manage_private_datasets.py inventory
.venv\Scripts\python.exe scripts\manage_private_datasets.py register `
  --entry data\private\eval\gold150_sanity_v2\gold150_sanity_registry.json
.venv\Scripts\python.exe scripts\manage_private_datasets.py validate
.venv\Scripts\python.exe scripts\manage_private_datasets.py review gold150_sanity@v2
.venv\Scripts\python.exe scripts\manage_private_datasets.py complete-review `
  gold150_sanity@v2 `
  --gold-artifact data\private\eval\gold150_sanity_v2\gold150_sanity_gold.jsonl
.venv\Scripts\python.exe scripts\manage_private_datasets.py freeze gold150_sanity@v2
```

Текущий `gold150_sanity@v2` зарегистрирован как `draft`, `human_review_status=pending` и
`independent_evaluation=false`. Freeze до завершённой человеческой проверки обязан завершаться
ошибкой. `complete-review` не принимает самозаявленный hash: он читает sealed GoldTicket JSONL из
корня версии и сверяет schema, membership с selection, количество тикетов, IDs, duplicate
components и provenance hashes. `freeze` повторяет artifact-проверку и блокирует links,
hardlinks, tampering и неподдерживаемые виды датасетов. Raw-набор не может объявить себя safe
aggregate; case-insensitive и вложенные roots запрещены. `retention-plan` только показывает
кандидатов и блокеры; CLI ничего не удаляет.

Предыдущий `gold150_sanity_v1@v1` остаётся историческим pending draft: он был собран до
canonical KB-hash binding и новым verifier не финализируется. Его нельзя мигрировать или
перезаписывать на месте; human review ведётся только по v2.

## Human-review contract

1. Primary reviewer проверяет роли, privacy, действия, аспекты, constraints, qrels и claims для
   всех 150 тикетов до просмотра ответа нового runtime.
2. Все critical claims получает отдельный secondary reviewer; дополнительно вторично проверяется
   детерминированная выборка не менее 25% остальных кейсов.
3. Любое расхождение проходит adjudication. Нельзя молча принять weak label или операторский
   ответ как факт.
4. Для `answer` каждый required claim связывается с точным published Yonote source span. Для
   `clarify`, `escalate` и `scope_note` нельзя придумывать factual expectation.
5. Final GoldTicket JSONL валидируется через `GoldTicketContentV1`, sealed через
   `seal_gold_ticket()` и хранится только в private dataset version. Текущая pending review queue
   не является GoldTicket и не запускается через `/ask`.
6. В существующий ask contract можно проецировать только однозначный single-step calibration или
   validation case. Multi-turn и graded alternatives fail-closed блокируются, потому что legacy
   runner не сохраняет порядок полного тикета и ошибочно трактует список источников как `all-of`.
   Их canonical оценка выполняется через GoldTicket stage funnel; holdout остаётся только в
   существующем sealed one-shot контуре.

## Stage-separated pipeline lineage

Новые trace events не содержат query/response/chunk text. Они сохраняют только request-local
`q1..qN`, безопасные filter scope, упорядоченные chunk IDs, числовые scores и контролируемые
decision/reason:

`question -> retrieved -> reranked -> globally selected -> cited -> verified`.

Legacy `observed_chunk_ids` остаётся совместимым union-полем, но всегда помечается
`legacy_coarse`; по нему запрещено заявлять точную стадию потери. Новый ask report отдельно
выводит ordered arrays, per-question lineage, источник decision confidence и наличие evidence
для каждой стадии. Глобальный список выбранных источников точный; пересечение источника с
кандидатами отдельного `qN` остаётся `candidate_overlap_coarse_unattributed`, пока генератор не
возвращает явную claim-to-question binding. Filter values не попадают в trace в исходном виде, а
telemetry имеет жёсткие caps и явные truncation counters. Exact/partial attribution разрешена
только для полной versioned schema; иначе отчёт остаётся `legacy_coarse` или `unscored`.

## Offline stage funnel

После human Gold и локального observation report:

```powershell
.venv\Scripts\python.exe -m eval.stage_funnel `
  --tickets data\private\eval\gold150_sanity_v2\gold150_sanity_gold.jsonl `
  --observations data\private\eval\gold150_sanity_v2\ask-observations.json `
  --output data\private\eval\gold150_sanity_v2\stage-funnel-safe.json
```

Scorer не вызывает модель и формирует безопасный отчёт без текстов, ticket/chunk/claim IDs. Он
считает action confusion/macro-F1, Recall@1/3/5/10, MRR@10, graded NDCG@10, survival между
стадиями, selection/citation recall, completeness required claims и первый точный loss stage.

Отсутствующее evidence означает `unscored`, а не `0`. Legacy union получает только coarse
attribution. `answer` без human qrels считается `label_or_content_gap`, а не ошибкой retrieval;
корректный `clarify` с partial answerability и отсутствующим измерением не требует выдуманных
qrels.

## Правило следующего исправления

Платный прогон разрешён только после offline-отчёта с одной конкретной гипотезой и ожидаемым
изменением метрики. Сначала исправляется крупнейший системный loss stage и добавляются unit/
regression-тесты. Затем проходят Ruff, полный pytest и KB validate. Только после этого допустим
targeted live eval максимум на 10 кейсов и 100 рублей с D-036 ledger и ручной billing-сверкой.

Полный Product80 не является следующим шагом: он уже exposed calibration, дорог и не даёт
human-gold stage attribution. Сервер, Yonote и Qdrant не меняются до отдельного release decision.
