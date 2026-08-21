# Аудит админ-панели и цикла обновления RAG — 20–21 августа 2026

## Граница проверки

Проверена локальная редакция и первый реальный read-only запуск exact runtime
`3e185a70995c4c1cba42d5f33e9e8b8c47f2fa40`. Runtime/admin/Qdrant gates прошли, Yonote API
успешно прочитал 2 коллекции и 116 документов, но merged semantic gate вернул единственный
`forum_text_conflict`. Seed/Qdrant не менялись и receipt не создавался. Локальный successor
исправляет generic cross-reference classification и делает STOP диагностируемым; его повторный
server run ещё не выполнен. Внешние правила HDE/VK остаются owner-attested и выключенными.
Chatme, его слоты и готовые ответы в проект не импортируются.

Статусы: `РАБОТАЕТ` — подтверждено локальным тестом; `ЧАСТИЧНО` — основной путь есть, но остаётся
ограничение; `НЕБЕЗОПАСНО` — действие запрещено до исправления; `ОТСУТСТВУЕТ` — следующий уровень
продукта, не входящий в текущий release.

| Область | Статус | Что подтверждено | Ограничение / следующий шаг |
|---|---|---|---|
| Login, cookie-сессия, logout | РАБОТАЕТ локально | Missing/wrong token отклоняются; login создаёт TTL-bound Redis session, logout удаляет её и выставляет delete-cookie; replay старой cookie получает `401` | Реальный Redis/TLS, срок сертификата и renewal проверяются только server-local |
| Read-only и test-editor | РАБОТАЕТ | Backend блокирует mutation; UI в read-only отключает Save, Reindex, status и textarea | Это два режима одного owner-token, а не многопользовательский RBAC |
| Список, поиск, фильтры, просмотр | ЧАСТИЧНО | API и browser flow нашли и открыли фактический чанк | UI получает максимум 100 строк и пока не имеет pagination/source-type filter |
| Редактирование и точечный reindex | ЧАСТИЧНО | Published upsert использует канонический payload; draft/archived удаляют point; cache сбрасывается | Нет ETag/optimistic concurrency, истории изменений и rollback одной записи |
| Validate | РАБОТАЕТ | Structural и semantic gate читают один snapshot и возвращают SHA именно проверенных bytes | Registry остаётся отдельным versioned файлом |
| Quality/ops/eval-cases | ЧАСТИЧНО | API/UI и mock-тесты есть | Старый quality report ещё не связан обязательными runtime/seed SHA; ops UI фиксирован на 7 дней |
| Yonote statistics/export | ЧАСТИЧНО | Реальный read-only API probe подтвердил 2 настроенные коллекции и 116 документов; таймауты, общий объём и безопасные ошибки реализованы | Полный приватный экспорт не выполнялся и не должен попадать в Git/чат |
| Полный Yonote Preview | ЧАСТИЧНО | Реальный API/egress/storage подтверждены на 116 документах; один snapshot, пределы, hashes, add/change/remove и chunk audit реализованы | Первый server run остановился на `forum_text_conflict:1`; successor должен вернуть полный `GO` либо безопасный semantic `STOP` с затронутыми chunk IDs |
| Preview против параллельного PATCH | РАБОТАЕТ | Изменение seed во время чтения даёт `409`, receipt не создаётся, ручная правка сохраняется | Межпроцессные editor jobs появятся только в durable publish job |
| Защита от массового удаления | РАБОТАЕТ | Пустой или аномально уменьшившийся Yonote snapshot получает `STOP` без applyable receipt | Осознанное массовое удаление потребует отдельного owner-waiver, которого в текущем API нет |
| Receipt-bound Apply | РАБОТАЕТ локально | Exact id+SHA, schema v2, 24 часа, single-use, conflict при изменении seed, без второго Yonote fetch; pre-audit receipts отклоняются | Apply остаётся выключенным в read-only runtime и не запускает Qdrant |
| Seed ↔ Qdrant runtime-status | РАБОТАЕТ локально | Сравнивается полный канонический payload, включая filter keys, text, embedding input и source metadata; seed повторно хешируется после scan, а изменение во время scan даёт `STOP` | Векторы не пересчитываются; после `GO` обязателен novel-query retrieval smoke |
| Semantic response cache | РАБОТАЕТ локально | Cache schema, point ID, lookup filter и payload привязаны к SHA runtime seed; старый физический point после Apply не становится hit | Старые points удаляются отдельной controlled cleanup, но ответить уже не могут |
| Полная индексация | ЧАСТИЧНО | Production `app`, `app-ml` и `index-kb` используют один `KB_SEED_PATH`; index требует reviewed SHA-256, проверяет bytes до Qdrant и перед success и выполняет `--prune-stale` | Backup/fingerprint/index/restart/rollback выполняет только владелец server-local после review Preview |
| Гибридное ядро | РАБОТАЕТ локально | Один атомарный факт остаётся direct-source; составной bounded ответ идёт через grounded LLM; verifier отклоняет известные typed-противоречия | Обобщение доказывается только запечатанными novel-вопросами после реального Yonote Apply |
| HDE/VK | ВЫКЛЮЧЕНЫ | Acceptance требует явного owner-attestation и проверяет отсутствие прямого VK webhook/активности очередей | Внешние provider rules сервер не видит; после server-local gate нужны отдельные ручные smoke |
| Durable publish job | ОТСУТСТВУЕТ | Намеренно не входит в эту редакцию | После доказанного ручного цикла: background job, progress, lock, backup и rollback в UI |

