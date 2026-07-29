# Программа продуктового качества AI-бота

Дата фиксации: 28 июля 2026 года.

## Цель

Главный результат — доля реальных тикетов, полностью закрытых ботом без оператора за любое
число сообщений. Целевой диапазон первого доказанного релиза — 50–60%.

Успешный ответ обязан:

1. понять сущность и запрошенный аспект;
2. ответить именно на этот аспект;
3. не добавлять соседние темы без запроса;
4. опираться на разрешённое подтверждение фактов;
5. уточнить или эскалировать запрос, если подтверждения недостаточно.

Актуальность конкретной даты — отдельная задача управления контентом. Ошибка аспекта
(`даты` → `трансфер`) является дефектом продукта и блокирует релиз независимо от качества
контентной базы.

## Реальный корпус

`data/private/tickets/RAG_Dataset.xlsx` содержит:

- 25 508 строк экспорта;
- 24 220 уникальных тикетов;
- 1 288 continuation-строк, которые должны присоединяться к исходному тикету;
- обращения за период с 6 июня 2025 года по 10 июня 2026 года.

Весь `data/private` используется только локально как продуктовый и оценочный корпус.
Операторские ответы помогают понять сценарий, ожидаемое действие и стиль, но не являются
автоматически подтверждёнными фактами и не индексируются как factual KB.
Маскирование query-кандидатов является best-effort деперсонализацией, а не анонимизацией:
все исходные и производные ticket-level артефакты остаются только в `data/private`.

Старые автоматически собранные golden/eval-наборы не считаются независимым доказательством:
в них обнаружены continuation leakage, служебные сообщения вместо пользовательских вопросов
и слабые operator-derived labels.

После первичной очистки 11 453 тикета содержат пригодный query-кандидат и aspect label.
Оставшиеся 12 767 помечены `unresolved`, а не додумываются из ответа оператора. Среди
пригодных кандидатов наиболее часты:

| Response profile | Тикетов |
|---|---:|
| `generic` | 4 668 |
| `grants` | 1 388 |
| `selection_status` | 1 282 |
| `application` | 1 109 |
| `technical` | 883 |
| `documents` | 588 |
| `travel` | 468 |
| `food` | 317 |
| `dates` | 301 |
| `program` | 188 |

Это рабочая частотная карта со статусом `weak_unreviewed`, а не измерение качества бота.
Первый correction cycle закрывает верхние сочетания сущности и этих аспектов; следующий цикл
восстанавливает роли и multi-turn контекст в `unresolved`.

## Целевой продуктовый контур

### 1. Intent и aspect contract

Анализатор формирует:

- сущность (`forum`, мероприятие, грантовый этап, платформа);
- один или несколько `requested_aspects`;
- необходимость уточнения;
- допустимый route: `answer`, `clarify`, `escalate`;
- признаки персонального статуса, технической проблемы и safety-case.

Явный аспект запроса имеет приоритет над общим вопросительным словом. Например:

- `Когда будут результаты отбора?` → `selection_status`;
- `До какого числа подать заявку?` → `application`;
- `Когда будет трансфер?` → `travel`;
- `Когда проходит Машук?` → `dates`.

### 2. Retrieval и rerank

Кандидат должен совпадать по сущности и аспекту. Высокий similarity-score соседнего чанка
не даёт права отвечать им на другой вопрос.

Обязательные диагностические метрики:

- entity+aspect match@1;
- evidence coverage@3;
- wrong-aspect@1;
- отсутствие запрещённого provenance.

### 3. Composer и verifier

Структура ответа:

`прямой ответ → необходимые детали → одна ссылка или следующий шаг`.

Composer не расширяет тему самостоятельно. Verifier сопоставляет аспекты вопроса, источников
и ответа. Off-aspect ответ повторно формируется один раз, затем запрос уточняется или
контролируемо эскалируется.

## Оценочный контур

Текущая автоматическая review-очередь содержит один query-кандидат, связанный с целым
объединённым тикетом. Это не full-ticket holdout и не измеритель multi-turn closure.
Нормализованные одинаковые запросы и лексические шаблонные семьи не делятся между split;
семантические перефразировки дополнительно проверяются человеком.

Локальный конвейер формирует:

- `calibration` — разработка и разбор ошибок;
- `validation` — выбор реализации и порогов;
- `holdout` — хронологически поздние кандидаты.

Автоматические labels имеют статус `weak_unreviewed`. Сейчас получено 8 438 calibration,
1 445 validation и 1 570 holdout-кандидатов; разбиение использует самое позднее из времени
создания, обновления и закрытия тикета. Sealed holdout появляется только после восстановления
ролей, ручной проверки и замораживания минимум 400 полных тикетов. Ответ оператора в product
eval payload не передаётся. Time-sensitive кейсы получают factual verdict только при наличии
соответствующего approved release snapshot.

## Release gate для заявления о 50–60%

