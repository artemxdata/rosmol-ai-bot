# Операторский holdout — прерван и ожидает чистого перезапуска

**Статус на 16 июля 2026:** `PAUSED / INTERRUPTED BY P0 SECURITY INCIDENT`.
**Release status:** `NO GO / SECURITY HOLD`.
**Runtime:** старая VM выключена (`SHUTOFF`), бот/webhook/админка offline.
**Причина:** подтверждённые признаки root-компрометации и аномальный исходящий трафик; см.
`docs/security_incident_20260715.md`.

Этот документ сохраняет методику измерения, но **не разрешает** запускать команды, подключаться к
старому IP или продолжать старый cohort. Сначала выполняются отдельная ротация секретов, clean
rebuild и полный security/release gate.

## Последний зафиксированный кандидат — историческая baseline

- Код ответов: RC `8bca860`.
- Migration: `007_hde_delivery_telemetry` (`head`).
- Последний известный Qdrant count: `2152` published из `2186` seed records, `34 archived`.
- Server-local gate: smoke `16/16`; Yonote `15/15`; safety `16/16`; off-topic `8/8`; PII `4/4`;
  adversarial `66/66`; follow-up `16/16` ходов и `4/4` диалога.
- Forums `10/11` из-за известного citation-ID/coverage mismatch при фактически полном grounded
  ответе.
- Реальный HDE gate: stable `message.id`, одна trace/одна delivery на inbound,
  `delivery_status=delivered`, HTTP `200`, multi-turn context сохранён.

Эти данные доказывают только pre-incident regression behavior. Они не являются current runtime
health и должны быть повторены на новой инфраструктуре.

## Прерванный cohort

Предварительная нижняя граница старого cohort была `2026-07-15 11:54:43+00`. До P0 были получены
первые ручные наблюдения Наты, описанные в `docs/operator_feedback_20260715.md`.

Правила использования:

- старый cohort не считается завершённым и не даёт финальную conversion estimate;
- его нельзя объединять с новым cohort;
- старые traces могут использоваться только как предварительный qualitative/calibration input
  после проверки целостности;
- provisioning, smoke, обращения разработчика и regression cases никогда не являются
  независимым holdout;
- сырые тексты/идентификаторы остаются только в `data/private/` и не коммитятся.

## Recovery freeze

До preliminary security/runtime acceptance чистого контура запрещено:

- включать старую VM или обращаться к старому IP, webhook, админке и SSH tunnel;
- переносить что-либо со старого сервера в новый runtime;
- менять код ответов, routing, prompts, thresholds, cache policy, dispatcher payload или KB;
- исправлять по одному кейсы Наты;
- запускать `Save`, `Reindex`, `Apply to KB`, полную индексацию или очистку cache/session;
- считать автоматический containment proxy окончательной конверсией.

Разрешены read-only работа с локальным доверенным Git, документирование, анализ обезличенных
материалов и планирование recovery. До clean deploy также разрешён отдельно согласованный
минимальный infrastructure/security patch для нового endpoint/hardening с обязательными тестами;
он не должен менять response behavior, routing, prompts, thresholds или KB. Все секреты
перевыпускаются отдельной задачей и не попадают в документы/логи.

После preliminary acceptance допускается второе явное исключение: один отдельно согласованный
batch calibration correction по feedback Наты с regression-тестами и content verdict. Только в
этой фазе можно менять необходимые response/routing/KB слои; после неё обязательны финальный
полный gate, HDE smoke и новый handoff. До этой фазовой границы полный quality freeze сохраняется.

## Уже сработавший stop-criterion

Первоначальный holdout должен был останавливаться при утечке секрета, недоступности runtime или
другом P0 security issue. Этот критерий сработал. Обычные дефекты качества Наты сами по себе не
остановили бы тест; причиной остановки является компрометация хоста.

## Условия нового старта

Новый operator cohort разрешён только когда выполнены все пункты:

1. Новая VM создана из чистого vendor image; ничего со старого хоста не перенесено.
2. Все потенциально раскрытые ключи и секреты перевыпущены отдельной задачей.
3. SSH/Firewall/egress/monitoring настроены по `docs/security_incident_20260715.md`.
4. С доверенного устройства проверены GitHub deploy keys/tokens, audit history, commits/tags и
   Actions; server deploy credentials отозваны, trusted commit hash зафиксирован.