## Локальный browser flow

На локальном FastAPI без lifespan, внешних каналов и Yonote проверены: экран входа, HttpOnly-cookie
flow, загрузка `2186` записей, поиск по «Ладоге» (`42` результата), открытие чанка, доступность
editor controls в test-editor, logout, повторный вход в read-only и фактическая блокировка Save,
Reindex, status и textarea. При недоступном Qdrant раздел `Seed и индекс` не ломает страницу и
показывает понятную ошибку сервиса. Это UI-проверка, а не server runtime acceptance.

## Первый допустимый server-local запуск

Скрипт `scripts/run_admin_kb_acceptance_server_local.sh` выполняет только read-only аудит exact
candidate: identity/containers/ready, auth/session, Redis-backed logout с replay старой cookie,
chunks, Validate, runtime-status до/после,
нулевую активность HDE, отсутствие прямого VK webhook и полный Yonote Preview. Он не вызывает
Apply, PATCH, Reindex, `/ask`, channel webhook или `index-kb`, не передаёт
`EXPECTED_KB_SEED_SHA256` и не печатает токены, тексты базы, document/chunk IDs и receipt
credentials. При полном `GO` единственное созданное состояние — приватный одноразовый Preview
receipt; semantic/snapshot/chunk-audit `STOP` не создаёт новый receipt и инвалидирует старый
активный receipt. Seed, Qdrant, cache, Yonote и каналы не меняются. Внешнее выключение правил HDE/VK подтверждается
владельцем отдельным аргументом `HDE_VK_DISABLED`, поскольку сервер сам не может наблюдать
provider-side rules. Schema `v2` возвращает exit `2` для безопасного Preview quality STOP и
exit `1` для технического/invariant FAIL. До `GO` каналы остаются выключенными.

Первый реальный запуск 21 августа прошёл все проверки до Yonote Preview и остановился на HTTP
`422`. Безопасная диагностика исключила сеть, token/scope, proxy и filesystem: точная причина —
один `forum_text_conflict` в merged semantic integrity. Следующая редакция переводит такой исход
в проверяемый HTTP `200`/quality `STOP` без receipt и показывает владельцу понятную причину и
затронутый chunk ID; настоящий конфликт при этом не ослабляется.

После exact-SHA deployment владелец запускает в shell сервера только:

```bash
./scripts/run_admin_kb_acceptance_server_local.sh \
  <40-character-lowercase-candidate-sha> HDE_VK_DISABLED
```

`GO` этой команды ещё не означает допуск каналов: затем нужны controlled index/restart и
server-local novel-query regression из последовательности ниже.

## Release-последовательность

1. Exact candidate deploy при выключенных HDE/VK.
2. Read-only acceptance и полный Yonote Preview; review hashes, diff, пустых документов, дублей и
   удаления.
3. До Apply запечатать минимум три новых вопроса: новый объект, изменившийся факт и составной
   вопрос по нескольким секциям.
4. Только после owner review включить isolated test-editor, применить exact one-time receipt и
   проверить merged seed hash.
5. Убедиться, что `ADMIN_KB_SEED_PATH` задаёт один `KB_SEED_PATH` для `app`, `app-ml` и
   `index-kb`; сделать Qdrant backup/fingerprint и передать reviewed `merged_seed_sha256` только
   one-shot index run как `EXPECTED_KB_SEED_SHA256`. Полный index `--prune-stale` должен повторно
   проверить seed перед success, после чего нужно перезапустить runtime и получить Seed ↔ Qdrant
   `GO`.
6. Выполнить novel-query server-local regression. Только затем — короткие HDE/VK smoke и новый
   независимый Blind50 full-ticket gate (`>=25/50`, critical unsupported facts = `0`).
