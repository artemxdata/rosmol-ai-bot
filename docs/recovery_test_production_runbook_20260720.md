# Recovery test-production runbook — HDE/VK

Дата подготовки: 20 июля 2026 года. Фактическое окно clean deployment и UTC каждого gate
фиксируются в private evidence. Целевой результат — вернуть бота только в ограниченный тестовый
HDE/VK-контур на новом чистом сервере. HDE остаётся транспортом до тестового VK-канала; прямые
`/webhook/vk` и `/webhook/max` в этом релизе не используются.

Этот запуск не снимает общий security hold автоматически. Каждый gate ниже закрывается явным
evidence. Dispatcher включается последним и только для тестового департамента/канала.

## Жёсткие границы

- Старую VM не включать, не монтировать и не использовать как источник файлов.
- Не переносить старые `.env`, TLS/ACME state, SSH keys, Docker images/volumes/cache, PostgreSQL,
  Redis, Qdrant, model cache, runtime reports или backups.
- На новый сервер попадает только clean checkout trusted commit и versioned published seed.
- До preliminary acceptance не менять prompts, routing, thresholds, KB и HDE dispatcher payload.
- Yonote sync и Apply выключены. Новый Yonote token для этого запуска не нужен.
- Массовые проверки выполняются server-local через `/ask`, а не через HDE/VK.
- Значения секретов не записываются в runbook, Git, issue, чат, shell history или логи.
- GitHub Actions выполняет только secretless CI. Он не получает product secrets, SSH/deploy
  access, GitHub Environment и не выполняет deployment; оператор вручную выбирает зелёный SHA.

Эта граница относится к initial frozen recovery launch. После его отдельной acceptance ручной
read-only Yonote Preview может быть добавлен только как новый reviewed release/configuration
change по `docs/operations.md`; production Apply, live KB mutation и scheduler остаются запрещены.

## Пятичасовой critical path

| Окно | Работа | Gate |
|---|---|---|
| 00:00–00:40 | Provider/account cleanup и trusted Git audit | старые HDE rules выключены; неизвестные credentials/sessions удалены; trusted SHA зафиксирован |
| 00:00–02:00 | Локальный infrastructure/security patch и тесты | Ruff/pytest/KB/Compose/security regression green |
| 00:40–01:30 | Создание clean VM, SSH/firewall/Docker, server-bound identities | новый host fingerprint; только разрешённые ports; нет старых artifacts |
| 01:30–02:30 | Clean checkout, secretless pinned build, SBOM/CVE gate и ML artifact preparation | commit/image/dependency/model provenance совпадает; новых provider secrets на host ещё нет |
| 02:30–03:10 | Новые secrets, fresh data services, migration, Qdrant indexing и preliminary acceptance | migration head; `knowledge_base=2152`; `response_cache=0`; strict readiness |
| 03:10–03:40 | Воспроизведение feedback Наты и content verdict по «Машуку» | три кейса воспроизведены на новом runtime; verdict записан без смешивания cohort |
| 03:40–04:15 | Один согласованный regression-first correction cycle и полный server-local gate | точечный regression green; полный release gate green; auth/TLS/PII/dedupe/retry green |
| 04:15–04:45 | Новый HDE endpoint и три ручных smoke в тестовом канале | один inbound → одна trace → одна delivery; escalation реально видит оператор |
| 04:45–05:00 | Traffic/admin verification и handoff | RX/TX baseline принят; alerts работают; admin только HTTPS; dispatcher scope подтверждён |

Если любой security, provenance, delivery или grounding gate красный, dispatcher выключается и
окно завершается без live-трафика. Старый runtime не является rollback target.

Временная экономия не отменяет границу между preliminary acceptance и финальным handoff. Если
content verdict по «Машуку» или regression-first correction cycle не закрыты, чистый runtime можно
оставить поднятым для изолированных server-local проверок, но HDE dispatcher остаётся выключенным,
а новый sealed cohort не начинается.

## Gate 0 — до создания новых ключей

В реестре `docs/secret_rotation_20260716.md` обновляются только статусы и безопасные fingerprints,
никогда не значения credentials.

Обязательные подтверждения:

1. По подтверждению владельца два старых тестовых HDE-канала отключены и связанные keys удалены.
   Перед live smoke в HDE UI повторно подтверждено, что старый endpoint/rules остаются inactive.
2. Глобальный HDE API key по явному решению владельца не меняется из-за зависимых интеграций и
   записан как `retained_exception`, а не rotated/verified. До установки проверить provider
   usage/audit и минимальный scope; в runtime обязательны egress allowlist, rate/cost alerts,
   один test dispatcher и доступный kill switch. Dedicated bot API user/key остаётся будущим
   планом безопасной миграции.
3. В Selectel завершены account/MFA/session, API/application credential и project SSH key audit.
4. В GitHub проверены security log, PAT, account SSH keys, deploy keys, OAuth Apps, webhooks,
   Actions secrets/environments и активные sessions.
5. Проверены registry/GHCR/Docker Hub, DNS/registrar, monitoring, backup, SMTP и n8n. Для
   отсутствующих интеграций фиксируется `not_applicable`.
6. Старая VM остаётся `SHUTOFF`; provider-side forensic/billing ticket не закрывается удалением
   evidence без решения владельца инцидента.
7. Trusted commit проверен с доверенного локального устройства и записан полным Git object ID
   (40 hex для текущего SHA-1 repository format). Live remote, local checkout и будущий server
   checkout должны совпасть.
8. Exact commit имеет зелёный GitHub check `Secretless release gate`; workflow не использует
   Actions secrets и не deploy-ит. Контракт зафиксирован в `docs/github_release_workflow.md`.

## Gate 1 — классы новых identities

Server-bound identities создаются только на clean host:

- новый admin SSH key access и новые SSH host keys;
- отдельный read-only GitHub deploy key;
- новый TLS private key/certificate и новый ACME account state;
- новые PostgreSQL credentials и пустая база;
- новые независимые `API_AUTH_TOKEN`, `WEBHOOK_AUTH_TOKEN`, `ADMIN_AUTH_TOKEN`,
  `USER_HASH_SECRET` и `HDE_TRIGGER_PREFIX`;
- новый Cloud.ru key с минимальным scope;
- сохранённый shared HDE API credential только как явно принятый `retained_exception`; оператор
  переносит его из password manager непосредственно в server-only env без показа Codex;
- Redis/Qdrant credentials, если они включены production overlay; иначе только internal network
  и отсутствие опубликованных data ports фиксируются как временный residual risk.

`USER_HASH_SECRET` намеренно создаёт новую pseudonym/cohort epoch. Старые sessions и interrupted
cohort не восстанавливаются.

## Gate 2 — clean host

Минимальные требования к VM:

- актуальный vendor image и security updates;
- отдельный непривилегированный deploy/operator user;
- SSH key-only, password login и direct root login запрещены;
- provider firewall: `22/tcp` только с доверенных адресов, `80/tcp` только для ACME/redirect policy,
  `443/tcp` для нового endpoint; PostgreSQL/Redis/Qdrant наружу не открыты;
- time synchronization включена;
- Docker и Compose установлены из доверенного vendor repository;
- `/opt/rosmol-ai-bot` создан заново, без копирования старого `/opt`;
- секретный env-файл находится вне Git, принадлежит root/deploy group и имеет mode `0600`;
- provider alerts включены минимум для CPU, RAM, disk, network egress и резкого роста стоимости.

### Проверка и bootstrap Ubuntu 24.04

Команды ниже выполняются только на новой VM из чистого vendor image. Если Docker уже установлен
из официального repository, его не переустанавливают вслепую: сначала фиксируют версии и
источник пакетов. Convenience script `get.docker.com` для recovery production не используется.
Актуальная первичная инструкция: <https://docs.docker.com/engine/install/ubuntu/>.

```bash
set -Eeuo pipefail
test "$(. /etc/os-release && printf '%s' "$ID")" = ubuntu
. /etc/os-release
printf 'OS=%s VERSION=%s ARCH=%s\n' "$ID" "$VERSION_ID" "$(dpkg --print-architecture)"
timedatectl status
df -h /
free -h

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
sudo apt-get install -y ca-certificates curl git jq python3 ufw
test ! -e /var/run/reboot-required || {
  echo 'STOP: reboot the clean VM, reconnect, then continue'
  exit 1
}
```

Если `docker version` и `docker compose version` ещё не проходят, Docker ставится из его
подписанного apt repository:

```bash
set -Eeuo pipefail
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
dpkg-query -W docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Доступ к Docker socket эквивалентен root-доступу. Временный deploy/operator user добавляется в
группу `docker` только если это уже принятая модель администрирования; его SSH key, GitHub deploy
key и права не переиспользуются между назначениями. Rootless-переход не совмещается с этим
пятичасовым recovery окном без отдельного теста портов, volumes, systemd и Certbot.

До отключения password/root SSH обязательно открывается и проверяется второй key-only сеанс.
После этого evidence должно показывать:

```bash
set -Eeuo pipefail
sudo sshd -t
sudo sshd -T | grep -E \
  '^(passwordauthentication no|kbdinteractiveauthentication no|permitrootlogin no|pubkeyauthentication yes)$'
sudo ss -lntup
sudo ufw status verbose
```

Provider security group остаётся главным внешним барьером: `22/tcp` только из доверенного
CIDR/VPN, `80/tcp` и `443/tcp` для нового endpoint, остальные inbound закрыты. Docker-published
ports могут обходить правила UFW; поэтому внешний port scan и production Compose exposure test
обязательны, а PostgreSQL/Redis/Qdrant не публикуются вообще.

### Новый read-only Git deploy identity и trusted checkout

На clean host создаётся отдельный Ed25519 key без passphrase только для read-only deploy access.
В GitHub добавляется только public part; в журнал ротации — label и fingerprint. Перед первым
соединением fingerprint GitHub host key сверяется с официальной документацией GitHub с
доверенного устройства.

Public key в GitHub вставляет оператор. Ни public key body, ни private key, ни вывод `cat` не
возвращаются Codex; достаточно безопасного fingerprint и статуса `read-only clone passed`.

```bash
set -Eeuo pipefail
export DEPLOY_USER='<NEW_DEPLOY_USER>'
export TRUSTED_GIT_SHA='<40_LOWERCASE_HEX_SHA_FROM_TRUSTED_LOCAL_CHECKOUT>'
printf '%s' "$TRUSTED_GIT_SHA" | grep -Eq '^[0-9a-f]{40}$'

sudo install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "/home/${DEPLOY_USER}/.ssh"
sudo -u "$DEPLOY_USER" ssh-keygen -t ed25519 \
  -C 'rosmol-ai-bot-clean-readonly-deploy' \
  -f "/home/${DEPLOY_USER}/.ssh/rosmol_ai_bot_deploy" -N ''