5. Код получен clean checkout проверенного trusted commit из `origin/master`; hardcoded старый IP
   удалён/параметризован в отдельном infrastructure patch и защищён тестами.
6. PostgreSQL, Redis и Qdrant созданы с нуля; KB индексирована только из versioned published seed
   frozen RC, без свежего Yonote Apply и без старого snapshot.
7. Preliminary migration head, `/ready`, Qdrant count и server-local smoke зелёные.
8. На чистом runtime воспроизведены кейсы Наты; согласован content verdict и закрыт один
   regression-first correction cycle до открытия нового cohort.
9. После fixes полный server-local suite зелёный, а короткий реальный HDE smoke подтверждает
   stable event id, dedupe, одну доставку и telemetry.
10. В `CURRENT_STATE.md` записаны новый host/commit без секретов и release decision.

Новая cohort boundary — timestamp первого реального операторского HDE trace **после** этого
handoff. Она не переносится из старого теста.

## Freeze во время будущего нового holdout

После нового handoff снова запрещено менять code/routing/prompts/thresholds/cache/KB, исправлять
единичные вопросы и открывать sealed holdout. Разрешены read-only мониторинг и сбор полного ticket
context. Infrastructure P0/P1 корректируется только с regression и новым handoff.

## Что собирать от операторов

Для каждого спорного полного тикета нужны:

- безопасный ticket pseudonym, дата и порядок реплик;
- verdict: `correct`, `partial`, `wrong`, `unnecessary_escalation`, `missing_escalation` или
  `delivery_or_channel_error`;
- полностью ли бот закрыл тикет без оператора;
- ожидаемое поведение и правильный ответ, если он известен;
- официальный источник/владелец факта либо `source missing`;
- краткое описание неполноты, устаревания или неясности.

Сырые файлы хранятся только в `data/private/`. Ответ оператора не становится KB-фактом без
контентной проверки.

## Как считать метрику после нового старта

Главная метрика:

```text
полностью закрытые ботом tickets / все in-scope tickets нового operator cohort
```

Отдельно публикуются first-turn closure, multi-turn resolution, justified escalation, delivery
success, unsupported claims, latency p50/p95 и LLM cost. Уточнение становится успехом только если
следом получен grounded delivered answer. Ticket без trace остаётся в знаменателе как
`delivery_or_channel_error` или `unresolved` после проверки.

`hde_ticket_resolution_rate`/ops-report — только технический containment proxy. Финальный verdict
требует ручной оценки полного тикета.

## Stop-criteria нового holdout

Тест немедленно приостанавливается, если наблюдается хотя бы одно:

- safety-запрос получил обычный ответ;
- подтверждён unsupported/hallucinated факт с риском;
- дубли публичного ответа, потеря accepted inbound или повторяемые delivery failures;
- публичный webhook/ML runtime недоступен или растёт `5xx`;
- утечка секрета/ПДн или иной security indicator;
- аномальный исходящий трафик, CPU или процесс вне ожидаемого Compose runtime.

## План качества после нового cohort

1. Совместить trace outcomes с operator verdicts по полному ticket.
2. Разделить новые failures на calibration и sealed holdout до исправлений.
3. Классифицировать: knowledge, entity/topic, retrieval, rerank, synthesis, verification,
   session/context, policy или channel/infrastructure.
4. Начать с уже зафиксированных кейсов Наты, но не считать их sealed holdout после исправления.
5. Выбрать минимальный пакет с максимальным влиянием на конверсию; не смешивать слои без
   доказанной необходимости.
6. Добавить regression, пройти calibration и один новый sealed holdout.

## Остаточные продуктовые риски

- `BackgroundTasks` не является durable outbox.
- Shared admin token предоставляет write-права; полноценная ролевая read-only модель не сделана.
- Screenshot-only запросы требуют controlled escalation; OCR/vision нет.
- Feedback Наты показал greeting, clarification, source/topic/deadline extraction и answer
  composition gaps.
- Сайт ФГАИС, официальные соцсети и ответы второй линии ещё не подключены как versioned read-only
  источники.
