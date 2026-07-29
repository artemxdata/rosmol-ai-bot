# Независимый product baseline v2: 80 реальных first-turn запросов

Дата подготовки: 29 июля 2026 года.

Статус: model-assisted pre-run review завершён, выборка запечатана и экспортирована, но ещё не
запускалась. Ответы текущего runtime не просмотрены, изменения prompts, routing, thresholds,
Yonote, Qdrant и KB под эти кейсы не вносились. Целевой runtime зафиксирован на commit
`4c6262455d1338c6e0f26b8900a5f66e64a97489`. Сервер и HDE/VK этапом подготовки не
затрагивались; HDE rules остаются выключенными.

## Что именно мы измеряем

Два кейса «Правда» и «Машук» были полезным regression smoke: они показали дефект связывания
факта с конкретным чанком. Однако по двум заранее выбранным вопросам нельзя оценить продукт.

Новый набор измеряет другое:

- долю запросов, которые бот закрывает полным ответом с первого сообщения;
- корректность аспекта: дата не должна превращаться в трансфер, регистрацию или статус;
- retrieval, citation binding, grounded answer и обоснованность эскалации;
- длину ответа, лишние детали и фактическую точность;
- распределение причин провалов по слоям pipeline.

Это **не общая ticket-conversion**. В наборе нет replay полного диалога, а корректное уточнение
или эскалация не закрывает тикет. Результат называется directional first-turn baseline.

## Воронка данных

| Этап | Количество |
|---|---:|
| Исходные тикеты 2026 года | 24 220 |
| Query-кандидаты после очистки и восстановления ролей | 6 630 |
| Доля исходного корпуса, попавшая в query-only split | 27,37% |
| Calibration | 6 186 |
| Validation | 259 |
| Chronological holdout | 185 |
| Подходящие пользовательские `single_turn` в holdout | 172 |
| Запечатанная выборка | 80 |

Один исходно выбранный элемент оказался не пользовательской репликой. До запуска он был
исключён с причиной `not_user_turn` и заменён следующим детерминированным кандидатом той же
страты. Все 172 подходящих кандидата относятся к каналу `ЕСЗ Текстовая линия`. Поэтому
результат нельзя автоматически переносить на VK, другие каналы и
многоходовые диалоги.

Из 185 holdout-кандидатов исключены 12 `multi_turn`. Их нельзя честно тестировать одной
склеенной строкой: для них нужен отдельный conversation replay с сохранением порядка ходов и
контекста.

## Состав 80 кейсов

Выборка детерминированно повторяет частотное распределение профилей и route среди 172
подходящих обращений. Внутри неё 80 уникальных ticket hashes, duplicate clusters и duplicate
components.

| Response profile | Кейсы |
|---|---:|
| `selection_status` | 18 |
| `generic` | 15 |
| `application` | 9 |
| `technical` | 8 |
| `documents` | 7 |
| `grants` | 7 |
| `dates` | 6 |
| `program` | 3 |
| `eligibility` | 2 |
| `food` | 2 |
| `travel` | 2 |
| `accommodation` | 1 |
| `accessibility` | 0 |

Дополнительные срезы:

- ожидаемый `answer` — 72;
- ожидаемый `escalate` — 8;
- май 2026 года — 47;
- июнь 2026 года — 33;
- `single_turn` — 80.

Это pre-review распределение по эвристически восстановленным labels. Reviewer вправе исправить
профиль или route без замены выбранного кейса; контракт сохраняет отдельно исходные и
подтверждённые counts, чтобы исправление разметки не выглядело как изменение выборки.

Редкий профиль `accessibility` этим holdout не измеряется. Он должен появиться в отдельном
целевом наборе, но нельзя искусственно добавлять его в частотный denominator и затем называть
результат конверсией реального потока.

## Насколько набор независим

До запуска автоматически проверяется нулевое пересечение holdout с calibration и validation
по трём известным признакам:

- ticket hash;
- duplicate cluster;
- duplicate component.