sudo -u "$DEPLOY_USER" ssh-keygen -lf \
  "/home/${DEPLOY_USER}/.ssh/rosmol_ai_bot_deploy.pub"
sudo -u "$DEPLOY_USER" cat "/home/${DEPLOY_USER}/.ssh/rosmol_ai_bot_deploy.pub"
```

После добавления public key как read-only deploy key фиксируется официальный Ed25519 host key
GitHub из <https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints>
и создаётся отдельный alias:

```bash
set -Eeuo pipefail
sudo install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null \
  "/home/${DEPLOY_USER}/.ssh/known_hosts"
printf '%s\n' \
  'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' \
  | sudo -u "$DEPLOY_USER" tee "/home/${DEPLOY_USER}/.ssh/known_hosts" >/dev/null
sudo install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null \
  "/home/${DEPLOY_USER}/.ssh/config"
sudo -u "$DEPLOY_USER" tee "/home/${DEPLOY_USER}/.ssh/config" >/dev/null <<EOF
Host github.com-rosmol
  HostName github.com
  User git
  IdentityFile /home/${DEPLOY_USER}/.ssh/rosmol_ai_bot_deploy
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  UserKnownHostsFile /home/${DEPLOY_USER}/.ssh/known_hosts
EOF
sudo -u "$DEPLOY_USER" ssh-keygen -lf "/home/${DEPLOY_USER}/.ssh/known_hosts"
```

Выведенный Ed25519 fingerprint должен быть
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`. После этого репозиторий клонируется в пустой новый каталог,
а не поверх каких-либо старых файлов:

```bash
set -Eeuo pipefail
test ! -e /opt/rosmol-ai-bot
sudo install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/rosmol-ai-bot
sudo -u "$DEPLOY_USER" git clone \
  git@github.com-rosmol:artemxdata/rosmol-ai-bot.git /opt/rosmol-ai-bot
cd /opt/rosmol-ai-bot
sudo -u "$DEPLOY_USER" git checkout --detach "$TRUSTED_GIT_SHA"
test "$(sudo -u "$DEPLOY_USER" git rev-parse HEAD)" = "$TRUSTED_GIT_SHA"
test -z "$(sudo -u "$DEPLOY_USER" git status --porcelain --untracked-files=normal)"
sudo -u "$DEPLOY_USER" git log -1 --format='commit=%H time=%cI subject=%s'
```

Если каталог уже существует, команда намеренно останавливается: это не повод удалять или
переиспользовать неизвестное содержимое.

Для дальнейших обновлений запрещён плавающий `git pull`: оператор выполняет `git fetch`,
проверяет наличие вручную объявленного зелёного SHA и делает detached checkout по процедуре из
`docs/github_release_workflow.md`.

## Gate 3 — build и data plane

Команды выполняются только из detached trusted commit. Итоговый server handoff фиксирует:

- полный Git SHA;
- hashes lockfile/release manifest/KB seed/case manifests;
- Docker image IDs/digests;
- model repository revisions и локальные artifact checksums;
- migration head;
- Qdrant collection/version/count;
- время начала новой cohort epoch.

PostgreSQL, Redis и Qdrant создаются пустыми. Qdrant индексируется только из
`data/knowledge_base_seed.json` с `--prune-stale`; expected baseline — `2152` published points и
нулевой semantic response cache. Любое расхождение блокирует ingress.

### Secretless build gate — до выпуска provider credentials

Сначала собираются и проверяются application/ML images из clean detached commit. На host в этот
момент ещё не должно быть `.env.production`, Cloud.ru/HDE keys или TLS/ACME material. Build context
должен формироваться из tracked tree trusted commit; ignored локальные `.cache/`, `build/`, private
data и credential files не являются допустимым входом.

Обязательные результаты этого gate:

- все Python packages установлены только из versioned hash-lock с `--require-hashes`;
- base/data/proxy/certbot images закреплены digest, application images помечены полным Git SHA;
- OCI label `org.opencontainers.image.revision` совпадает с trusted commit;
- две BAAI-модели получены только по зафиксированным revisions, проверены и затем используются
  runtime в offline-режиме из read-only model volume;
- `pip check`, offline ML smoke, SBOM и Critical-CVE check зелёные;
- образ не содержит `.env*`, private data, SSH/TLS keys, backups, local caches или stale build tree.

Нельзя подменять закреплённые references свежими тегами «для ускорения». Secretless build и
подготовка моделей выполняются до создания `.env.production`. Обычный `sudo` удаляет
`RELEASE_GIT_SHA` из окружения, поэтому Compose запускается через изолированное окружение с
единственным явно переданным публичным SHA; иначе он молча применит development fallback из
40 нулей:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
printf '%s' "$TRUSTED_GIT_SHA" | grep -Eq '^[0-9a-f]{40}$'
test "$(git rev-parse HEAD)" = "$TRUSTED_GIT_SHA"
test -z "$(git status --porcelain --untracked-files=normal)"
test ! -e .env.production

build_dc=(sudo env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/root RELEASE_GIT_SHA="$TRUSTED_GIT_SHA" \
  docker compose -f docker-compose.yml -f docker-compose.ml.yml)
"${build_dc[@]}" --profile ml config --quiet
"${build_dc[@]}" --profile ml build --pull app app-ml
sudo docker image tag rosmol-ai-bot-app:dev "rosmol-ai-bot-app:${TRUSTED_GIT_SHA}"
sudo docker image tag rosmol-ai-bot-ml:dev "rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}"

for image in \
  "rosmol-ai-bot-app:${TRUSTED_GIT_SHA}" \
  "rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}"; do
  test "$(sudo docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
    = "$TRUSTED_GIT_SHA"
done

"${build_dc[@]}" --profile ml run --rm --no-deps --pull never ml-cache-init
"${build_dc[@]}" --profile ml run --rm --no-deps --pull never model-prefetch
"${build_dc[@]}" --profile ml run --rm --no-deps --pull never ml-check --load-models

sudo docker image inspect \
  "rosmol-ai-bot-app:${TRUSTED_GIT_SHA}" \
  "rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}" \
  --format '{{.RepoTags}} {{.Id}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Цикл до one-shot jobs fail-closed проверяет обе OCI revision, а диагностический вывод после них
должен показывать тот же `TRUSTED_GIT_SHA`. `ml-check` работает с
`network_mode: none`, читает модели из read-only volume и тем самым доказывает offline load.
Затем выполняется один закреплённый scanner workflow. Теги ниже дополнительно закреплены
linux/amd64 manifest digest; менять version/digest без отдельного review нельзя. Gitleaks читает
всю Git history и работает с `--redact`. Trivy сохраняет версию и timestamp vulnerability DB,
SBOM, Critical-CVE и image-secret reports в private каталог. Provider credentials на этом этапе
ещё отсутствуют:

GitHub dependency graph 20 июля открыл восемь alerts для прежнего `torch==2.6.0+cpu`: три moderate
и пять low. Recovery candidate обновлён до hash-locked `torch==2.13.0+cpu`; локально уже пройдены
image build, verified offline BGE-M3/BGE-reranker load, scanner gate и полный regression. На чистом
сервере до provider credentials этот verdict должен быть воспроизведён и сохранён в private
evidence как `upgraded_and_regressed`. Возврат к 2.6.0 или подмена этого доказательства простым
risk acceptance для текущего candidate запрещены и означают `STOP`.

```bash
set -Eeuo pipefail
GITLEAKS_IMAGE='zricethezav/gitleaks:v8.28.0@sha256:bf00b5e039f0fad4b32935dc5ec1e358f227ccd097bcb64b971f0331072fe2ae'
TRIVY_IMAGE='aquasec/trivy:0.64.1@sha256:de90a656e79b175a294abe85cb8b99670fab83ebf339cccd163e6f584846809a'
POSTGRES_IMAGE='postgres:16.14-alpine3.24@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777'
REDIS_IMAGE='redis:7.4.9-alpine@sha256:b1addbe72465a718643cff9e60a58e6df1841e29d6d7d60c9a85d8d72f08d1a7'
QDRANT_IMAGE='qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286'
NGINX_IMAGE='nginx:1.30.4-alpine@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
CERTBOT_IMAGE='certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4'
RUNTIME_EGRESS_PROXY_IMAGE='ubuntu/squid:6.6-24.04_edge@sha256:94f844158e12b52f51b4ae996515e37e8fb3e8d85e1c86caba1a297376e4ec4f'
EDGE_RELAY_IMAGE='haproxy:3.4.2-alpine@sha256:0878b11eb64c433be1b0f578a584b8aca12f6caaa64c8f239b8b556c0dd5eeeb'
APP_IMAGE="rosmol-ai-bot-app:${TRUSTED_GIT_SHA}"
ML_IMAGE="rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}"
SCAN_DIR="/var/lib/rosmol/security-scan/${TRUSTED_GIT_SHA}"
TRIVY_EXCEPTION_REVIEW_DEADLINE='2026-08-10'
[[ "$(date -u +%F)" < "$TRIVY_EXCEPTION_REVIEW_DEADLINE" ]] || {
  echo 'STOP: scoped Trivy exception expired; re-review before continuing'
  exit 1
}
sudo install -d -m 0700 "$SCAN_DIR"

production_images=(
  "$APP_IMAGE" "$ML_IMAGE" "$POSTGRES_IMAGE" "$REDIS_IMAGE" "$QDRANT_IMAGE"
  "$NGINX_IMAGE" "$CERTBOT_IMAGE" "$RUNTIME_EGRESS_PROXY_IMAGE" "$EDGE_RELAY_IMAGE"
)
pull_images=(
  "$GITLEAKS_IMAGE" "$TRIVY_IMAGE" "$POSTGRES_IMAGE" "$REDIS_IMAGE" "$QDRANT_IMAGE"
  "$NGINX_IMAGE" "$CERTBOT_IMAGE" "$RUNTIME_EGRESS_PROXY_IMAGE" "$EDGE_RELAY_IMAGE"
)
for image in "${pull_images[@]}"; do
  sudo docker pull --platform linux/amd64 "$image" >/dev/null
done

sudo docker run --rm --network none --platform linux/amd64 \
  -v "$PWD:/repo:ro" -v "$SCAN_DIR:/reports" "$GITLEAKS_IMAGE" \
  detect --source=/repo --redact --no-banner --exit-code=1 --log-opts=--all \
  --report-format=json --report-path=/reports/gitleaks.json
sudo docker run --rm --network none --platform linux/amd64 "$GITLEAKS_IMAGE" version \
  | sudo tee "$SCAN_DIR/gitleaks-version.txt" >/dev/null

sudo docker run --rm --platform linux/amd64 \
  -v rosmol_trivy_cache:/root/.cache/ "$TRIVY_IMAGE" image --download-db-only
sudo docker run --rm --network none --platform linux/amd64 \
  -v rosmol_trivy_cache:/root/.cache/ "$TRIVY_IMAGE" version --format json \
  | sudo tee "$SCAN_DIR/trivy-version-db.json" >/dev/null
sudo jq -e '.Version == "0.64.1" and .VulnerabilityDB.UpdatedAt != null' \
  "$SCAN_DIR/trivy-version-db.json" >/dev/null

for image in "${production_images[@]}"; do
  safe_name="$(printf '%s' "$image" | sed 's/[^A-Za-z0-9._-]/_/g')"
  ignore_args=()
  case "$image" in
    "$APP_IMAGE"|"$ML_IMAGE") ignore_args=(--ignorefile /repo/security/trivy-app-ignore.yaml --show-suppressed) ;;
    "$POSTGRES_IMAGE") ignore_args=(--ignorefile /repo/security/trivy-postgres-ignore.yaml --show-suppressed) ;;
    "$QDRANT_IMAGE") ignore_args=(--ignorefile /repo/security/trivy-qdrant-ignore.yaml --show-suppressed) ;;
  esac
  sudo docker run --rm --network none --platform linux/amd64 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v rosmol_trivy_cache:/root/.cache/ -v "$SCAN_DIR:/reports" "$TRIVY_IMAGE" \
    image --skip-db-update --format cyclonedx \
    --output "/reports/${safe_name}.cdx.json" "$image"
  sudo docker run --rm --network none --platform linux/amd64 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "$PWD:/repo:ro" -v rosmol_trivy_cache:/root/.cache/ \
    -v "$SCAN_DIR:/reports" "$TRIVY_IMAGE" \
    image --skip-db-update --scanners vuln --severity CRITICAL --exit-code 1 \
    --format json "${ignore_args[@]}" \
    --output "/reports/${safe_name}.critical.json" "$image"
  sudo docker run --rm --network none --platform linux/amd64 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v rosmol_trivy_cache:/root/.cache/ -v "$SCAN_DIR:/reports" "$TRIVY_IMAGE" \
    image --skip-db-update --scanners secret --exit-code 1 --format json \
    --output "/reports/${safe_name}.secrets.json" "$image"
done

sudo find "$SCAN_DIR" -maxdepth 1 -type f ! -name SHA256SUMS -print0 \
  | sort -z | sudo xargs -0 sha256sum | sudo tee "$SCAN_DIR/SHA256SUMS" >/dev/null
unset GITLEAKS_IMAGE TRIVY_IMAGE POSTGRES_IMAGE REDIS_IMAGE QDRANT_IMAGE NGINX_IMAGE
unset CERTBOT_IMAGE RUNTIME_EGRESS_PROXY_IMAGE EDGE_RELAY_IMAGE APP_IMAGE ML_IMAGE
unset SCAN_DIR TRIVY_EXCEPTION_REVIEW_DEADLINE safe_name image ignore_args production_images pull_images
```

