# Процесс постоянного улучшения качества

## Принцип

Качество бота повышается не бесконечной настройкой модели, а управляемым циклом: реальные вопросы
пользователей превращаются в подтверждённые пробелы, человек закрывает их в Yonote/KB, затем
versioned RAG release проходит validation, regression и контролируемую индексацию. Система
обучается на подтверждённых knowledge gaps, а не на ПДн, сырых тикетах или автоматически принятых
ответах операторов.

## Текущий режим: limited clean test-production

Операторский тест, начатый 15 июля 2026, прерван P0-компрометацией старого сервера. Новый clean
runtime, постоянный endpoint и ограниченный HDE/VK handoff завершены. Новая измеримая cohort
boundary начинается с первого реального обращения после handoff; smoke и прерванный cohort в неё
не входят. Ротация/принятые исключения и contamination boundary остаются обязательными. Детали:
`docs/CURRENT_STATE.md`, `docs/security_incident_20260715.md` и
`docs/operator_holdout_runbook.md`.

Healthy deployed runtime не меняется вне отдельно согласованных change set и release gate.
Последние тесты Наты зафиксированы в `docs/operator_feedback_20260715.md` как будущий
calibration/regression backlog; они не исправляются по одному и не являются финальной оценкой
конверсии. Автоматический ops-report остаётся containment proxy, а окончательный verdict даёт
оператор по полному тикету.

### Активный цикл Pilot50 от 11 августа 2026

Frozen balanced calibration baseline равен `18/50 = 36%` mechanical first-turn closure:
`11/25 = 44%` на typical и `7/25 = 28%` на atypical. Из `24` эскалаций `18` относятся к
output-contract/fact-binding/length/profile, ещё `6` — к retrieval/source coverage. Поэтому
активная гипотеза ограничена двумя слоями: source-bound сборка короткого multi-aspect ответа и
entity/intent binding retrieval. KB, Yonote, safety rules, thresholds как общий класс и
production runtime не меняются без нового evidence.

V1 содержит измерительный дефект: `11` atypical multi-aspect qrels ссылаются на legacy XLSX/DOCX,
которые published-Yonote-only runtime обязан отбрасывать. V1 сохраняется как historical evidence.
Первый candidate v2 заменил их опубликованными qrels, но post-run diagnostics нашёл ещё три
ошибочных answer-only cases: персональный ticket-status, unanswerable «Амур» без Yonote и
неконкретный FGAIS support. Поэтому sealed v2 тоже не переписывается. V3 сохраняет остальные
`47` query/order/strata и заменяет только эти три позиции конкретными published-Yonote cases.
Межверсионные overall/atypical проценты не объявляются apples-to-apples; отдельно сравнимы только
общие кейсы и неизменный typical slice.

Порядок цикла:

1. Сохранить baseline и frozen membership отдельным tracked safe artifact без вопросов,
   ответов, идентификаторов и approval reference.
2. Добавить regression-тесты на наблюдаемые output-contract и entity-binding отказы.
3. Исправить существующие generation/retrieval/rerank пути минимально, не ослабляя guard и
   grounding.
4. Выполнить focused tests, полный `pytest`, Ruff и `scripts/index_kb.py --validate-only`.
5. Только после зелёного локального gate сформировать immutable v3 candidate и выполнить
   бесплатный isolated-capacity preflight; production не останавливать и не менять.
6. При preflight GO выполнить один server-local v3 run без HDE/VK/rollout. Принять цикл только
   при `>=30/50`, typical `>=11/25`, atypical `>=7/25`, output-contract эскалациях `<=6`, нуле
   source-binding failures на `50/50` published-Yonote qrel-кейсах и нуле провалов `15/50` critical
   regression-кейсов. Это mechanical gate с `human_product_verdict=false`; human-semantic
   качество всех 50 ответов он не заявляет. Иначе STOP, разбор evidence и новая гипотеза до
   следующего платного запуска.

Стоимость baseline eval-проекции — `11.647398 RUB`. Для следующей проверки достаточно runner
projected stop-limit `30 RUB`; разрешённые владельцем `500 RUB` остаются верхней границей на
обоснованный прогон, а не бюджетом, который нужно израсходовать.

Первый v2 candidate `64cc182d37a3c060439ed7a55f5cc875a27d786d` завершён: execution
`50/50`, closure `25/50` (`17/25` typical, `8/25` atypical), cost `13.375452 RUB`, cache `0`,
но quality gate — `STOP`. Не пройдены overall floor (`25 < 30`), output-contract ceiling
(`8 > 6`), source-binding (`5/38`) и critical regression (`7/15`). Typical `11 -> 17` на тех же
25 кейсах — полезный calibration-сигнал; overall/atypical v1/v2 не являются независимым или
apples-to-apples измерением из-за 11 replacement-кейсов.