Lexical duplicate components, пересекающие временную границу, заранее принудительно оставлены в
calibration. Это защищает от точных и известных шаблонных дублей.

Ограничение: текущая процедура не доказывает отсутствие всех смысловых перефразировок между
split. Поэтому корректная формулировка — «независимый по запечатанным ID и известным duplicate
components». Перед внешним заявлением о качестве нужен отдельный semantic leakage audit или
ручная проверка ближайших соседей.

После просмотра ответов эти 80 кейсов перестают быть независимым holdout для следующего цикла.
Их можно сохранить как regression/calibration, а следующий результат подтверждать на новой
запечатанной выборке.

## Если pre-run разметку подготовил Codex

Такую разметку нельзя называть `human_reviewed`, даже если Codex проверил все строки, источники и
privacy-поля. Для неё используется отдельный режим `model_assisted_prerun`.

Он разрешён только для обезличенного sealed holdout и сохраняет все строгие проверки:

- ровно 80 запечатанных ID, freeze, source/workbook/manifest/seed SHA и payload SHA;
- privacy, role, route, chunk и Yonote-only gates;
- server-local `/ask`, сверка runtime SHA, bypass cache, PostgreSQL traces и one-shot ledger;
- запрет на изменение prompts, routing, thresholds и KB до разбора результата.

Режим должен быть указан одинаково при seal и export:
`--review-mode model_assisted_prerun`. Runner принимает такой набор только с явным
`--allow-model-assisted-prerun`. Review mode входит в хеш каждой строки и execution-контракт,
поэтому нельзя запечатать модельную разметку, а затем экспортировать её как человеческую.
Дополнительно model-assisted строки требуют псевдоним reviewer с префиксом `codex-`, `model-`
или `ai-`; такой псевдоним блокируется в режиме `human_reviewed`.

JSON-отчёт, Markdown и receipts явно получают статус
`provisional_model_assisted_prerun`, `product_verdict_eligible=false` и
`human_product_verdict=false`. Такой запуск показывает распределение технических и продуктовых
ошибок, но не является human product verdict и не доказывает конверсию. Он всё равно раскрывает
ответы и расходует one-shot selection: повторно прогонять те же 80 как независимый holdout
нельзя. Для продуктового вывода после запуска нужен отдельный человеческий post-run verdict.

## Почему перед запуском нужен pre-run review

Исходный XLSX не содержит надёжного speaker metadata, а labels восстановлены эвристиками.
Операторские ответы намеренно не используются как factual ground truth. Поэтому каждый кейс
до первого `/ask` должен пройти проверку:

1. `Role verdict`: это действительно пользовательский запрос.
2. Обезличенный тестовый текст не содержит ФИО, контактов, аккаунтов, документов, адреса,
   персональной даты рождения и других идентификаторов.
3. `response_profile`, entity и route подтверждены или исправлены.
4. Для `answer` подтверждено `answerable_from_snapshot=true`.
5. Указаны опубликованные Yonote chunks, достаточные для ответа.
6. Для `clarify` и `escalate` factual chunks не назначаются.
7. Дата отдельно помечена как `not_present` или `event_date_only`; персональная дата блокирует
   экспорт.
8. Указаны псевдоним reviewer без контакта и timezone-aware `reviewed_at`.

Если хотя бы один кейс нельзя безопасно использовать, запрещено молча запускать оставшиеся 79.
До просмотра runtime-ответов нужно сформировать и заново запечатать полноценную заменяющую
выборку из 80.

Приватные workbook-файлы хранятся только в `data/private`. В Git, Docker image и
release-артефакты они не входят. Сначала создаётся неизменяемый template, его SHA входит во
freeze. После freeze из template создаётся отдельная рабочая копия; reviewer заполняет только
её. Перед export заполненная рабочая копия сохраняется как отдельный read-only pre-run snapshot.
Именно его SHA связывается с JSON. После export создаётся ещё одна копия, но post-run verdict
вносится в неё только после запуска; pre-run snapshot больше не редактируется. На сервер
передаётся только обезличенный JSON.

