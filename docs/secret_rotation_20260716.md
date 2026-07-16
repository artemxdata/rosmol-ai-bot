# Ротация секретов после инцидента 15 июля 2026

## Статус

**Начато:** 16 июля 2026  
**Режим:** `SECURITY HOLD`; production runtime отсутствует  
**Источник доверия:** локальный clean checkout и `origin/master`; старая VM остаётся `SHUTOFF`  
**Текущий этап:** revoke-only; Cloud.ru API key, GitHub server deploy key и Yonote token отозваны
владельцем; новые credentials намеренно не создаются до отдельной clean-rebuild задачи

Этот документ — журнал статусов, а не хранилище секретов. Значения, части значений, длины,
пароли, DSN, private keys, recovery codes, cookies и заголовки авторизации сюда не записываются.
Private evidence хранится вне Git; в журнале допустим только его внутренний идентификатор.

## Правила

1. Любой credential, который мог находиться на старой VM, считается раскрытым.
2. Сначала старый credential отзывается у провайдера. Новый создаётся только непосредственно
   перед установкой на чистый хост; период совместного действия старого и нового не нужен.
3. Ничего со старой VM не переносится: `.env`, SSH/TLS/ACME keys, Docker state, volumes,
   PostgreSQL, Redis, Qdrant, backup и runtime-файлы запрещены.
4. Новые значения создаются на доверенном устройстве либо в UI провайдера и хранятся только в
   password manager и новом `.env` с mode `0600`.
5. Нельзя вставлять секреты в чат, shell history, Git, issue, support ticket, screenshot или
   опубликованный лог. В UI секрет копируется непосредственно в password manager.
6. Каждый новый прикладной secret независим и имеет не менее 256 бит случайной энтропии.
7. Ротация считается завершённой только после отзыва старого, выпуска нового, установки на
   clean host и отдельной проверки. Один факт создания нового ключа completion не доказывает.

## Статусы записи

- `not_started` — отзыв старого credential ещё не подтверждён;
- `old_revoked` — старый credential удалён/отозван provider-side;
- `new_created` — новый credential создан и сохранён вне Git;
- `installed_on_clean_host` — новый credential установлен только на новой VM;
- `verified` — позитивный и негативный тесты прошли, секрет полностью заменён;
- `legacy_not_configured` — в старой конфигурации такого credential не было;
- `not_applicable` — проверено, что интеграция не использовалась;
- `local_key_deleted` — точно идентифицированный local keypair удалён; provider-side public-key
  metadata и возможные другие копии проверяются отдельной строкой;
- `blocked` — действие невозможно; причина и следующий шаг указаны без секретных данных;
- `quarantined_legacy` — credential существует только внутри недоверенного выключенного контура,
  provider-side отзыва для него нет; он никогда не переносится, а окончательное уничтожение
  произойдёт вместе со старым диском после forensic hold.

## Реестр