Цикл продолжается одним новым v3 candidate:

1. Получить из sealed report payload-free offline failure matrix без вопросов, ответов и ID.
2. Сгруппировать провалы по boolean check, allowlisted escalation/retry path и latency bucket;
   зафиксировать конкретные корневые гипотезы.
3. Закрыть серьёзные source-binding, critical и output-contract дефекты regression-first, не
   ослабляя grounding/guards и не меняя KB ради прохождения scorer. Отдельно разобрать рост p95
   `14235 -> 40015 ms` по model/retry path; следующий candidate не должен улучшать closure ценой
   необъяснённого трёхкратного хвоста latency.
4. Пройти полный local gate и бесплатный isolated preflight нового immutable candidate.
5. Выполнить ровно один разрешённый владельцем v3 run с projected stop-limit `30 RUB`; quality
   GO/STOP снова считается завершённым one-shot без выборочного retry. Если запуск попадает в
   rolling-24h окно v2, D-041 допускает только один атомарный exact-bound v2 -> v3 waiver с
   отдельной owner reference и external residual provider-risk ceiling `500 RUB`. Ledger, время
   и run classification не меняются; второй waiver и любой следующий paid retry запрещены.

## Источники данных

- HDE: деперсонализированные тикеты и причины эскалаций.
- Yonote: актуальная документация по форумам и мероприятиям, только чтение.
- Админка KB: ручные правки опубликованных чанков и точечный reindex.
- Trace: cited_sources, generator_model, latency, llm_cost, escalation_reason.

## Еженедельный цикл

1. Зафиксировать начало/конец cohort и экспортировать деперсонализированные полные тикеты за период.
2. Совместить trace outcomes с ручными operator verdicts и присвоить root cause незакрытия:
   `content_missing`, `content_stale`, `retrieval_miss`, `routing_or_generation_failure`,
   `delivery_failure` или `operator_only`.
3. Найти топ-20 пробелов, которые чаще всего ведут к оператору.
4. Для каждого пробела определить решение:
   - улучшить query normalization или alias;
   - добавить/исправить чанк в Yonote;
   - добавить eval-кейс;
   - оставить operator-only, если вопрос опасный или индивидуальный.
5. До изменений разделить новые кейсы на calibration и sealed holdout, затем согласовать один
   пакет исправлений.
6. После content approval получить полный Yonote Preview diff и оформить versioned seed change;
   production admin Apply не использовать.
7. Провести review, validation/regression и контролируемый полный reindex при фактическом изменении
   KB.
8. Прогнать smoke, типовые, нетиповые, safety, off-topic, PII и follow-up.
9. Проверить calibration, затем один раз sealed holdout; зафиксировать метрики и изменения.
10. Связать версию KB с cohort/trace и после релиза измерить эффект на сопоставимой выборке.
    Улучшение на calibration не выдавать за независимый эффект.

## Бюджет платных eval

До любого live eval сначала выполняются локальные unit/regression-тесты, статические проверки и
доступные mock/replay-прогоны. Обычная точечная проверка содержит 8–10 кейсов и запускается с
расчётным budget не более 100 рублей; программный максимум 10 кейсов действует независимо от
точности оценки стоимости. Суммарно на обычные live eval разрешено не более 300 рублей за любые
скользящие 24 часа.

Перед первым `/ask` runner атомарно резервирует запуск в общем постоянном
`eval-cost-ledger-v1`. Реализованный ledger машинно запрещает повторное использование approval ID,
routine reservations свыше 300 рублей за скользящие 24 часа, а также второй полный eval за
скользящие 24 часа или для того же release candidate. Если ledger отсутствует, повреждён или не
может быть атомарно обновлён, платный прогон не начинается. Ledger учитывает расчётные reservations,
а не фактический provider billing.

Запуск более чем на 10 кейсов, запуск дороже 100 рублей или полный Product80 выполняется только
после отдельного одноразового согласования владельца. До старта в согласовании фиксируются точный
runtime SHA, название и версия набора, прогноз стоимости и расчётный верхний budget. Одно
согласование нельзя повторно использовать для другого SHA, набора или попытки. Полный прогон
разрешён максимум один раз на release candidate и максимум один раз за скользящие 24 часа;
начатая или оборванная попытка также учитывается. Автоматические повторы платных прогонов
запрещены.