## Что проверяет технический контракт

Цепочка fail-closed связывает выборку, заявленную provenance pre-run разметки и запуск:

1. Freeze повторно запускает детерминированный selector и требует получить те же 80 case ID.
   Затем он фиксирует source, selection, calibration, validation, runtime SHA, KB seed,
   отдельный synthetic suite и неизменяемый workbook template.
2. Импорт заполненной рабочей копии workbook сверяет каждый приватный запрос с исходным source,
   а также case ID, fingerprint, cluster, source SHA, selection SHA и freeze SHA. SHA
   заполненной копии записывается отдельно и не подменяет SHA template.
3. Reviewed manifest содержит только разрешённые review-поля. Raw query, operator answer,
   формулы, macros, external links и лишние CSV-колонки запрещены.
4. Seal повторно импортирует заполненный workbook, требует точного совпадения всех разрешённых
   CSV-полей и только затем рассчитывает отдельный `review_payload_sha256` для каждой строки.
   Изменение route, профиля, chunks, privacy verdict, reviewer или `review_mode` после seal
   обнаруживается.
5. Export независимо повторяет сверку reviewed manifest с заполненным workbook, требует точного
   совпадения всех 80 ID и сверяет source, selection, freeze, reviewed manifest и KB seed.
6. Каждый exported case разрешает factual citations только с `source_type=yonote`.
7. Общий `cases_payload_sha256` связывает execution-семантику всех 80 кейсов: запрос, route,
   profiles, chunks и source policy. Отдельный SHA всего JSON-файла связывает также сам
   execution-контракт; оба digest передаются runner извне и пересчитываются перед запуском.
8. Runner принимает только exact-80, проверяет `/ready.release_git_sha`, требует bypass cache и
   PostgreSQL trace lookup, запрещает `--max-cases` и создаёт атомарный receipt до первого
   `/ask`. Ключ receipt строится по стабильному digest 80 выбранных case ID, поэтому повторный
   freeze с новой датой, runtime или именем файла не открывает выборку заново.
9. Completed baseline возможен только при 80 HTTP-ответах, 100% trace coverage и нулевых cache
   hits. Частичный budget stop и infrastructure failure сохраняются как невалидный прогон.

Git SHA и seed SHA не доказывают состояние изменяемой Qdrant сами по себе. Перед и после
server-local запуска отдельно сверяются collection count и отсутствие изменения KB. Это
операционный gate, а не повод передавать на сервер сырые тикеты.

One-shot ledger имеет один канонический путь `data/private/sealed-holdout-ledger-v1`, который
попадает в существующий persistent private mount runtime. Гарантия «один запуск» действует при
сохранности этого каталога. Пользователь с привилегиями на сервере технически может удалить
ledger или изменить код, поэтому это аудируемый операционный контроль, а не защита от владельца
хоста.

## Как ставится продуктовый вердикт

Автоматический `pass_rate` runner — диагностическая метрика, а не product conversion. Например,
корректная эскалация может пройти machine expectation, но не считается закрытием ботом.

Строгий first-turn closure ставит reviewer после запуска. Кейс закрыт только если одновременно:

- пользователь получил фактический ответ, а observed behavior — `answer`;
- `resolved`, `correct_aspect`, `concise` и `no_unasked_details` равны `pass`;
- `grounded` и `factual` равны `pass` или обоснованному `na`;
- эскалации не было;
- ответ не подменяет запрошенный аспект соседней темой.

Отдельно считаются:

- justified escalation rate;
- clarification correctness;
- retrieval recall;
- expected citation hit;
- Yonote-only source policy;
- latency p50/p95;
- стоимость LLM;
- доля каждого root cause.

Post-run verdict желательно проводить вторым reviewer, который не менял labels и не видел
ответы операторов. В workbook для этого есть отдельные reviewer и timestamp.

