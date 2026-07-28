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
