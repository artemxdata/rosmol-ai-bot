# GitHub → clean server: release workflow

## Решение для recovery release

GitHub используется как доверенный транспорт проверенного исходного кода, но не как хранилище
production secrets и не как средство автоматического deployment. Рабочий цикл:

1. Codex изменяет и тестирует проект локально без product credentials.
2. Изменение коммитится и отправляется в GitHub.
3. Secretless GitHub Actions проверяет exact commit.
4. Только зелёный commit объявляется release candidate полным 40-символьным SHA.
5. Оператор вручную выполняет на clean server `git fetch` и detached checkout этого SHA.
6. Оператор создаёт server-only `.env.production`, запускает real-runtime gate и передаёт Codex
   только обезличенные статусы и отчёты.

Обычный `git pull` на server запрещён: он связывает release с изменяемой вершиной ветки и не
доказывает, что развёрнут именно проверенный commit.

## Что делает GitHub Actions

`.github/workflows/ci.yml` запускается для push/PR. Job `Secretless release gate`:

- получает полную Git history без сохранения checkout credential;
- запускает закреплённый digest Gitleaks с redaction;
- устанавливает Python `3.11.15` и зависимости только из hash-locked файлов;
- выполняет `pip check`, Ruff, полный pytest и KB validation;
- проверяет production Compose с versioned placeholder-файлом;
- подтверждает, что checkout остался чистым.

Workflow имеет только `contents: read`, не ссылается на GitHub Secrets/Environments, не логинится
в registry, не подключается по SSH и ничего не deploy-ит. Проверка не заменяет server-side
secretless image build, SBOM/Trivy/model receipt и runtime acceptance из recovery runbook.

## Что GitHub Actions намеренно не делает

- не содержит HDE, Cloud.ru, VK, PostgreSQL, Redis, Qdrant, TLS или SSH credentials;
- не создаёт `.env.production`;
- не публикует Docker image;
- не имеет deploy key и доступа к серверу;
- не включает HDE dispatcher/webhook;
- не запускает запросы к реальной модели или HDE;
- не публикует private scanner reports и runtime logs.

CD через Actions рассматривается только отдельным архитектурным решением после recovery. Для
первого clean deployment это ненужное расширение trust boundary.

## Обязательные GitHub settings

Перед признанием первого Action-run доверенным владелец проверяет в GitHub UI:

1. `Settings → Actions → General → Actions permissions`:
   разрешены repository actions и только явно разрешённые GitHub-owned external actions.
2. `Require actions to be pinned to a full-length commit SHA` включён.
3. Fork pull request workflows отключены на время recovery. Их последующее включение требует
   отдельного решения; write token, secrets и variables для fork-PR при этом запрещены.
4. Default workflow permissions — read-only; создание/approval PR через Actions выключено.
5. `Settings → Secrets and variables → Actions`: для recovery release product secrets отсутствуют.
6. `Settings → Environments`: deployment environment и environment secrets не нужны.
7. `Settings → Deploy keys`: до clean host нового key нет; затем существует ровно один
   отдельный read-only key нового host, без `Allow write access`.
8. `Settings → Webhooks`: нет endpoint старого server и неизвестных hooks.
9. Account security log, PAT, SSH/GPG keys, OAuth/GitHub Apps и active sessions проверены
   владельцем отдельно; private evidence не коммитится.

На 20 июля 2026 в GitHub UI подтверждено и сохранено:

- Actions ограничены действиями владельца и GitHub; для внешних Actions обязателен полный SHA;
- default `GITHUB_TOKEN` read-only, создание/approval PR из Actions выключено;
- fork pull request workflows выключены;
- repository/environment Actions secrets и variables отсутствуют;
- environments, deploy keys и repository webhooks отсутствуют;
- dependency graph, Dependabot vulnerability alerts и malware alerts включены;
- автоматические dependency submission, security/version updates и Dependabot PR выключены.

Если тариф/тип репозитория поддерживает ruleset без конфликта с текущим solo workflow, после
первого зелёного run для `master` можно потребовать check `Secretless release gate`. До перехода
на обязательный PR-flow сервер всё равно принимает только вручную объявленный зелёный SHA.

## Локальная публикация

До commit:

```powershell
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.venv\Scripts\python.exe scripts\index_kb.py --validate-only
git status --short --branch
```

После review diff Codex создаёт обычный commit и выполняет `git push origin master`. Product
credentials при этом физически отсутствуют в workspace. Release SHA фиксируется только после
зелёного GitHub check:

```powershell
git rev-parse HEAD
git status --short --branch
```

## Ручное получение exact commit на существующем clean checkout

Команды выполняет оператор на новом server после настройки read-only deploy key. SHA вводится
только как публичный идентификатор commit:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
export TRUSTED_GIT_SHA='<GREEN_40_CHARACTER_COMMIT_SHA>'
printf '%s' "$TRUSTED_GIT_SHA" | grep -Eq '^[0-9a-f]{40}$'
test -z "$(git status --porcelain --untracked-files=normal)"
git fetch --prune origin master
git cat-file -e "${TRUSTED_GIT_SHA}^{commit}"
git checkout --detach "$TRUSTED_GIT_SHA"
test "$(git rev-parse HEAD)" = "$TRUSTED_GIT_SHA"
test -z "$(git status --porcelain --untracked-files=normal)"
git log -1 --format='commit=%H time=%cI subject=%s'
```

Первичный clone и создание deploy key выполняются строго по
`docs/recovery_test_production_runbook_20260720.md`. Public key оператор вставляет в GitHub сам;
private key не покидает server. Codex получает только безопасный fingerprint и результат
`clone/fetch/checkout passed`, без тела public/private key.

## Контракт передачи server evidence

Оператор возвращает Codex:

- deployed Git SHA и OCI revision;
- exit codes и названия прошедших/упавших gate;
- HTTP status, `/ready` без чувствительных полей и агрегированные datastore counts;
- обезличенный runtime acceptance report;
- sanitized error с удалёнными headers, payload, DSN, cookies, IP/private provider IDs и PII.

Нельзя возвращать `.env.production`, `env`, `docker inspect` environment, Authorization headers,
ключи, token suffix, shell history или сырые HDE tickets.
