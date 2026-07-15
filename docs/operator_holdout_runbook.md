# Активный операторский holdout

**Статус:** запущен 15 июля 2026 после решения `LIMITED GO` и передачи бота операторам.
**Главная цель:** измерить долю полных HDE-тикетов, закрытых ботом без оператора, на новых
обращениях, которые не использовались для настройки текущего кандидата.

Этот документ — оперативная инструкция на время теста. История исправлений и server evidence
находятся в `docs/CURRENT_STATE.md`, действующие ограничения — в `docs/DECISIONS.md`.

## Зафиксированный кандидат

- Код ответов в работающих `app/app-ml`: RC `8bca860`.
- Migration: `007_hde_delivery_telemetry` (`head`).
- Qdrant: `knowledge_base=2152` published records; полный seed содержит `2186`, из них `34`
  archived.
- Финальный server-local gate: smoke `16/16`; Yonote `15/15`; safety `16/16`; off-topic `8/8`;
  PII `4/4`; adversarial `66/66`; follow-up `16/16` ходов и `4/4` диалога.
- Forums `10/11` только из-за известного citation-ID mismatch при полном grounded-ответе; это
  принято как неблокирующий regression artifact.
- Реальный HDE gate: stable `message.id`, одна trace/одна отправка на inbound,
  `delivery_status=delivered`, HTTP `200`, сохранение multi-turn контекста.
- Общая админка: `https://139.100.225.44/admin/kb`; HTTPS и `/ready` проверены извне.
- Сертификат IP действует до `2026-07-22 02:55:43 UTC`; Certbot dry-run успешен, systemd renewal
  запускается дважды в сутки.

## Граница cohort

Предварительная нижняя граница — `2026-07-15 11:54:43+00`, момент завершения HTTPS provisioning
перед подтверждением передачи теста. Перед финальным расчётом нужно заменить её на timestamp
первого фактического операторского HDE-тикета, если он начался позднее. В cohort включаются только
новые HDE tickets; повторное использование старого `chat_id/ticket_id` помечается отдельно.

Не считать calibration/regression запросы, ручные smoke и обращения разработчика независимым
holdout. Не публиковать сырые тексты или идентификаторы.

## Freeze на время теста

До пакетного разбора результатов запрещено:

- менять код ответов, routing, prompts, thresholds, cache policy и KB;
- выполнять `Save`, `Reindex`, `Apply to KB` в админке;
- запускать полную индексацию, очищать `response_cache`, Redis или историю диалогов;
- менять HDE dispatcher payload и stable `message.id={last_post_id}`;
- исправлять единичный неудачный вопрос сразу после его обнаружения;
- открывать sealed holdout при последующей калибровке.

Разрешены просмотр/поиск, `Validate`, ops/quality reports и `Yonote Preview`. Infrastructure P0/P1
исправляется немедленно только при недоступности сервиса, потерях/дублях доставки, safety-дефекте
или риске утечки данных. Ответы операторов не становятся KB-фактами автоматически.

## Что мониторить без изменения поведения

В админке открыть `Работа бота` и смотреть ticket outcomes, delivery, latency, стоимость, cache,
эскалации и проблемные темы. Серверный агрегированный отчёт за сутки:

```bash
cd /opt/rosmol-ai-bot
docker compose \
  -f docker-compose.yml \
  -f docker-compose.ml.yml \
  --profile ml \
  exec -T app-ml python scripts/report_traces.py --days 1
```

Минимальная ежедневная проверка эксплуатации:

```bash
curl -fsS -w '\n' https://139.100.225.44/ready
systemctl status --no-pager rosmol-admin-tls-renew.timer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.ml.yml \
  --profile ml \
  logs --since 24h app-ml nginx
```

Искать `hde_send_`, `hde_delivery_trace_update_failed`, `request_trace_log_failed`, HTTP `5xx`,
OOM/restart и повторные ответы. Логи не копировать в Git, если в них есть пользовательские данные.

## Как считать технический containment proxy

`ops-report` группирует HDE turns по `ticket_id_hash` и отдаёт:

- `bot_resolved_first_turn`;
- `bot_resolved_multi_turn`;
- `operator_required`;
- `unresolved_clarification`;
- `not_delivered`, `delivery_unknown`, `error`, `unresolved`.