Live eval без заданного бюджета и unbounded-запуски запрещены. Внутренняя
`llm_estimated_cost_rub` сверяется с фактическим биллингом и не считается финансовой гарантией.
Заранее обнаруженный дефект тарифа блокирует запуск до `/ask`; дефект, обнаруженный только по
trace, останавливает прогон после первого такого кейса и до следующего запроса. Остановка по
pricing или budget означает неполный прогон и не может использоваться как успешное
calibration/holdout evidence. Эти правила не отменяют provenance, integrity и contamination gates.

После каждого live eval владелец бюджета выполняет ручную post-run сверку с биллингом провайдера
за точное UTC-окно запуска. В private evidence или во внешней owner-записи фиксируются
`eval_run_id`, runtime SHA, набор и версия, UTC-окно, approval ID при его наличии,
`llm_estimated_cost_rub`, фактическая сумма, процент расхождения и verdict владельца. Approval ID
не является секретом, но его нельзя придумывать: он должен быть заранее выдан во внешней записи
владельца для конкретного SHA, набора, прогноза и расчётного stop-limit. Один уже начатый запрос
может превысить остаток, поэтому этот лимит не выдаётся за provider hard cap.

Автоматической сверки с provider billing нет. Допустимо расхождение по модулю не более 10%.
Если оно больше, либо фактический счёт нельзя однозначно связать с запуском, действует `STOP` на
следующие платные eval до исправления pricing/trace attribution и нового owner approval. Пока
provider billing ещё не сформирован, сверка остаётся незакрытой и не является финансовым
подтверждением результата.

## Метрики

- Conversion without operator: доля полных tickets, закрытых ботом без оператора за любое число
  сообщений.
- First-turn closure и multi-turn resolution: отдельные составляющие основной конверсии.
- Unresolved clarification: уточнение без последующего grounded delivered answer.
- Operator verdict agreement: совпадение автоматического outcome с ручной оценкой полного ticket.
- Controlled escalation rate: доля корректных передач специалисту.
- Source coverage: доля ответов с подтверждёнными источниками.
- Hallucination rate: целевое значение 0%.
- Latency p50/p95: скорость ответа.
- LLM cost: стоимость генерации.
- Cache hit rate: доля ответов из кэша.
- Top failed topics: темы, которые чаще всего требуют доработки.

## Критерии релиза новой базы

- `scripts/index_kb.py --validate-only` проходит без ошибок.
- Быстрый smoke зелёный.
- Safety hard topics дают controlled escalation.
- Внебазовые вопросы дают scope-note или уточнение, а не выдумку.
- Новые чанки не содержат ПДн и сырые тикеты.
- Изменения источников видны в trace/cited_sources.

## Что отдавать контент-команде

Для каждого gap-а нужен короткий тикет:

- пользовательский вопрос;
- формализованный root cause и evidence, почему бот не закрыл его;
- какой форум/тема;
- частота и какой подтверждённый факт/раздел Yonote нужен;
- контрольные вопросы и критерии приёмки;
- owner и статус:
  `gap_found -> content_approved -> published -> released -> verified_on_real_questions`.

Freshness и сезонная подготовка создают alerts/content tasks, но не изменяют Yonote или
production KB автономно.

## Что не автоматизируем сейчас

- Автономное редактирование Yonote агентом.
- Удаление документов в Yonote.
- Автопубликацию непроверенных ответов из тикетов.
- Массовые HDE-тесты через боевой канал.

Эти ограничения нужны, чтобы не получить быстрый рост качества ценой риска для production и источников данных.

## Human Gold и stage funnel

Для новых системных циклов используется `GoldTicket v1` из
`docs/human_gold_quality_workflow.md`. Weak labels выбирают кандидатов, но не оценивают продукт.
Human review подтверждает полный тикет, action, aspect/constraints, claims и published Yonote
qrels. Gold150 — calibration sanity; независимая конверсия по нему не заявляется.

Каждый observation report обязан сохранять отдельные ordered stages. Legacy union разрешён только
для coarse-аудита. Глобальный source selection может быть exact, но per-question candidate overlap
не считается фактической claim binding. Multi-turn и graded source alternatives оцениваются
canonical GoldTicket scorer, а не несовместимой legacy-проекцией. Offline `eval.stage_funnel`
определяет первый доказуемый loss stage; отсутствие versioned telemetry или human verdict
считается `unscored`. Платный targeted eval запускается только после этого отчёта и одной
проверяемой гипотезы, в пределах D-036.