## Root-cause taxonomy

Для каждого незакрытого кейса выбирается одна primary-причина по самому раннему сломанному слою:

- `clarification` — лишнее, неверное или отсутствующее уточнение;
- `retrieval_miss` — нужный источник не найден;
- `rerank_selection` — источник найден retrieval, но потерян при отборе;
- `citation_binding` — факт найден, но не связан с подтверждающим чанком;
- `conditional_merge` — смешаны разные возрастные, сменные или иные условия;
- `generation_drift` — генератор добавил, исказил или заменил аспект;
- `verifier_block` — корректный grounded answer заблокирован без более раннего дефекта;
- `operator_only` — подтверждённого источника действительно недостаточно;
- `delivery` — правильный ответ не дошёл из-за transport/channel слоя.

Если правильный chunk найден, но citation не сформирована и verifier блокирует ответ, primary
root cause — `citation_binding`, а не `verifier_block`.

## Отдельные наборы, которые нельзя смешивать

`eval/cases/product_calibration_synthetic_pilot_20.json` содержит 20 синтетических calibration
кейсов: 11 `answer`, 6 `clarify`, 3 `escalate`. Это не независимый stress-набор и не часть
denominator 80. В нём нет отдельного покрытия `accessibility`, multi-turn replay и гарантированного
cross-aspect stress.

`eval/cases/product_date_aspect_regression_v1.json` содержит два известных regression-кейса
«Правда» и «Машук». Они нужны после исправлений, но также не доказывают общее качество.

## Что результат скажет о build vs buy

После blind verdict строится распределение ошибок, а не список отдельных неудачных ответов.

- Если retrieval recall высокий, а большинство провалов сосредоточено в `citation_binding`,
  `conditional_merge` и избыточном verifier block, сначала исправляется собственный
  composer/verification layer. Dify не устраняет этот класс ошибок автоматически.
- Если после одного контролируемого correction cycle first-turn closure остаётся ниже 40%,
  путаница аспектов выше 15%, а ошибки системно распределены между analysis, generation и
  governance при хорошем retrieval, появляется основание для сравнительного Dify PoC.
- Сравнение собственного ядра и Dify проводится на новой общей sealed-выборке. Нельзя сначала
  исправить своё решение по раскрытым 80 кейсам, а затем сравнивать его с Dify на них же.

80 кейсов дают directional baseline и достаточно полезное распределение причин, но не точную
оценку целевой конверсии 50–60%. Для внешнего продуктового заявления нужны:

- не менее 400 полностью восстановленных независимых диалогов;
- отдельные calibration и holdout;
- human verdict и Wilson 95% confidence interval;
- новый sealed operator cohort после финального release handoff.

## Порядок дальнейшей работы

1. Создаётся workbook template, выполняется freeze, затем из template создаётся рабочая копия.
2. Reviewer заполняет все 80 строк pre-run рабочей копии.
3. Локально импортируются reviewed-поля, выполняются privacy и source gates, затем набор
   запечатывается и экспортируется.
4. Пользователю выдаётся одна секрет-безопасная server-local команда. Codex сервер не трогает.
5. Пользователь запускает exact-80 напрямую через `/ask`; VK/HDE не используются как массовый
   транспорт.
6. Ответы и traces переносятся в private evidence, после чего проводится blind post-run verdict.
7. До завершения root-cause классификации runtime, prompts, thresholds и KB не меняются.
8. По распределению ошибок принимается решение: один regression-first correction cycle своего
   ядра или отдельный Dify PoC.

## Фактически запечатанный v2

В model-assisted pre-run подтверждены все 80 ролей и privacy verdict. После исправления
эвристических labels ожидаемое поведение распределилось так:

- `answer` — 31;
- `clarify` — 10;
- `escalate` — 39;
- factual cases с утверждёнными published Yonote chunks — 31;
- остаточные non-date PII findings — 0.

Строгий инвариант не меняется: factual grounding и citations разрешены только из
`source_type=yonote`. XLSX/DOCX и ответы операторов не могут подтверждать runtime-ответ.