Любая Gitleaks/image-secret finding, активная Critical CVE, истёкшее scoped Trivy-исключение, отсутствующий
vulnerability DB timestamp или отсутствующий PyTorch verdict блокирует выпуск provider
credentials. Ignore policy применяется только к exact PURL соответствующего app/PostgreSQL/Qdrant image;
Redis/Nginx/Certbot/Squid/HAProxy сканируются без исключений. Основание и срок обязательного re-review
зафиксированы в
`docs/security_scan_verdict_20260720.md`. Scanner JSON и SBOM не публикуются автоматически: они
могут содержать package paths и остаются private evidence.

22 июля refreshed DB обнаружила новый `CVE-2026-57433` и штатно остановила первый clean-host scan.
После проверки upstream patch и package contents исключение добавлено только для exact
`perl-base=5.40.1-6` PURL app/app-ml и Qdrant: уязвимый модуль Storable в этих runtime images
отсутствует, а entrypoints не используют Perl. Это решение не разрешает продолжить старый
прерванный scan: нужен новый Git SHA, новые SHA-bound app/app-ml images и fresh `SCAN_DIR`; срок
повторного review после fail-closed перепроверки 27 июля установлен на `2026-08-10`.

Следующий fresh scan SHA `15f3c2aae3891a7e064a48f1fff3f21c1956296d` также штатно остановился до
credentials на `CVE-2026-59873` (`pkg:npm/tar@7.5.16`) в pinned Qdrant digest. Exact-image
inspection показал, что запись существует только в embedded
`/qdrant/static/qdrant-web-ui.spdx.json`: `node`, `npm`, `npx` и файлы `node_modules/tar`
отсутствуют, а entrypoint запускает Rust Qdrant. Уязвимый parse/extract path недостижим, поэтому
короткоживущее исключение применяется только к этому PURL и digest до `2026-08-10`. Partial scan
не продолжается: после policy patch снова обязательны новый trusted SHA, новые SHA-bound app/ML
images и fresh полный scan всех девяти images.

### Новый production env без вывода значений

Локальные application/database secrets генерируются только на clean host. Скрипт создаёт файл
атомарно с mode `0600`, не печатает значения и отказывается перезаписывать существующий файл:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
umask 077
sudo -u "$DEPLOY_USER" python3 scripts/generate_production_env.py init \
  --template .env.production.example --output .env.production
```

Затем оператор в защищённом редакторе заполняет provider-side значения: новый Cloud.ru key,
сохранённый по принятому исключению HDE key, новый DNS host и ACME email. Значения переносятся из
password manager непосредственно в server-only файл и не показываются Codex. Никакое значение не
вставляется в командную строку, чат или Git. В `RELEASE_GIT_SHA` записывается тот же
`TRUSTED_GIT_SHA`. До запуска data plane обязательна проверка:

```bash
set -Eeuo pipefail
sudo -u "$DEPLOY_USER" python3 scripts/generate_production_env.py validate .env.production
test ! -L .env.production
test "$(stat -c '%a' .env.production)" = 600
sudo -u "$DEPLOY_USER" git check-ignore -q .env.production
sudo -u "$DEPLOY_USER" python3 scripts/generate_production_env.py \
  render-egress-proxy --env-file .env.production \
  --output data/private/runtime-egress/squid.conf
test -f data/private/runtime-egress/squid.conf
test ! -L data/private/runtime-egress/squid.conf
test "$(stat -c '%a' data/private/runtime-egress/squid.conf)" = 600
sudo -u "$DEPLOY_USER" git check-ignore -q data/private/runtime-egress/squid.conf
test -z "$(sudo -u "$DEPLOY_USER" git status --porcelain --untracked-files=normal)"
```

Generator принимает только точный reviewed Cloud.ru endpoint, точный tenant
`rosmolodezh.helpdeskeddy.com` и — только при отдельно включённом ручном Preview — точный
`rossmol.yonote.ru`; cross-provider substitution, IP literal, другой порт, query, fragment или
credentials в URL блокируют запуск. Сгенерированный Squid config содержит два hostname при
выключенном Yonote и ровно три при включённом Preview, не содержит API keys и не перезаписывается
неявно. До первого `up` его разбирает тот же закреплённый image, который будет работать в runtime:

```bash
set -Eeuo pipefail
RUNTIME_EGRESS_PROXY_IMAGE='ubuntu/squid:6.6-24.04_edge@sha256:94f844158e12b52f51b4ae996515e37e8fb3e8d85e1c86caba1a297376e4ec4f'
EDGE_RELAY_IMAGE='haproxy:3.4.2-alpine@sha256:0878b11eb64c433be1b0f578a584b8aca12f6caaa64c8f239b8b556c0dd5eeeb'
sudo docker run --rm --platform linux/amd64 --network none \
  --entrypoint /usr/sbin/squid \
  -v "$PWD/data/private/runtime-egress/squid.conf:/etc/squid/squid.conf:ro" \
  "$RUNTIME_EGRESS_PROXY_IMAGE" -k parse -f /etc/squid/squid.conf
sudo docker run --rm --platform linux/amd64 --network none \
  --entrypoint haproxy \
  -v "$PWD/haproxy/edge-relay.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" \
  "$EDGE_RELAY_IMAGE" -c -f /usr/local/etc/haproxy/haproxy.cfg
unset RUNTIME_EGRESS_PROXY_IMAGE EDGE_RELAY_IMAGE
```

### Сборка и запуск data plane

Во всех следующих командах используется один и тот же Compose stack:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
dc=(sudo docker compose --env-file .env.production \
  -f docker-compose.yml \
  -f docker-compose.ml.yml \
  -f docker-compose.prod.yml)

"${dc[@]}" --profile ml config --quiet
"${dc[@]}" --profile ml up -d --no-build postgres redis qdrant runtime-egress-proxy
"${dc[@]}" --profile ml run --rm --pull never migrate
"${dc[@]}" --profile ml run --rm --pull never -T migrate python - <<'PY'
import asyncio
import os

import asyncpg

from src.channels.hde_transport import (
    FAIL_INBOX_SQL,
    FAIL_OUTBOX_SQL,
    RECOVER_STALE_INBOX_SQL,
    RECOVER_STALE_OUTBOX_SQL,
)


async def main() -> None:
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        for query in (
            RECOVER_STALE_INBOX_SQL,
            RECOVER_STALE_OUTBOX_SQL,
            FAIL_INBOX_SQL,
            FAIL_OUTBOX_SQL,
        ):
            await connection.prepare(query)
    finally:
        await connection.close()


asyncio.run(main())
print("hde_transport_sql_prepare=passed")
PY
"${dc[@]}" --profile ml run --rm --pull never index-kb sh -c \
  'python scripts/init_qdrant.py && python scripts/index_kb.py \
   --path data/knowledge_base_seed.json --prune-stale'
RUNTIME_STARTED_AT_FILE="data/private/runtime/runtime-started-at-${TRUSTED_GIT_SHA}.txt"
test ! -e "$RUNTIME_STARTED_AT_FILE"
umask 077
date -u +%Y-%m-%dT%H:%M:%SZ > "$RUNTIME_STARTED_AT_FILE"
"${dc[@]}" --profile ml up -d --no-build --wait --wait-timeout 480 app app-ml
"${dc[@]}" --profile ml ps
app_ml_id="$("${dc[@]}" --profile ml ps -q app-ml)"
test -n "$app_ml_id"
sudo docker inspect "$app_ml_id" --format '{{json .HostConfig.PortBindings}}' \
  | jq -e 'length == 0' >/dev/null
test -z "$(sudo docker port "$app_ml_id" 2>/dev/null)"
test "$(ss -H -ltn 'sport = :8001' | wc -l | tr -d ' ')" = 0
ready_json="$("${dc[@]}" --profile ml exec -T app-ml python - <<'PY'
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=20) as response:
    print(response.read().decode("utf-8"))
PY
)"
jq -e --arg sha "$TRUSTED_GIT_SHA" '
  .status == "ready" and
  .release_git_sha == $sha and
  .hde_transport_counts.inbox_backlog == 0 and
  .hde_transport_counts.inbox_processing == 0 and
  .hde_transport_counts.inbox_dead_letter == 0 and
  .hde_transport_counts.outbox_backlog == 0 and
  .hde_transport_counts.outbox_sending == 0 and
  .hde_transport_counts.outbox_dead_letter == 0
' <<<"$ready_json" >/dev/null

collection_counts="$("${dc[@]}" --profile ml exec -T app-ml python - <<'PY'
import asyncio
import json

from qdrant_client import AsyncQdrantClient

from src.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=10,
    )
    try:
        knowledge = await client.get_collection(settings.qdrant_knowledge_collection)
        response_cache = await client.get_collection("response_cache")
        result = {
            "knowledge_base": int(knowledge.points_count or 0),
            "response_cache": int(response_cache.points_count or 0),
        }
        print(json.dumps(result, sort_keys=True))
    finally:
        await client.close()


asyncio.run(main())
PY
)"
jq -e '.knowledge_base == 2152 and .response_cache == 0' \
  <<<"$collection_counts" >/dev/null
unset app_ml_id ready_json collection_counts
```