- human-verified ticket closure не ниже 50%;
- отдельно опубликованы first-turn и multi-turn closure;
- off-aspect answer rate не выше 1%;
- критические пары (`dates`/`travel`, `application`/`documents` и подобные) — 0 ошибок;
- aspect precision не ниже 98%;
- aspect recall не ниже 90%;
- критические неподтверждённые факты — 0;
- justified escalation не ниже 95%;
- unresolved clarification не выше 5%;
- не менее 400 независимых тикетов в sealed holdout;
- результат сопровождается доверительным интервалом и разрезом по частотным классам.

## Порядок работы

1. Очистить извлечение пользовательских реплик и построить честную частотную карту.
2. Вручную проверить top-20 сочетаний `intent × aspect × entity class`.
3. Получить baseline полного локального `/ask` replay без HDE.
4. Исправлять классы по убыванию доли трафика, не отдельные красивые примеры.
5. После каждого цикла запускать calibration и validation; holdout не открывать.
6. Один раз пройти sealed holdout, затем подтвердить результат на новой operator cohort.

До clean security acceptance никакие подготовленные behavior-изменения не разворачиваются
на сервер и не отправляются в HDE.

## Результат восстановления ролей и multi-turn, 29 июля 2026 года

Исходный XLSX не содержит отдельной колонки `speaker`. Поэтому роли нельзя восстановить
абсолютно точно: автоматический контур принимает только реплики `user/high`, а всё
неоднозначное отправляет в закрытую ручную очередь. Заголовок тикета больше не считается
репликой пользователя и не может заменить реальный вопрос. Вопросы и обещания оператора
вроде «Вам удалось войти?» или «Я уточню информацию» не попадают в автоматический eval.

После консервативной пересборки 24 220 тикетов:

- `complete`: 1 440 тикетов;
- `partial`: 5 215 тикетов;
- `unresolved`: 17 565 тикетов;
- подтверждённый `user/high` найден в 6 655 тикетах;
- 1 346 тикетов содержат больше одной подтверждённой пользовательской реплики;
- conversation-корпус содержит 8 447 пользовательских turns.

`unresolved` здесь не означает проблему базы знаний. Это означает, что без speaker-разметки
нельзя безопасно доказать, какая реплика принадлежит пользователю. Снижение автоматических
query-кандидатов с 11 453 до 6 630 — осознанное устранение ложных заголовков, операторских
вопросов и неоднозначных фрагментов, а не потеря контента.

Итоговые query-only splits:

| Split | Кейсы |
|---|---:|
| `calibration` | 6 186 |
| `validation` | 259 |
| `holdout` | 185 |

Итоговые conversation splits:

| Split | Диалоги |
|---|---:|
| `calibration` | 6 207 |
| `validation` | 262 |
| `holdout` | 186 |

Query-only и conversation представления используют один `ProductSplitPlan`. Он объединяет
тикеты по `ticket_id_hash` и общей duplicate-family. Любой компонент с неполной ролью
принудительно остаётся в calibration. Проверка финального корпуса: 0 тикетов и 0 duplicate
components пересекают разные splits.

### Безопасная ручная разметка

Из 6 186 calibration-кейсов подготовлена metadata-only очередь:

- 20 наиболее частых сочетаний `intent × aspect × entity class`;
- 300 кейсов;
- 300 уникальных duplicate clusters;
- query, ответы операторов и названия сущностей в CSV не выводятся.

Manifest находится рядом с приватным корпусом:
`data/private/tickets/product_baseline_20260729_roles_v1/top20_review_manifest.csv`.
Для допуска кейса требуются:

- `label_verdict=approved`;
- `include_in_calibration=true`;
- заполненные `reviewer` и timezone-aware `reviewed_at`;
- `role_verdict=confirmed_user_turn`;
- совпадающие `source_schema_version` и `source_case_fingerprint`;
- SHA-256 именно того KB snapshot, по которому принимался verdict.

Для `answer` обязательны `answerable_from_snapshot=true` и хотя бы один chunk, который
существует в этом snapshot и имеет одновременно `status=published` и
`source_type=yonote`. Для `clarify` и `escalate` factual chunks запрещены. SHA snapshot
вычисляется по каноническому JSON, поэтому не меняется из-за CRLF/LF или форматирования:

```powershell
.\.venv\Scripts\python.exe scripts\build_reviewed_ticket_ask_cases.py `
  --kb-seed data\knowledge_base_seed.json `
  --print-kb-seed-sha256
```

После разметки ask-набор экспортируется только локально:

```powershell
.\.venv\Scripts\python.exe scripts\build_reviewed_ticket_ask_cases.py `
  --input data\private\tickets\product_baseline_20260729_roles_v1\product_calibration_cases.jsonl `
  --manifest data\private\tickets\product_baseline_20260729_roles_v1\top20_review_manifest.csv `
  --output data\private\tickets\product_baseline_20260729_roles_v1\product_calibration_reviewed_ask_cases.json `
  --kb-seed data\knowledge_base_seed.json
```

Экспорт помечается `privacy_class=private_ticket_derived`. Такой набор нельзя отправить
через общий eval-runner на внешний endpoint или сохранить в `reports/`: разрешены только
loopback/server-local `/ask`, а вход и отчёты обязаны оставаться внутри `data/private`.