Контрольные значения для единственного server-local запуска:

- freeze contract SHA-256:
  `6291666f604e12212c59510b8b86a21dff5ad12c9c5d48970ac0a3ed00cc4e26`;
- review manifest SHA-256:
  `3fa713f62b48cda3ced611676cb8b4befc280898144039d54fb4b255810f46d3`;
- cases payload SHA-256:
  `1ff86f0e88576dc1d529688ccb362078c64dd68a73858101aa74c3f14a08da5e`;
- exact JSON file SHA-256:
  `bc477d4c641620d0519e348a803d5a7e29852625a4e6f766dcb36bd93143f4db`.

Приватный JSON:
`data/private/tickets/product_baseline_20260729_roles_v1/independent_holdout_80_v2/reviewed_holdout_80_v2.json`.
Он не входит в Git, Docker image или release artifacts.

## Локальная подготовка после pre-run review

Команды ниже не обращаются к серверу, HDE или VK. Они читают только приватные локальные
артефакты, импортируют заполненный workbook, запечатывают review и создают обезличенный JSON:

```powershell
$baselineDir = "data\private\tickets\product_baseline_20260729_roles_v1"
$holdoutDir = "$baselineDir\independent_holdout_80_v2"
$workingWorkbook = "$holdoutDir\independent_holdout_80_review_prefilled_v2.xlsx"
$preRunWorkbook = "$holdoutDir\independent_holdout_80_prerun_sealed_v2.xlsx"
$postRunWorkbook = "$holdoutDir\independent_holdout_80_postrun_review_v2.xlsx"

if (Test-Path -LiteralPath $preRunWorkbook) {
  throw "Pre-run snapshot already exists"
}
Copy-Item -LiteralPath $workingWorkbook -Destination $preRunWorkbook
(Get-Item -LiteralPath $preRunWorkbook).IsReadOnly = $true

.\.venv\Scripts\python.exe scripts\import_ticket_holdout_review_workbook.py `
  --workbook $preRunWorkbook `
  --selection "$holdoutDir\selection_manifest.csv" `
  --source "$baselineDir\product_holdout_cases.jsonl" `
  --freeze "$holdoutDir\independent_holdout_80_freeze_v2.json" `
  --output "$holdoutDir\reviewed_manifest_v2.csv"

.\.venv\Scripts\python.exe scripts\build_reviewed_ticket_ask_cases.py `
  --split holdout `
  --review-mode model_assisted_prerun `
  --input "$baselineDir\product_holdout_cases.jsonl" `
  --manifest "$holdoutDir\reviewed_manifest_v2.csv" `
  --freeze "$holdoutDir\independent_holdout_80_freeze_v2.json" `
  --review-workbook $preRunWorkbook `
  --seal-review-payload-hashes

.\.venv\Scripts\python.exe scripts\build_reviewed_ticket_ask_cases.py `
  --split holdout `
  --review-mode model_assisted_prerun `
  --input "$baselineDir\product_holdout_cases.jsonl" `
  --manifest "$holdoutDir\reviewed_manifest_v2.csv" `
  --freeze "$holdoutDir\independent_holdout_80_freeze_v2.json" `
  --review-workbook $preRunWorkbook `
  --kb-seed "data\knowledge_base_seed.json" `
  --output "$holdoutDir\reviewed_holdout_80_v2.json"

if (Test-Path -LiteralPath $postRunWorkbook) {
  throw "Post-run workbook already exists"
}
Copy-Item -LiteralPath $preRunWorkbook -Destination $postRunWorkbook
(Get-Item -LiteralPath $postRunWorkbook).IsReadOnly = $false
```

Если какой-либо файл уже существует, команда останавливается. `--overwrite` используют только
осознанно до первого runtime-прогона; изменение review после раскрытия ответов означает, что
этот набор становится calibration и для следующей независимой оценки нужен новый holdout.