До dispatcher отдельно доказывается фактическая network policy. Проверка не передаёт provider
credentials: она отправляет только HTTP `CONNECT` к host из уже проверенных URL. Разрешённые host
должны вернуть proxy status `200`, посторонний host — `403`, а прямой TCP из `app-ml` к
публичному адресу и metadata endpoint обязан завершиться ошибкой:

```bash
set -Eeuo pipefail
"${dc[@]}" --profile ml exec -T app-ml python - <<'PY'
import os
import socket
from urllib.parse import urlsplit

proxy = ("runtime-egress-proxy", 3128)
allowed = {
    urlsplit(os.environ["CLOUD_RU_CHAT_COMPLETIONS_URL"]).hostname,
    urlsplit(os.environ["HDE_BASE_URL"]).hostname,
}
if os.environ.get("YONOTE_SYNC_ENABLED", "").strip().casefold() == "true":
    yonote_host = urlsplit(os.environ["YONOTE_BASE_URL"]).hostname
    assert yonote_host == "rossmol.yonote.ru"
    allowed.add(yonote_host)

def connect_status(host: str) -> int:
    with socket.create_connection(proxy, timeout=8) as stream:
        request = f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n"
        stream.sendall(request.encode("ascii"))
        status_line = stream.recv(4096).split(b"\r\n", 1)[0]
    return int(status_line.split()[1])

expected = {
    "foundation-models.api.cloud.ru",
    "rosmolodezh.helpdeskeddy.com",
}
if os.environ.get("YONOTE_SYNC_ENABLED", "").strip().casefold() == "true":
    expected.add("rossmol.yonote.ru")
assert allowed == expected
assert all(connect_status(host) == 200 for host in allowed)
assert connect_status("example.com") == 403
for target in (("1.1.1.1", 443), ("169.254.169.254", 80)):
    try:
        socket.create_connection(target, timeout=3).close()
    except OSError:
        continue
    raise SystemExit(f"direct egress unexpectedly reachable: {target[1]}")
print("runtime_https_egress_gate=passed")
PY

app_ml_id="$("${dc[@]}" --profile ml ps -q app-ml)"
proxy_id="$("${dc[@]}" --profile ml ps -q runtime-egress-proxy)"
test -n "$app_ml_id" && test -n "$proxy_id"
sudo docker inspect "$app_ml_id" --format '{{json .NetworkSettings.Networks}}' | jq -e '
  (keys | length) == 3 and
  any(keys[]; endswith("_data")) and
  any(keys[]; endswith("_edge")) and
  any(keys[]; endswith("_runtime_egress")) and
  (has("rosmol-ai-bot_egress") | not)
' >/dev/null
sudo docker inspect "$proxy_id" --format '{{json .NetworkSettings.Networks}}' | jq -e '
  (keys | length) == 2 and
  any(keys[]; endswith("_runtime_egress")) and
  any(keys[]; endswith("_egress"))
' >/dev/null
unset app_ml_id proxy_id
```

`model-prefetch` в production overlay имеет `network_mode: none`: сетевое получение моделей
разрешено только в предыдущем secretless gate, а после появления ключей любой отсутствующий model
artifact приводит к fail-closed, а не к неявной загрузке.

До публикации Nginx фиксируются image IDs/digests и фактические версии зависимостей. Значения
артефактов безопасны для handoff; содержимое env не выводится:

```bash
set -Eeuo pipefail
sudo docker image inspect \
  "rosmol-ai-bot-app:${TRUSTED_GIT_SHA}" \
  "rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}" \
  --format '{{.RepoTags}} {{.Id}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
"${dc[@]}" --profile ml exec -T app python -m pip check
"${dc[@]}" --profile ml exec -T app-ml python -m pip check
test "$("${dc[@]}" --profile ml exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
   "select version_num from alembic_version"' | tr -d '\r')" = 008_hde_durable_transport
```

Nginx сначала поднимается только в HTTP bootstrap policy: ACME и `/health` доступны, а `/ask`,
`/webhook/*`, `/ready` и admin возвращают `426`.

```bash
set -Eeuo pipefail
ADMIN_PUBLIC_HOST="$(python3 -c '
from pathlib import Path
for line in Path(".env.production").read_text(encoding="utf-8").splitlines():
    if line.startswith("ADMIN_PUBLIC_HOST="):
        print(line.split("=", 1)[1].strip())
        break
')"
EXPECTED_PUBLIC_IPV4='<NEW_SERVER_PUBLIC_IPV4>'
python3 - "$ADMIN_PUBLIC_HOST" "$EXPECTED_PUBLIC_IPV4" <<'PY'
import ipaddress
import socket
import sys

host, expected_text = sys.argv[1:]
if not host or expected_text.startswith("<"):
    raise SystemExit("set the reviewed DNS host and new server IPv4 before ACME")
try:
    ipaddress.ip_address(host)
except ValueError:
    pass
else:
    raise SystemExit("ADMIN_PUBLIC_HOST must be a DNS name, not an IP literal")
expected = ipaddress.ip_address(expected_text)
if expected.version != 4:
    raise SystemExit("expected server address must be IPv4")
resolved = {
    ipaddress.ip_address(item[4][0].split("%", maxsplit=1)[0])
    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
}
if resolved != {expected}:
    raise SystemExit(
        "ADMIN_PUBLIC_HOST IP records must exactly match the reviewed new server IPv4"
    )
print("public_dns_preflight=PASS")
PY
"${dc[@]}" --profile ml up -d --no-build nginx edge-relay
test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/ask)" = 426
test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/webhook/hde)" = 426

nginx_id="$("${dc[@]}" --profile ml ps -q nginx)"
relay_id="$("${dc[@]}" --profile ml ps -q edge-relay)"
test -n "$nginx_id" && test -n "$relay_id"
sudo docker inspect "$nginx_id" --format '{{json .NetworkSettings.Networks}}' | jq -e '
  (keys | length) == 1 and any(keys[]; endswith("_edge"))
' >/dev/null
sudo docker inspect "$nginx_id" --format '{{json .HostConfig.PortBindings}}' \
  | jq -e 'length == 0' >/dev/null
sudo docker inspect "$relay_id" --format '{{json .NetworkSettings.Networks}}' | jq -e '
  (keys | length) == 2 and
  any(keys[]; endswith("_ingress")) and
  any(keys[]; endswith("_edge"))
' >/dev/null
sudo docker inspect "$relay_id" --format '{{json .HostConfig.PortBindings}}' | jq -e '
  (keys | sort) == ["8080/tcp", "8443/tcp"]
' >/dev/null
if "${dc[@]}" --profile ml exec -T nginx wget -q -T 3 -O /dev/null http://1.1.1.1; then
  echo 'STOP: Nginx direct public egress is reachable'
  exit 1
fi
if "${dc[@]}" --profile ml exec -T nginx wget -q -T 3 -O /dev/null http://169.254.169.254; then
  echo 'STOP: Nginx metadata egress is reachable'
  exit 1
fi
unset nginx_id relay_id
sudo env -u ADMIN_PUBLIC_HOST -u CERTBOT_EMAIL bash scripts/provision_admin_https.sh
unset ADMIN_PUBLIC_HOST EXPECTED_PUBLIC_IPV4
```

Пустые shell overrides выше заставляют provisioning script прочитать host/email из защищённого
`.env.production`; они не являются значениями credentials. После выпуска сертификата:

```bash
set -Eeuo pipefail
ADMIN_PUBLIC_HOST="$(python3 -c '
from pathlib import Path
for line in Path(".env.production").read_text(encoding="utf-8").splitlines():
    if line.startswith("ADMIN_PUBLIC_HOST="):
        print(line.split("=", 1)[1].strip())
        break
')"
test -n "$ADMIN_PUBLIC_HOST"
curl -fsS --max-time 20 "https://${ADMIN_PUBLIC_HOST}/ready" | jq .
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' \
  --data '{"user_id":"auth-negative","text":"security-auth-probe"}' \
  "https://${ADMIN_PUBLIC_HOST}/ask")" = 401
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  "https://${ADMIN_PUBLIC_HOST}/docs")" = 404
unset ADMIN_PUBLIC_HOST
```

Shell tracing (`set -x`) во время всех операций с env запрещён.

## Gate 4 — security и runtime acceptance

Этот gate намеренно разделён на доказательство code invariants и безопасные live-пробы. Offline
regression не выдаётся за проверку развернутого runtime, а live-проба не создаёт provider side
effect ради тестирования ошибок доставки.

### Gate 4A — offline security/transport regression

На clean checkout точного `TRUSTED_GIT_SHA` выполняется закреплённый набор. Он доказывает auth
guards, production config fail-closed, admin cookie/logout, PII masking и принудительный отказ,
stable-event dedupe, порядок одного ticket, durable transaction/recovery, retry/429 и
ambiguous-delivery dead-letter policy. Это доказательство семантики кода, но не HDE delivery:

```powershell
$trustedGitSha = '<40_LOWERCASE_HEX_SHA>'
if ((git rev-parse HEAD) -ne $trustedGitSha) { throw 'trusted SHA mismatch' }
if (git status --porcelain --untracked-files=normal) { throw 'worktree is dirty' }
New-Item -ItemType Directory -Force data/private/runtime | Out-Null
.venv\Scripts\python.exe -m pytest -q `
  tests/test_endpoint_security.py `
  tests/test_admin_kb_api.py `
  tests/test_pii_masker.py `
  tests/test_user_hashing.py `
  tests/test_health_ready.py `
  tests/test_hde_webhook.py `
  tests/test_hde_transport.py `
  tests/test_hde_worker.py `
  tests/test_hde_adapter.py `
  tests/test_db_logger.py `
  --junitxml="data/private/runtime/gate4-offline-$trustedGitSha.xml"