| № | Система / credential class | Старый статус | Новый статус | Зависимости и критерий проверки |
|---:|---|---|---|---|
| 1 | Cloud.ru `CLOUD_RU_API_KEY` | `old_revoked` | `not_started` | Владелец удалил все Cloud.ru API keys 16 июля; audit/usage ещё проверить; новый least-privilege key — только в будущей задаче |
| 2 | GitHub server deploy key | `old_revoked` | `not_started` | Владелец удалил старый deploy key 16 июля; новый отдельный Ed25519 deploy key создать только на новой VM, read-only |
| 2a | GitHub PAT / account SSH keys / OAuth Apps / webhooks / sessions | `not_started` | `not_started` | Проверить security log и account/repository settings; удалить всё серверное, неизвестное или недоверенное |
| 3a | HDE dispatcher rules старого webhook | `not_started` | `not_started` | Удалить только два правила бота со старым endpoint; это не требует изменения глобального API key |
| 3b | HDE global `HDE_API_KEY` и API-user access | `blocked` | `not_started` | Ключ глобальный и может использоваться другими интеграциями; не инвалидировать до dependency inventory и подтверждения Лёши |
| 4 | Yonote `YONOTE_API_TOKEN` | `old_revoked` | `not_started` | Владелец удалил все Yonote tokens 16 июля; новый token позже строго read-only, без `Apply to KB` |
| 5 | Selectel account password, MFA, sessions | `not_started` | `not_started` | Сменить пароль/MFA recovery, завершить старые/неизвестные sessions; старая VM остаётся `SHUTOFF` |
| 6 | Selectel API/application/service credentials | `not_started` | `not_started` | Отозвать все credentials, которые могли быть на VM или имеют неизвестное происхождение; новые — только least privilege |
| 7 | Selectel project SSH keypairs / metadata keys | `not_started` | `not_started` | Удалить старые public keys из control plane; новый admin key не совпадает с GitHub deploy key |
| 8 | Registry / Docker Hub / GHCR credentials | `not_started` | `not_started` | Проверить dashboard; при отсутствии записать `not_applicable`, при наличии отозвать всё старое |
| 9 | DNS/registrar, monitoring, backup, SMTP credentials | `not_started` | `not_started` | Provider inventory; неизвестное происхождение означает обязательный отзыв |
| 10 | n8n credentials и webhook secrets, если использовались | `not_started` | `not_started` | Проверить отдельный n8n credential store/workflows; репозиторий их наличие не подтверждает |
| 11 | `API_AUTH_TOKEN` | `not_started` | `not_started` | Новый независимый secret; `/ask` без/wrong auth даёт `401`, с новым — controlled success |
| 12 | `WEBHOOK_AUTH_TOKEN` | `not_started` | `not_started` | Новый runtime и HDE dispatcher меняются согласованно; без/wrong auth — `401` |
| 13 | `ADMIN_AUTH_TOKEN` | `not_started` | `not_started` | Новый token инвалидирует старые cookies; проверить только HTTPS, Secure/HttpOnly/SameSite cookie |
| 14 | `USER_HASH_SECRET` | `not_started` | `not_started` | Новый независимый secret; намеренно создаёт новую pseudonym/cohort epoch и разрывает старые sessions |
| 15 | `HDE_TRIGGER_PREFIX` | `not_started` | `not_started` | Новый случайный marker, но не считать его аутентификацией; основной контроль — webhook token |
| 16 | PostgreSQL `POSTGRES_PASSWORD` / `POSTGRES_DSN` / eval DSN override | `not_started` | `not_started` | Новый пустой cluster и уникальный пароль; DSN меняется атомарно; migration head, loopback/internal only |
| 17 | Redis credential | `legacy_not_configured` | `not_started` | Старого пароля не было. Обязателен новый пустой instance без старого volume и без public `6379`; auth — отдельный security patch |
| 18 | Qdrant credential | `legacy_not_configured` | `not_started` | Старого API key не было. Новый пустой instance, без public `6333/6334`, reindex только из trusted published seed; auth — отдельный patch |
| 19 | Dedicated admin SSH keypair старой VM | `local_key_deleted` | `not_started` | Удалены только `id_ed25519_server` и его `.pub`, однозначно привязанные к старому host; другой project key сохранён; Selectel profile metadata закрывается строкой 7 |
| 20 | GitHub read-only deploy key новой VM | `not_applicable` | `not_started` | Создать только на clean host; в журнал записать только label и public fingerprint |
| 21 | TLS private key и certificate | `not_started` | `not_started` | Старый cert отозвать, если возможно; новый key/cert на clean host, проверить chain/endpoint/renewal |
| 22 | ACME account key/state | `not_started` | `not_started` | Старый state не переносить; новый account создать с нуля и проверить renewal dry-run |
| 23 | SSH host keys новой VM | `not_applicable` | `not_started` | Генерируются новой OS; fingerprint сверяется при первом подключении, старые host keys не используются |

`HDE_API_EMAIL`, `HDE_BOT_USER_ID`, service URLs, model names, old IP/UUID и Git commit SHA не
являются секретами. Их корректность проверяется отдельно, но они не отмечаются как rotated.

В текущем проекте не обнаружены используемые `GIGACHAT_API_KEY`, `GIGACHAT_ACCESS_TOKEN`,
developers.sber.ru OAuth, VK Bot API token, MAX Bot API token или Hugging Face token. Это не
заменяет provider-side inventory: если такой доступ существовал вне репозитория, его нужно
добавить в таблицу и отозвать.

## Порядок выполнения

### 0. Внешняя автоматика и evidence

- подтвердить, что HDE dispatcher rules старого endpoint выключены;
- старую VM не включать и не удалять до решения Selectel по forensic evidence;
- фиксировать UTC, provider object label/ID, scope и private evidence reference.

### 1. Немедленный отзыв внешних ключей