Технический proxy:

```text
(bot_resolved_first_turn + bot_resolved_multi_turn) / traced HDE tickets cohort
```

Уточнение считается успешным только после последующего delivered answer. Любая эскалация внутри
тикета делает автоматический исход `operator_required`. Этот показатель не доказывает качество:
система пока не знает, признал ли оператор ответ полным и правильным. Он также не видит входящий
ticket, если webhook вообще не дошёл до приложения, поэтому не является финальной конверсией.

Финальная conversion without operator считается по операторскому реестру:

```text
полностью закрытые ботом tickets / все in-scope tickets операторского cohort
```

Реестр нужно сверить с traces. Любой in-scope ticket без trace нельзя исключать из знаменателя:
он размечается как `delivery_or_channel_error` либо `unresolved` после проверки причины.

Для точного cohort вместо rolling `--days` новый чат должен использовать ту же CASE-логику из
`src/ops/reports.py::_fetch_ticket_outcomes`, заменив lookback на зафиксированный
`TIMESTAMPTZ` старта. До подтверждения первого operator timestamp не зашивать новую дату в код.

## Что получить от операторов

Для каждого спорного тикета нужен файл или таблица со следующими полями:

- ticket id или безопасный псевдоним, дата и весь порядок реплик;
- вердикт: `correct`, `partial`, `wrong`, `unnecessary_escalation`, `missing_escalation`,
  `delivery_or_channel_error`;
- смог ли бот полностью закрыть тикет без оператора;
- ожидаемое поведение и, если известно, правильный ответ;
- официальный источник/владелец факта либо отметка `source missing`;
- комментарий, что именно было неясно, устарело или неполно.

Сырые файлы сохраняются только в `data/private/`. Перед Git допускаются только обезличенные
regression cases и агрегаты.

## Stop-criteria

Тест приостанавливается и начинается infrastructure/safety triage, если наблюдается хотя бы одно:

- safety-запрос получил обычный предметный ответ вместо немедленной эскалации;
- подтверждённый unsupported/hallucinated факт с риском для пользователя;
- дубли публичного ответа, потеря accepted inbound или повторяемые delivery failures;
- публичный webhook/ML runtime недоступен либо растёт HTTP `5xx`;
- утечка секрета или немаскированных персональных данных.

Обычный неправильный, неполный или избыточно уточняющий ответ фиксируется в batch и не снимает
freeze сам по себе.

## План после получения результатов

1. Зафиксировать конец cohort и выгрузить агрегаты/trace evidence без секретов.
2. Совместить технические outcomes с ручными operator verdicts по полному ticket.
3. Посчитать first-turn closure, multi-turn resolution, итоговую conversion without operator,
   justified escalation, unsupported claims, delivery success, latency p50/p95 и стоимость.
4. До исправлений разделить новые кейсы на calibration и sealed holdout.
5. Классифицировать failures: knowledge gap, entity/topic, retrieval, rerank, synthesis,
   verification, session/context, policy либо channel/infrastructure.
6. Выбрать минимальный пакет с наибольшим влиянием на конверсию; не смешивать KB, routing,
   thresholds и prompts без доказанной необходимости.
7. Добавить regression-тесты, выполнить calibration один раз и затем один sealed holdout.
8. Только после нового server-local gate и короткого HDE smoke принимать следующий release
   decision.

## Остаточные риски

- `BackgroundTasks` не является durable outbox; широкий трафик требует persistent worker/outbox.
- Shared `ADMIN_AUTH_TOKEN` даёт write-права; операторам разрешён только read-only режим.
- Screenshot-only запросы требуют controlled escalation, OCR/vision нет.
- Автоматический containment proxy требует ручного operator verdict для итоговой конверсии.
- После тестового окна нужно планово ротировать Cloud.ru и Yonote API tokens, которые ранее
  раскрылись в локальном служебном выводе Compose. Значения не находятся в Git; ротацию выполнять
  согласованно, чтобы не прервать генерацию и Yonote read-only sync.
- `USER_HASH_SECRET` при этой ротации не менять: согласно D-022 это разорвёт pseudonym, session и
  ticket-level continuity.