if ($LASTEXITCODE -ne 0) { throw 'Gate 4A failed' }
if (-not (Test-Path "data/private/runtime/gate4-offline-$trustedGitSha.xml")) {
  throw 'Gate 4A evidence is missing'
}
```

Gate 4A выполняется в clean local/CI checkout до deploy и не повторяется production image.
Локальную `.venv` нельзя переносить на server, заменять этот gate тестами внутри runtime image
или устанавливать dev dependencies в production image. Server-local quality gate ниже запускает
только release eval cases и trace verification.

### Gate 4B — safe live runtime probes

На новой VM после TLS/data-plane acceptance, но при выключенном dispatcher, versioned script
проверяет фактический runtime:

- `/health` и строгий `/ready` с точным SHA, config/DB/KB/ML/PII-prewarm/transport status;
- пустую HDE inbox/outbox/dead-letter очередь до и после проб;
- `401` для отсутствующих и неверных `/ask`, HDE и admin credentials;
- `426` для sensitive plaintext HTTP, `404` для docs и прямых VK/MAX webhook;
- HTTPS admin login, `Secure`/`HttpOnly`/`SameSite=Lax`, logout и invalidation cookie;
- отсутствие уникального probe user id, значений всех credentials и secret header names в
  свежих `app-ml`/Nginx/edge-relay container logs.

Скрипт читает secrets непосредственно из mode `0600` `.env.production`, не экспортирует и не
печатает их. Он **не отправляет корректно авторизованный `/ask`**, не отправляет корректно
авторизованный HDE event и не вызывает provider delivery. Поэтому dedupe/retry/dead-letter
остаются Gate 4A code invariants до ограниченного end-to-end smoke в Gate 5.

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
sudo -v
RUNTIME_SECURITY_REPORT="data/private/runtime/runtime-security-${TRUSTED_GIT_SHA}.json"
test ! -e "$RUNTIME_SECURITY_REPORT"
RUNTIME_STARTED_AT_FILE="data/private/runtime/runtime-started-at-${TRUSTED_GIT_SHA}.txt"
test -f "$RUNTIME_STARTED_AT_FILE"
test ! -L "$RUNTIME_STARTED_AT_FILE"
test "$(stat -c '%a' "$RUNTIME_STARTED_AT_FILE")" = 600
RUNTIME_STARTED_AT="$(cat "$RUNTIME_STARTED_AT_FILE")"
EXPECTED_PUBLIC_IPV4='<NEW_SERVER_PUBLIC_IPV4>'
ADMIN_PUBLIC_HOST="$(python3 -c '
from pathlib import Path
for line in Path(".env.production").read_text(encoding="utf-8").splitlines():
    if line.startswith("ADMIN_PUBLIC_HOST="):
        print(line.split("=", 1)[1].strip())
        break
')"
test -n "$ADMIN_PUBLIC_HOST"
RUNTIME_BASE_URL="https://${ADMIN_PUBLIC_HOST}"
python3 scripts/run_runtime_security_acceptance.py \
  --env-file .env.production \
  --expected-git-sha "$TRUSTED_GIT_SHA" \
  --expected-public-ipv4 "$EXPECTED_PUBLIC_IPV4" \
  --runtime-base-url "$RUNTIME_BASE_URL" \
  --log-since-utc "$RUNTIME_STARTED_AT" \
  --log-container rosmol-app-ml \
  --log-container rosmol-nginx \
  --log-container rosmol-edge-relay \
  --use-sudo-docker \
  --output "$RUNTIME_SECURITY_REPORT"
jq -e --arg sha "$TRUSTED_GIT_SHA" --arg since "$RUNTIME_STARTED_AT" \
  '.passed == true and .expected_git_sha == $sha and
   .log_scan_since_utc == $since and
   ([.checks[] | select(.passed != true)] | length) == 0' \
  "$RUNTIME_SECURITY_REPORT" >/dev/null
test "$(stat -c '%a' "$RUNTIME_SECURITY_REPORT")" = 600
git check-ignore -q "$RUNTIME_SECURITY_REPORT"
unset ADMIN_PUBLIC_HOST EXPECTED_PUBLIC_IPV4 RUNTIME_BASE_URL RUNTIME_SECURITY_REPORT \
  RUNTIME_STARTED_AT RUNTIME_STARTED_AT_FILE
```

Любой skipped/недоступный log scan, неожиданный status, непустая очередь, SHA mismatch или
утечка marker/credential делает report красным и сохраняет dispatcher в состоянии `OFF`.

### Финальный acceptance, привязанный к commit

Полный quality suite требует прямого trace lookup в PostgreSQL. Поэтому production credentials не
переносятся на локальную машину и database host port не открывается. Eval запускается на сервере
одноразовым `quality-acceptance` контейнером: он получает только `API_AUTH_TOKEN` и внутренний
`POSTGRES_DSN`, подключён только к internal `data`, не имеет provider secrets/egress/host ports,
читает read-only `git archive` snapshot exact commit без `.git`/`.env` и удаляется после выполнения.
API-запросы идут прямо к `app-ml` внутри Docker; TLS/Nginx policy независимо доказывается Gate 4B.
Минимальный runtime image не обязан содержать `git`: host сначала создаёт из clean checkout
отдельную direct-Git аттестацию, а one-shot повторно сверяет её SHA и все KB/case fingerprints с
файлами неизменяемого snapshot.

До команды `HIGH_COST_APPROVAL_ID` экспортируется из внешней одноразовой owner-записи для точного
runtime SHA, набора, прогноза и расчётного stop-limit. ID не является секретом, но придумывать его на сервере
или копировать из примера запрещено; отсутствие реальной записи означает `STOP`.

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
test "$(git rev-parse HEAD)" = "$TRUSTED_GIT_SHA"
test -z "$(git status --porcelain --untracked-files=normal)"
CLEAN_ACCEPTANCE_SOURCE="/var/lib/rosmol/release-source/${TRUSTED_GIT_SHA}"
test "$(sudo -u "$DEPLOY_USER" git -C "$CLEAN_ACCEPTANCE_SOURCE" rev-parse HEAD)" \
  = "$TRUSTED_GIT_SHA"
test -z "$(sudo -u "$DEPLOY_USER" git -C "$CLEAN_ACCEPTANCE_SOURCE" \
  status --porcelain --untracked-files=normal)"
test ! -e "$CLEAN_ACCEPTANCE_SOURCE/.env"
test ! -e "$CLEAN_ACCEPTANCE_SOURCE/.env.production"

ACCEPTANCE_RUN_ID="quality-${TRUSTED_GIT_SHA}-$(date -u +%Y%m%dT%H%M%SZ)"
: "${HIGH_COST_APPROVAL_ID:?STOP: set the one-time owner approval reference}"
QUALITY_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
QUALITY_EVIDENCE_DIR="/var/lib/rosmol/acceptance/${ACCEPTANCE_RUN_ID}"
QUALITY_ATTESTATION_DIR="/var/lib/rosmol/acceptance-attestations/${ACCEPTANCE_RUN_ID}"
QUALITY_ATTESTATION_FILE="$QUALITY_ATTESTATION_DIR/source-provenance.json"
ACCEPTANCE_SOURCE_SNAPSHOT="/var/lib/rosmol/acceptance-source/${ACCEPTANCE_RUN_ID}"
ACCEPTANCE_COST_LEDGER_DIR="/var/lib/rosmol/eval-cost-ledger-v1"
test ! -e "$QUALITY_EVIDENCE_DIR"
test ! -e "$QUALITY_ATTESTATION_DIR"
test ! -e "$ACCEPTANCE_SOURCE_SNAPSHOT"
sudo install -d -m 0700 -o 10001 -g 10001 "$QUALITY_EVIDENCE_DIR"
sudo install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$QUALITY_ATTESTATION_DIR"
sudo install -d -m 0700 -o 10001 -g 10001 "$ACCEPTANCE_COST_LEDGER_DIR"
test ! -L "$ACCEPTANCE_COST_LEDGER_DIR"
sudo -u "$DEPLOY_USER" env \
  CLEAN_ACCEPTANCE_SOURCE="$CLEAN_ACCEPTANCE_SOURCE" \
  QUALITY_ATTESTATION_FILE="$QUALITY_ATTESTATION_FILE" \
  ACCEPTANCE_RUN_ID="$ACCEPTANCE_RUN_ID" \
  TRUSTED_GIT_SHA="$TRUSTED_GIT_SHA" \
  python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

source_root = Path(os.environ["CLEAN_ACCEPTANCE_SOURCE"]).resolve()
sys.path.insert(0, str(source_root))
os.chdir(source_root)

from eval.release_provenance import build_release_provenance

case_paths = {
    "yonote": Path("eval/cases/pre_pilot_yonote.json"),
    "forums": Path("eval/cases/pre_pilot_forums.json"),
    "safety": Path("eval/cases/pre_pilot_safety.json"),
    "off_topic": Path("eval/cases/pre_pilot_off_topic.json"),
    "pii": Path("eval/cases/pre_pilot_pii.json"),
    "adversarial": Path("eval/cases/pre_pilot_adversarial.json"),
    "followup": Path("eval/cases/pre_pilot_followup.json"),
}
payload = build_release_provenance(
    release_run_id=os.environ["ACCEPTANCE_RUN_ID"],
    target="http://app-ml:8000/ask",
    kb_seed_path=Path("data/knowledge_base_seed.json"),
    case_paths=case_paths,
    expected_git_sha=os.environ["TRUSTED_GIT_SHA"],
)
if payload.get("complete") is not True:
    raise SystemExit("clean source provenance is incomplete")
path = Path(os.environ["QUALITY_ATTESTATION_FILE"])
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
PY
sudo jq -e --arg sha "$TRUSTED_GIT_SHA" --arg run "$ACCEPTANCE_RUN_ID" '
  .complete == true and
  .verification_mode == "direct_git" and
  .release_run_id == $run and
  .git_sha == $sha and
  .expected_git_sha == $sha and
  .git_worktree_clean == true and
  (.case_files | keys) ==
    ["adversarial", "followup", "forums", "off_topic", "pii", "safety", "yonote"]
' "$QUALITY_ATTESTATION_FILE" >/dev/null
sudo chown root:root "$QUALITY_ATTESTATION_DIR" "$QUALITY_ATTESTATION_FILE"
sudo chmod 0711 "$QUALITY_ATTESTATION_DIR"
sudo chmod 0444 "$QUALITY_ATTESTATION_FILE"
if sudo -u "$DEPLOY_USER" git -C "$CLEAN_ACCEPTANCE_SOURCE" ls-files -s \
  | awk '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }'; then
  echo 'STOP: symlink is not allowed in the acceptance source snapshot'
  exit 1