Порядок минимизирует финансовый и supply-chain риск. Выполнено: Cloud.ru API key, GitHub server
deploy key и Yonote token. Остаётся:

1. удалить HDE dispatcher rules старого webhook; глобальный `HDE_API_KEY` отдельно ожидает
   подтверждения владельца зависимостей;
2. Selectel API/application credentials, project SSH keys и control-plane sessions;
3. GitHub PAT/account SSH/OAuth/webhooks/sessions, если они существуют;
4. registry, DNS, n8n и остальные найденные интеграции.

На этом этапе новые ключи вообще не выпускаются. Их создание и установка — отдельная будущая
задача после фиксации полного provider-side отзыва.

### 2. Новые локальные и datastore credentials

На доверенном endpoint создать независимые `API_AUTH_TOKEN`, `WEBHOOK_AUTH_TOKEN`,
`ADMIN_AUTH_TOKEN`, `USER_HASH_SECRET`, `HDE_TRIGGER_PREFIX` и новый PostgreSQL password/DSN.
Значения сохраняются в password manager; в журнале меняется только статус.

### 3. Clean host credentials

На новой VM из vendor image создать SSH host keys, отдельный admin key access, отдельный GitHub
read-only deploy key и новый TLS/ACME state. Новый `.env` набирается заново; старый не читается и
не копируется.

### 4. Provider credentials для runtime

Только после подготовки clean host выпустить новые Cloud.ru, HDE и Yonote credentials и
установить их в новый `.env`. HDE dispatcher остаётся выключенным до полного release gate.

### 5. Проверка и handoff

- negative auth tests для `/ask`, webhook и admin;
- provider-specific safe checks без реальных пользовательских данных;
- PostgreSQL migration head, Redis/Qdrant network isolation, `/ready`;
- reindex Qdrant только из versioned published seed;
- server-local quality gate и один контролируемый HDE delivery/dedupe smoke;
- включение нового dispatcher последним;
- новая граница operator cohort — первый реальный HDE trace после handoff.

## Формат evidence

Для подтверждённого шага добавляется строка без секретов:

| UTC | Система | Действие | Нечувствительный объект/scope | Проверка | Private evidence ref |
|---|---|---|---|---|---|
| 2026-07-16T05:04:44Z | Cloud.ru | Все старые API keys удалены владельцем | LLM API credentials | Подтверждение владельца; provider audit/usage pending | incident chat |
| 2026-07-16T05:04:44Z | GitHub | Старый server deploy key удалён владельцем | repo deploy access | Подтверждение владельца; account security inventory pending | incident chat |
| 2026-07-16T05:04:44Z | Yonote | Все старые API tokens удалены владельцем | Yonote API access | Подтверждение владельца | incident chat |
| 2026-07-16T05:04:44Z | Local workspace | Старый ignored `.env` удалён без чтения содержимого | local secret file | Exact path verified inside workspace; `.env.example` сохранён | local command result |
| 2026-07-16T05:04:44Z | Local SSH | Dedicated keypair старой VM, её SSH config и host-key записи удалены | public fingerprint `SHA256:trScMDf+4p3DC/9LYfnYeaVrFrGOaUmdZ8yLpZGyKz4` | Конфиг однозначно указывал на старый host; key другого проекта `id_ed25519` сохранён | local command result |
| 2026-07-16T05:21:10Z | HDE | Ротация глобального API key остановлена до согласования | Global HDE API access | Ключ может обслуживать чужие интеграции; ожидается подтверждение Лёши | incident chat |

Допустимы provider status `revoked/deleted`, label, role/scope, HTTP status безопасного теста,
public SSH fingerprint, TLS serial/fingerprint, trusted Git SHA, новый VM UUID и агрегированные
readiness/count results. Запрещены token suffix/value, password/DSN, private key, recovery code,
cookie, Authorization header, `.env` и HDE payload с персональными данными.

Старый локальный `.env` удалён 16 июля без чтения содержимого. Это не означает provider-side
отзыв находившихся в нём credentials: их статусы закрываются отдельно строками реестра.

## Следующий точный шаг

В HDE удалить только два dispatcher rule старого webhook. Глобальный `HDE_API_KEY` не менять до
ответа Лёши о зависимых интеграциях. Параллельно перейти к read-only inventory Selectel API keys,
service users и profile SSH keys; ничего неизвестного не удалять без идентификации.