### Контроль целостности и качества

Генератор сначала пишет 17 файлов в staging, затем атомарно заменяет целевые файлы и
последним публикует `artifact_manifest.json` с SHA-256. Оборванная или смешанная сборка
не проходит validation. Финальный audit:

- 0 operator-answer flags в query и conversation payload;
- 0 очевидных немаскированных email, телефонов, handles и длинных ID;
- 0 cross-split тикетов и duplicate components;
- 17/17 generated artifacts подтверждены manifest.

Off-aspect eval не использует detector production-verifier и поэтому не повторяет его
blind spots. Для критических профилей автоматически проверяются запрещённые пары:
`dates ≠ application ≠ selection_status ≠ travel`. Например, ответ
«Организаторы довезут участников от точки сбора» распознаётся как `travel` и проваливает
кейс, в котором пользователь спрашивал только даты.

Локальный `/ask` baseline по реальным тикетам пока не запускался: в manifest 0 вручную
одобренных строк, поэтому reviewed ask-файл намеренно не создан. Эти 6 630 weak-кандидатов
не являются доказательством 50–60% closure.

Чтобы не блокировать проверку самого продукта ручной разметкой 300 приватных строк,
подготовлен отдельный синтетический server-local pilot. Он проверяет направления,
выведенные из агрегированной top-20 карты, но не содержит и не пересказывает реальные
обращения. Это directional calibration, а не ticket-level conversion, validation или
независимый holdout.

### Синтетический server-local product pilot

Основной набор:
`eval/cases/product_calibration_synthetic_pilot_20.json`.

- ровно 20 вручную сформулированных синтетических запросов;
- по одному кейсу на каждую агрегированную top-20 stratum;
- 11 ответов, 6 уточнений и 3 контролируемые эскалации;
- 10 factual answer-кейсов требуют конкретный published Yonote chunk, citation и
  устойчивый факт в итоговом тексте;
- каждый кейс проверяет `response_profile`;
- критические профили проверяют запрет подмены:
  `dates ≠ application ≠ selection_status ≠ travel`;
- factual cases допускают только `source_type=yonote`, поэтому даже дополнительная
  XLSX/DOCX/operator-bank citation проваливает кейс.

Отдельный P0 regression:
`eval/cases/product_date_aspect_regression_v1.json`.

- смена «Правда» форума «Территория смыслов» должна вернуть период проведения;
- первая смена «Машука» должна вернуть период проведения;
- регистрация, статус отбора и трансфер в этих ответах запрещены.

Перед commit оба набора сравниваются с 6 186 приватными calibration queries. Exact
normalized совпадения, общий непрерывный фрагмент от восьми токенов и высокий
5-gram Jaccard запрещены. Audit выводит только synthetic case ID и тип совпадения,
никогда не выводит приватный текст:

```powershell
.\.venv\Scripts\python.exe scripts\audit_synthetic_case_privacy.py
.\.venv\Scripts\python.exe scripts\audit_synthetic_case_privacy.py `
  --cases eval\cases\product_date_aspect_regression_v1.json
```

После того как пользователь развернёт точный trusted SHA, оба запуска выполняются
внутри server-local acceptance network, не через VK/HDE:

```bash
python -m eval.run_ask \
  --cases eval/cases/product_calibration_synthetic_pilot_20.json \
  --output /evidence/product_calibration_synthetic_pilot_20.json \
  --markdown /evidence/product_calibration_synthetic_pilot_20.md \
  --target http://app-ml:8000/ask \
  --bypass-cache \
  --max-llm-cost-rub 20 \
  --fail-on-any-case \
  --require-complete-traces

python -m eval.run_ask \
  --cases eval/cases/product_date_aspect_regression_v1.json \
  --output /evidence/product_date_aspect_regression_v1.json \
  --markdown /evidence/product_date_aspect_regression_v1.md \
  --target http://app-ml:8000/ask \
  --bypass-cache \
  --max-llm-cost-rub 10 \
  --fail-on-any-case \
  --require-complete-traces
```

Acceptance обоих запусков: 100% case pass, 100% trace coverage, правильный
`response_profile`, обязательный факт в тексте, ожидаемый chunk/citation и отсутствие
запрещённых аспектов и источников. При любом провале HDE/VK smoke не начинается:
сначала выполняется один correction cycle по точной причине из trace.

Следующий измерительный gate после synthetic pilot не меняется: ручной verdict
top-20 очереди, затем отдельный frozen holdout минимум на 400 полноценных тикетов.
Только он может подтвердить или опровергнуть 50–60% closure.

Проверки change set:

- Ruff: успешно;
- pytest: `1662 passed, 1 skipped`;
- KB validation: `2186 valid / 2152 published`;
- private corpus audit: 0 cross-split, 0 operator-copy flags, 0 obvious unmasked
  contact/long-ID patterns;
- deployment и HDE/VK smoke: не выполнялись.