fi
sudo install -d -m 0550 -o 10001 -g 10001 "$ACCEPTANCE_SOURCE_SNAPSHOT"
sudo -u "$DEPLOY_USER" git -C "$CLEAN_ACCEPTANCE_SOURCE" \
  archive --format=tar "$TRUSTED_GIT_SHA" \
  | sudo tar -xf - -C "$ACCEPTANCE_SOURCE_SNAPSHOT" --no-same-owner
sudo chown -R 10001:10001 "$ACCEPTANCE_SOURCE_SNAPSHOT"
sudo find "$ACCEPTANCE_SOURCE_SNAPSHOT" -type d -exec chmod 0550 {} +
sudo find "$ACCEPTANCE_SOURCE_SNAPSHOT" -type f -exec chmod 0440 {} +
test ! -e "$ACCEPTANCE_SOURCE_SNAPSHOT/.env"
test ! -e "$ACCEPTANCE_SOURCE_SNAPSHOT/.env.production"
acceptance_dc=(sudo env \
  ACCEPTANCE_SOURCE_DIR="$ACCEPTANCE_SOURCE_SNAPSHOT" \
  ACCEPTANCE_OUTPUT_DIR="$QUALITY_EVIDENCE_DIR" \
  ACCEPTANCE_PROVENANCE_DIR="$QUALITY_ATTESTATION_DIR" \
  ACCEPTANCE_COST_LEDGER_DIR="$ACCEPTANCE_COST_LEDGER_DIR" \
  docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.ml.yml -f docker-compose.prod.yml \
  -f docker-compose.acceptance.yml)
"${acceptance_dc[@]}" --profile ml --profile acceptance config --quiet
"${acceptance_dc[@]}" --profile ml --profile acceptance run \
  --rm --no-deps --pull never quality-acceptance --help >/dev/null
echo 'quality_acceptance_import_preflight=PASS'
"${acceptance_dc[@]}" --profile ml --profile acceptance run \
  --rm --no-deps --pull never quality-acceptance \
  --output-dir /evidence \
  --target http://app-ml:8000/ask \
  --max-llm-cost-rub 80 \
  --high-cost-approval-id "$HIGH_COST_APPROVAL_ID" \
  --release-run-id "$ACCEPTANCE_RUN_ID" \
  --expected-git-sha "$TRUSTED_GIT_SHA" \
  --provenance-file /provenance/source-provenance.json \
  --sections yonote,forums,safety,off_topic,pii,adversarial,followup \
  --summary-only

sudo jq -e --arg sha "$TRUSTED_GIT_SHA" --arg run "$ACCEPTANCE_RUN_ID" '
  .passed == true and
  .release_run_id == $run and
  .expected_git_sha == $sha and
  .target == "http://app-ml:8000/ask" and
  .provenance.complete == true and
  .provenance.verification_mode ==
    "host_git_attestation_with_local_hash_verification" and
  .provenance.git_sha == $sha and
  .provenance.git_worktree_clean == true and
  .requested_sections ==
    ["yonote", "forums", "safety", "off_topic", "pii", "adversarial", "followup"] and
  .completed_sections ==
    ["yonote", "forums", "safety", "off_topic", "pii", "adversarial", "followup"] and
  (.sections | keys) ==
    ["adversarial", "followup", "forums", "off_topic", "pii", "safety", "yonote"] and
  ([.sections[] | .trace_coverage_rate] | all(. == 1))
' "$QUALITY_EVIDENCE_DIR/summary.json" >/dev/null
test -z "$("${acceptance_dc[@]}" --profile ml --profile acceptance \
  ps -q quality-acceptance)"
test "$(sudo -u "$DEPLOY_USER" git -C "$CLEAN_ACCEPTANCE_SOURCE" rev-parse HEAD)" \
  = "$TRUSTED_GIT_SHA"
test -z "$(sudo -u "$DEPLOY_USER" git -C "$CLEAN_ACCEPTANCE_SOURCE" \
  status --porcelain --untracked-files=normal)"
test -z "$(sudo find "$ACCEPTANCE_SOURCE_SNAPSHOT" -type f -perm /022 -print -quit)"
sudo chown -R "$DEPLOY_USER:$DEPLOY_USER" "$QUALITY_EVIDENCE_DIR"
sudo find "$QUALITY_EVIDENCE_DIR" -type d -exec chmod 0700 {} +
sudo find "$QUALITY_EVIDENCE_DIR" -type f -exec chmod 0600 {} +
sudo chown -R "$DEPLOY_USER:$DEPLOY_USER" "$QUALITY_ATTESTATION_DIR"
sudo chmod 0700 "$QUALITY_ATTESTATION_DIR"
sudo chmod 0600 "$QUALITY_ATTESTATION_FILE"
echo 'server_local_quality_acceptance=PASS'
```

`QUALITY_STARTED_AT` сохраняется до post-quality log scan. Passing summary содержит exact SHA,
clean-worktree provenance, KB/case hashes, все семь секций и `trace_coverage_rate=1`. Evidence
остаётся только на сервере; в handoff переносятся агрегаты и безопасные hashes. Для SHA после
обязательного correction re-release ниже этот gate выполняется заново; preliminary evidence старого
SHA не разрешает dispatcher.

Сразу после suite повторяется HTTPS security gate, но log window начинается до первого
авторизованного quality-запроса. Так проверка ищет в runtime/Nginx/relay logs значения credentials,
secret header names и probe markers за весь suite, не печатая сами логи:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
POST_QUALITY_SECURITY_REPORT="data/private/runtime/post-quality-security-${TRUSTED_GIT_SHA}.json"
test ! -e "$POST_QUALITY_SECURITY_REPORT"
EXPECTED_PUBLIC_IPV4='<NEW_SERVER_PUBLIC_IPV4>'
ADMIN_PUBLIC_HOST="$(python3 -c '
from pathlib import Path
for line in Path(".env.production").read_text(encoding="utf-8").splitlines():
    if line.startswith("ADMIN_PUBLIC_HOST="):
        print(line.split("=", 1)[1].strip())
        break
')"
test -n "$ADMIN_PUBLIC_HOST"
python3 scripts/run_runtime_security_acceptance.py \
  --env-file .env.production \
  --expected-git-sha "$TRUSTED_GIT_SHA" \
  --expected-public-ipv4 "$EXPECTED_PUBLIC_IPV4" \
  --runtime-base-url "https://${ADMIN_PUBLIC_HOST}" \
  --log-since-utc "$QUALITY_STARTED_AT" \
  --log-container rosmol-app-ml \
  --log-container rosmol-nginx \
  --log-container rosmol-edge-relay \
  --use-sudo-docker \
  --output "$POST_QUALITY_SECURITY_REPORT"
jq -e --arg sha "$TRUSTED_GIT_SHA" --arg since "$QUALITY_STARTED_AT" '
  .passed == true and
  .expected_git_sha == $sha and
  .log_scan_since_utc == $since and
  ([.checks[] | select(.passed != true)] | length) == 0
' "$POST_QUALITY_SECURITY_REPORT" >/dev/null
test "$(stat -c '%a' "$POST_QUALITY_SECURITY_REPORT")" = 600
git check-ignore -q "$POST_QUALITY_SECURITY_REPORT"
unset ACCEPTANCE_COST_LEDGER_DIR ACCEPTANCE_RUN_ID ACCEPTANCE_SOURCE_SNAPSHOT ADMIN_PUBLIC_HOST \
  HIGH_COST_APPROVAL_ID \
  CLEAN_ACCEPTANCE_SOURCE \
  EXPECTED_PUBLIC_IPV4 POST_QUALITY_SECURITY_REPORT \
  QUALITY_ATTESTATION_DIR QUALITY_ATTESTATION_FILE \
  QUALITY_EVIDENCE_DIR QUALITY_STARTED_AT acceptance_dc
echo 'post_quality_runtime_security=PASS'
```

До следующего платного eval владелец бюджета вручную сверяет `llm_estimated_cost_rub` этого
`ACCEPTANCE_RUN_ID` с provider billing за точное UTC-окно и сохраняет private/external evidence:
run ID, runtime SHA, набор/версию, approval ID, оценку, фактическую сумму, процент расхождения и
verdict. Автоматической provider-billing сверки нет. Расхождение по модулю свыше 10%, неоднозначная
атрибуция или отсутствующий финальный счёт означает `STOP` до исправления и нового owner approval.

## Gate 5 — ограниченный HDE/VK smoke

Перед включением dispatcher на уже прошедшем preliminary acceptance runtime отдельно
воспроизводятся зафиксированные кейсы Наты (`Начать`, вопрос про даты и кейс «Машук»). Результаты
старого прерванного cohort не объединяются с новыми. По «Машуку» нужен явный content verdict:
подтверждён ли ожидаемый факт опубликованным источником. Затем выполняется ровно один явно
согласованный correction cycle: наблюдаемый дефект → минимальная правка одного слоя → regression
test → полный server-local gate. Без этого шага разрешён только изолированный acceptance-контур.

### Обязательный re-release после regression-first correction

Correction создаёт новый commit, поэтому preliminary image/report/SHA немедленно становятся
stale. Нельзя править bind-mounted code на host, перезапускать старый image или считать зелёный
report предыдущего SHA достаточным. Provider secrets при этом не ротируются повторно без причины,
но они не должны попасть ни в новый build context, ни в build args, scanner source или отчёты.

Сначала dispatcher/rule подтверждается как `OFF`, старый preliminary runtime останавливается без
удаления data volumes, а новый reviewed commit извлекается в отдельный clean source tree. Этот tree
создаётся **без `.env.production`** и используется для build/history scan; production checkout с
secrets в Docker build не монтируется:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
PRELIMINARY_GIT_SHA="$TRUSTED_GIT_SHA"
CORRECTION_GIT_SHA='<NEW_REVIEWED_40_LOWERCASE_HEX_SHA>'
test "$CORRECTION_GIT_SHA" != "$PRELIMINARY_GIT_SHA"
printf '%s' "$CORRECTION_GIT_SHA" | grep -Eq '^[0-9a-f]{40}$'

old_dc=(sudo docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.ml.yml -f docker-compose.prod.yml)
"${old_dc[@]}" --profile ml stop edge-relay nginx app app-ml

sudo -u "$DEPLOY_USER" git fetch --prune origin master
sudo -u "$DEPLOY_USER" git cat-file -e "${CORRECTION_GIT_SHA}^{commit}"
sudo -u "$DEPLOY_USER" git checkout --detach "$CORRECTION_GIT_SHA"
test "$(sudo -u "$DEPLOY_USER" git rev-parse HEAD)" = "$CORRECTION_GIT_SHA"
test -z "$(sudo -u "$DEPLOY_USER" git status --porcelain --untracked-files=normal)"
sudo -u "$DEPLOY_USER" git check-ignore -q .env.production

CLEAN_RELEASE_SOURCE="/var/lib/rosmol/release-source/${CORRECTION_GIT_SHA}"
test ! -e "$CLEAN_RELEASE_SOURCE"
sudo install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$(dirname "$CLEAN_RELEASE_SOURCE")"
sudo -u "$DEPLOY_USER" git clone --no-local /opt/rosmol-ai-bot "$CLEAN_RELEASE_SOURCE"
sudo -u "$DEPLOY_USER" git -C "$CLEAN_RELEASE_SOURCE" checkout --detach "$CORRECTION_GIT_SHA"
test -z "$(sudo -u "$DEPLOY_USER" git -C "$CLEAN_RELEASE_SOURCE" \
  status --porcelain --untracked-files=normal)"
test ! -e "$CLEAN_RELEASE_SOURCE/.env"
test ! -e "$CLEAN_RELEASE_SOURCE/.env.production"
if ! sudo -u "$DEPLOY_USER" git -C "$CLEAN_RELEASE_SOURCE" diff --quiet \
  "$PRELIMINARY_GIT_SHA" "$CORRECTION_GIT_SHA" -- \
  .dockerignore Dockerfile requirements deploy/huggingface_models.lock.json \
  docker-compose.yml docker-compose.ml.yml \
  docker-compose.prod.yml docker-compose.admin-tls.yml docker-compose.acceptance.yml \
  haproxy nginx security; then
  echo 'STOP: build/infra/security inputs changed; repeat full Gate 3, including every image scan'
  exit 1
fi
```

Build запускается из clean tree, с очищенным process environment и без `--env-file`. Единственные
build inputs из shell — новый публичный Git SHA и versioned Compose/Dockerfile. Затем OCI label и
dependency graph проверяются до повторного допуска runtime:

```bash
set -Eeuo pipefail
cd "$CLEAN_RELEASE_SOURCE"
rebuild_dc=(sudo env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/root RELEASE_GIT_SHA="$CORRECTION_GIT_SHA" \
  docker compose -f docker-compose.yml -f docker-compose.ml.yml)
"${rebuild_dc[@]}" --profile ml config --quiet
"${rebuild_dc[@]}" --profile ml build --pull --no-cache app app-ml
sudo docker image tag rosmol-ai-bot-app:dev \
  "rosmol-ai-bot-app:${CORRECTION_GIT_SHA}"
sudo docker image tag rosmol-ai-bot-ml:dev \
  "rosmol-ai-bot-ml:${CORRECTION_GIT_SHA}"
for image in \
  "rosmol-ai-bot-app:${CORRECTION_GIT_SHA}" \
  "rosmol-ai-bot-ml:${CORRECTION_GIT_SHA}"; do
  test "$(sudo docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" \
    = "$CORRECTION_GIT_SHA"
  sudo docker run --rm --network none "$image" python -m pip check
done
unset rebuild_dc image
```

Повторный scan использует clean source и cached Trivy DB из Gate 3. Оба scanner container работают
с `--network none`; provider env им не передаётся. Любая finding/ошибка/отсутствующий DB timestamp
останавливает re-release. Squid digest не менялся, поэтому используется его уже зелёный Gate 3
report; новые application images сканируются заново:

```bash
set -Eeuo pipefail
cd "$CLEAN_RELEASE_SOURCE"
GITLEAKS_IMAGE='zricethezav/gitleaks:v8.28.0@sha256:bf00b5e039f0fad4b32935dc5ec1e358f227ccd097bcb64b971f0331072fe2ae'
TRIVY_IMAGE='aquasec/trivy:0.64.1@sha256:de90a656e79b175a294abe85cb8b99670fab83ebf339cccd163e6f584846809a'
RESCAN_DIR="/var/lib/rosmol/security-scan/${CORRECTION_GIT_SHA}"
TRIVY_EXCEPTION_REVIEW_DEADLINE='2026-08-10'
[[ "$(date -u +%F)" < "$TRIVY_EXCEPTION_REVIEW_DEADLINE" ]] || {
  echo 'STOP: scoped application Trivy exception expired; re-review before continuing'
  exit 1
}
test ! -e "$RESCAN_DIR"
sudo install -d -m 0700 "$RESCAN_DIR"

sudo docker run --rm --network none --platform linux/amd64 \
  -v "$CLEAN_RELEASE_SOURCE:/repo:ro" -v "$RESCAN_DIR:/reports" "$GITLEAKS_IMAGE" \
  detect --source=/repo --redact --no-banner --exit-code=1 --log-opts=--all \
  --report-format=json --report-path=/reports/gitleaks.json
sudo docker run --rm --network none --platform linux/amd64 \
  -v rosmol_trivy_cache:/root/.cache/ "$TRIVY_IMAGE" version --format json \
  | sudo tee "$RESCAN_DIR/trivy-version-db.json" >/dev/null
sudo jq -e '.Version == "0.64.1" and .VulnerabilityDB.UpdatedAt != null' \
  "$RESCAN_DIR/trivy-version-db.json" >/dev/null

for image in \
  "rosmol-ai-bot-app:${CORRECTION_GIT_SHA}" \
  "rosmol-ai-bot-ml:${CORRECTION_GIT_SHA}"; do
  safe_name="$(printf '%s' "$image" | sed 's/[^A-Za-z0-9._-]/_/g')"
  sudo docker run --rm --network none --platform linux/amd64 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v rosmol_trivy_cache:/root/.cache/ -v "$RESCAN_DIR:/reports" "$TRIVY_IMAGE" \
    image --skip-db-update --format cyclonedx \
    --output "/reports/${safe_name}.cdx.json" "$image"
  sudo docker run --rm --network none --platform linux/amd64 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "$CLEAN_RELEASE_SOURCE:/repo:ro" -v rosmol_trivy_cache:/root/.cache/ \
    -v "$RESCAN_DIR:/reports" "$TRIVY_IMAGE" \
    image --skip-db-update --scanners vuln --severity CRITICAL --exit-code 1 \
    --format json --ignorefile /repo/security/trivy-app-ignore.yaml --show-suppressed \
    --output "/reports/${safe_name}.critical.json" "$image"
  sudo docker run --rm --network none --platform linux/amd64 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v rosmol_trivy_cache:/root/.cache/ -v "$RESCAN_DIR:/reports" "$TRIVY_IMAGE" \
    image --skip-db-update --scanners secret --exit-code 1 --format json \
    --output "/reports/${safe_name}.secrets.json" "$image"
done
sudo find "$RESCAN_DIR" -maxdepth 1 -type f ! -name SHA256SUMS -print0 \
  | sort -z | sudo xargs -0 sha256sum | sudo tee "$RESCAN_DIR/SHA256SUMS" >/dev/null
test -s "$RESCAN_DIR/SHA256SUMS"
unset GITLEAKS_IMAGE TRIVY_IMAGE RESCAN_DIR TRIVY_EXCEPTION_REVIEW_DEADLINE safe_name image
```

Только после зелёного rescan production env атомарно получает новый SHA; остальные secrets
сохраняются byte-for-byte и не выводятся. Затем выполняются миграция, условный reindex frozen
seed только при изменении его входов, offline ML check и запуск строго из новых tags. Если seed,
индексатор, retrieval/embedding code, model lock и ML dependency lock byte-for-byte не менялись,
существующая freshly-built коллекция сохраняется и вместо дорогостоящего reindex выполняется
строгая проверка baseline count/cache:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
python3 scripts/generate_production_env.py set-release-sha \
  --env-file .env.production --git-sha "$CORRECTION_GIT_SHA"
python3 scripts/generate_production_env.py validate .env.production
TRUSTED_GIT_SHA="$CORRECTION_GIT_SHA"
dc=(sudo docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.ml.yml -f docker-compose.prod.yml)
"${dc[@]}" --profile ml run --rm --pull never migrate
"${dc[@]}" --profile ml run --rm --pull never -T migrate python - <<'PY'
import asyncio
import os

import asyncpg

from src.channels.hde_transport import (
    FAIL_INBOX_SQL,
    FAIL_OUTBOX_SQL,
    RECOVER_STALE_INBOX_SQL,
    RECOVER_STALE_OUTBOX_SQL,
)


async def main() -> None:
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        for query in (
            RECOVER_STALE_INBOX_SQL,
            RECOVER_STALE_OUTBOX_SQL,
            FAIL_INBOX_SQL,
            FAIL_OUTBOX_SQL,
        ):
            await connection.prepare(query)
    finally:
        await connection.close()


asyncio.run(main())
print("hde_transport_sql_prepare=passed")
PY
index_inputs=(
  data/knowledge_base_seed.json
  data/forums_registry.json
  scripts/index_kb.py
  scripts/init_qdrant.py
  src/config.py
  src/kb
  src/rag
  deploy/huggingface_models.lock.json
  requirements/ml.lock
)
index_diff_rc=0
sudo -u "$DEPLOY_USER" git diff --quiet \
  "$PRELIMINARY_GIT_SHA" "$CORRECTION_GIT_SHA" -- "${index_inputs[@]}" \
  || index_diff_rc=$?
case "$index_diff_rc" in
  0)
    echo 'index_inputs_unchanged=PASS reindex=SKIPPED'
    ;;
  1)
    "${dc[@]}" --profile ml run --rm --pull never index-kb sh -c \
      'python scripts/init_qdrant.py && python scripts/index_kb.py \
       --path data/knowledge_base_seed.json --prune-stale'
    echo 'index_inputs_changed=PASS reindex=COMPLETED'
    ;;
  *)
    printf 'STOP=index_input_diff_failed exit=%s\n' "$index_diff_rc" >&2
    exit "$index_diff_rc"
    ;;
esac
unset index_diff_rc
"${dc[@]}" --profile ml run --rm --pull never ml-check --load-models
RUNTIME_STARTED_AT_FILE="data/private/runtime/runtime-started-at-${TRUSTED_GIT_SHA}.txt"
test ! -e "$RUNTIME_STARTED_AT_FILE"
umask 077
date -u +%Y-%m-%dT%H:%M:%SZ > "$RUNTIME_STARTED_AT_FILE"
"${dc[@]}" --profile ml up -d --no-build --wait --wait-timeout 480 \
  app app-ml nginx edge-relay
correction_ready_json="$("${dc[@]}" --profile ml exec -T app-ml python - <<'PY'
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=20) as response:
    print(response.read().decode("utf-8"))
PY
)"
jq -e --arg sha "$TRUSTED_GIT_SHA" '
  .status == "ready" and
  .release_git_sha == $sha and
  (.checks | keys) ==
    ["config", "hde_transport", "knowledge_base", "ml_prewarm", "postgres", "redis"] and
  ([.checks[]] | all(. == "ok")) and
  ([
    .hde_transport_counts.inbox_backlog,
    .hde_transport_counts.inbox_processing,
    .hde_transport_counts.inbox_dead_letter,
    .hde_transport_counts.outbox_backlog,
    .hde_transport_counts.outbox_sending,
    .hde_transport_counts.outbox_dead_letter
  ] | all(. == 0))
' <<<"$correction_ready_json" >/dev/null
correction_collection_counts="$("${dc[@]}" --profile ml exec -T app-ml python - <<'PY'
import asyncio
import json

from qdrant_client import AsyncQdrantClient

from src.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=10,
    )
    try:
        knowledge = await client.get_collection(settings.qdrant_knowledge_collection)
        response_cache = await client.get_collection("response_cache")
        print(
            json.dumps(
                {
                    "knowledge_base": int(knowledge.points_count or 0),
                    "response_cache": int(response_cache.points_count or 0),
                },
                sort_keys=True,
            )
        )
    finally:
        await client.close()


asyncio.run(main())
PY
)"
jq -e '.knowledge_base == 2152 and .response_cache == 0' \
  <<<"$correction_collection_counts" >/dev/null
sudo env -u ADMIN_PUBLIC_HOST -u CERTBOT_EMAIL bash scripts/provision_admin_https.sh
unset RUNTIME_STARTED_AT_FILE correction_collection_counts correction_ready_json \
  index_inputs
```

Для нового SHA с нуля повторяются Gate 3 readiness/egress assertions, Gate 4B exact-HTTPS report,
server-local `quality-acceptance` и post-quality HTTPS/log gate; идентификаторы и output directories
содержат `CORRECTION_GIT_SHA`. Старые отчёты не перезаписываются и не объединяются. Только новый
полный green chain разрешает переход к HDE smoke; при любой ошибке остаются `dispatcher OFF` и
`NO GO`.

Dispatcher сначала создаётся выключенным. До его включения в отдельном терминале запускается
наблюдатель из Gate 6; первый HDE scenario запрещён, пока наблюдатель не пишет свежие samples.
Проверяются URL и secret, затем rule включается только на один тестовый департамент/канал. Старый
Chatme/бот не должен отвечать параллельно.

Три ручных сценария выполняются в отдельных новых tickets:

1. grounded-вопрос по форуму → опубликованный источник и delivered answer;
2. прямая просьба об операторе → подтверждённый handoff оператору;
3. off-topic → короткий scope-note без оператора.

После каждого сценария проверяется цепочка `stable upstream id → inbox → request trace → outbox →
HDE delivery`. Никаких batch/eval-запросов через HDE не выполняется.

Если smoke создаёт `dead_letter`, dispatcher немедленно выключается; автоматический requeue
запрещён. Оператор сверяет post в HDE, сохраняет private evidence только в
`data/private/runtime/`, передаёт CLI лишь SHA-256 evidence и выполняет ровно одно аудируемое
решение из раздела HDE recovery в `docs/operations.md`: `reconcile-delivered`,
`requeue-outbox` с `provider_confirmed_not_delivered` либо `requeue-inbox` после проверки всех
side effects. `--operator`, фиксированный `--reason`, `--evidence-sha256` и совпадающие
`--job-id/--confirm-job-id` обязательны. Затем проверяются audit row, пустая очередь и `/ready`;
без этого dispatcher остаётся выключенным и release имеет статус `NO GO`.

## Gate 6 — inbound/outbound traffic

До запуска фиксируются host и container RX/TX counters. Во время smoke наблюдаются:

- provider network graph;
- host interface byte counters;
- `docker stats` network I/O;
- активные established connections;
- Nginx access/error log, app delivery status и outbox backlog;
- неожиданные destination/port, постоянный egress без входящих запросов, CPU/disk spikes.

Ожидаемый provider-bearing application TCP/HTTPS egress ограничен proxy CONNECT к согласованным
Cloud.ru/HDE endpoint; exact Yonote endpoint допустим только во время отдельно включённого ручного
Preview. У secretless L4 `edge-relay` есть внешний маршрут для публичного ingress;
его ответный трафик на входящие `80/443` соединения ожидаем, но любой необъяснимый relay-initiated
destination без соответствующего inbound flow является stop criterion. Model и OS downloads
выполняются до открытия dispatcher. Необъяснимый постоянный egress, новый listening port или
резкий рост трафика — немедленный stop criterion: dispatcher off, ingress closed, VM isolated,
incident triage.

Docker embedded DNS остаётся host-mediated каналом и не входит в Squid CONNECT allowlist. Для
ограниченного test-production это явно принятый residual: provider flow/DNS logging и host alerts
обязательны, необычные или высокоэнтропийные DNS names являются stop criterion. До широкого
production traffic требуется отдельный reviewed DNS deny/allow policy; утверждать «полный egress
allowlist» до неё запрещено.

Перед HDE smoke в отдельном терминале новой VM запускается foreground-наблюдатель на 15 минут.
Smoke выполняется параллельно только после появления первого sample; ошибка наблюдателя прерывает
smoke. Результат не коммитится и не отправляется в общий чат без редактирования адресов
инфраструктуры:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
sudo install -d -m 0700 data/private/runtime
TRAFFIC_LOG="data/private/runtime/traffic-smoke-${TRUSTED_GIT_SHA}.log"
test ! -e "$TRAFFIC_LOG"
sudo install -m 0600 /dev/null "$TRAFFIC_LOG"
sudo bash scripts/monitor_runtime_traffic.sh 5 180 \
  | sudo tee "$TRAFFIC_LOG"
test -s "$TRAFFIC_LOG"
sudo ss -lntup
unset TRAFFIC_LOG
```

Наружу ожидаются только `22` из trusted CIDR и `80/443`; `8001`, `5432/6379/6333/6334`
не должны быть host listeners вообще. Наблюдаемый application
egress во время smoke сопоставляется с согласованными HDE/Cloud.ru endpoints; Yonote egress
ожидается только при явном manual Preview. Model/package downloads к этому моменту уже завершены.

## Gate 7 — admin и handoff

- Admin доступен только по новому HTTPS URL и не публикуется до TLS acceptance.
- По умолчанию recovery admin работает read-only. После отдельного enable разрешён полный Yonote
  Preview с неизменными seed/Qdrant.
- Ограниченный test-editor включается только отдельным post-acceptance решением владельца. Он
  требует private working seed, explicit capability flags и отдельный gate ниже. Это не снимает
  запрет на изменение Yonote и не превращает рабочую копию в canonical production seed.
- Старые presentation `100%` reports не считаются текущим release evidence.
- Проверяются login rate limit, logout/cookie invalidation, quality/ops report без PII и корректный
  security banner.
- В `docs/CURRENT_STATE.md` фиксируются trusted commit, clean host, rotation statuses, migration,
  KB count, gate results, HDE smoke, traffic baseline, residual risks и первая новая cohort boundary.

### Отдельный gate тестового редактора KB

Этот gate не является частью default production rollout. До него HDE dispatcher должен быть
выключен, durable queues — пусты, PostgreSQL backup — подтверждён, а exact candidate — полностью
проверен и просканирован. Никакое значение секрета в команды не передаётся.

1. Создать private working seed только из exact deployed tracked seed. Не перезаписывать уже
   существующий workspace и не хранить backup в writable mount:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
test "$(git rev-parse HEAD)" = "$TRUSTED_GIT_SHA"
sudo install -d -o 10001 -g 10001 -m 0700 data/private/admin-kb
test ! -e data/private/admin-kb/knowledge_base_seed.json
sudo install -o 10001 -g 10001 -m 0600 \
  data/knowledge_base_seed.json \
  data/private/admin-kb/knowledge_base_seed.json
cmp -s data/knowledge_base_seed.json \
  data/private/admin-kb/knowledge_base_seed.json
sudo install -d -o root -g root -m 0700 /var/backups/rosmol-ai-bot
sudo install -o root -g root -m 0400 \
  data/private/admin-kb/knowledge_base_seed.json \
  "/var/backups/rosmol-ai-bot/admin-kb-baseline-${TRUSTED_GIT_SHA}.json"
echo 'admin_kb_workspace_prepare=PASS'
```

2. Человек открывает server-only `.env.production` в защищённом редакторе и устанавливает ровно:

```dotenv
ADMIN_READ_ONLY=false
ADMIN_MUTATIONS_ENABLED=true
ADMIN_KB_SEED_PATH=/app/data/private/admin-kb/knowledge_base_seed.json
```

Затем запускаются env validator и effective Compose inspection. `app` обязан сохранить
`ADMIN_READ_ONLY=true`, `ADMIN_MUTATIONS_ENABLED=false` и read-only mount; только `app-ml` получает
writable capability и mount. Оба runtime используют exact private working seed path. Tracked seed
остаётся mounted read-only.

3. Без reindex пересоздаются только `app` и `app-ml`. Проверяются exact release label, health,
restart/OOM, отсутствие published ports, неизменность PostgreSQL/Redis/Qdrant/proxy/Nginx/relay
container IDs, равные tracked/working seed hashes и прежние Qdrant counts. Runtime security gate
повторяется с новым уникальным private report и лог-окном.

4. В UI сначала выполняется Yonote Preview. До и после обязаны совпасть working-seed hash, tracked
seed hash и Qdrant counts. Для безопасной проверки Save -> Qdrant можно сохранить без изменения
текста один уже открытый published чанк с включённым точечным reindex: это атомарно перепишет
эквивалентный working seed, выполнит upsert и сбросит соответствующий cache, не меняя content.
Затем один server-local `/ask` с bypass-cache должен вернуть grounded answer с ожидаемым source.

5. Yonote Apply разрешён только после content review. Он меняет исключительно private working
seed и не вызывает Yonote write API. Если меняется published set, HDE остаётся выключен, пока
server-controlled полный index с `--prune-stale`, cache clear и restart обоих runtime не завершит
readiness/security/RAG smoke. Публичного endpoint для полного reindex нет.

Перед sealed cohort вернуть `ADMIN_READ_ONLY=true`, `ADMIN_MUTATIONS_ENABLED=false`,
`ADMIN_KB_SEED_PATH=` и пересоздать `app`/`app-ml` на reviewed canonical seed release.

## Немедленный rollback/stop

При проблеме сначала выключается новый dispatcher rule. Затем сохраняются безопасные logs и
агрегированные counters, без выгрузки secrets/PII. Если проблема только в проверенном коде,
допустим rebuild предыдущего trusted commit на том же чистом host при совместимой schema. Старые
VM, images, volumes, credentials и backups не используются никогда.
