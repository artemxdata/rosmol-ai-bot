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
подготовка моделей выполняются до создания `.env.production`:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
export RELEASE_GIT_SHA="$TRUSTED_GIT_SHA"
test ! -e .env.production

build_dc=(sudo docker compose -f docker-compose.yml -f docker-compose.ml.yml)
"${build_dc[@]}" --profile ml config --quiet
"${build_dc[@]}" --profile ml build --pull app app-ml
sudo docker image tag rosmol-ai-bot-app:dev "rosmol-ai-bot-app:${TRUSTED_GIT_SHA}"
sudo docker image tag rosmol-ai-bot-ml:dev "rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}"

"${build_dc[@]}" --profile ml run --rm --no-deps --pull never ml-cache-init
"${build_dc[@]}" --profile ml run --rm --no-deps --pull never model-prefetch
"${build_dc[@]}" --profile ml run --rm --no-deps --pull never ml-check --load-models

sudo docker image inspect \
  "rosmol-ai-bot-app:${TRUSTED_GIT_SHA}" \
  "rosmol-ai-bot-ml:${TRUSTED_GIT_SHA}" \
  --format '{{.RepoTags}} {{.Id}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Обе строки `revision=` обязаны совпасть с `TRUSTED_GIT_SHA`. `ml-check` работает с
`network_mode: none`, читает модели из read-only volume и тем самым доказывает offline load.
Затем выполняется один закреплённый scanner workflow. Теги ниже дополнительно закреплены
linux/amd64 manifest digest; менять version/digest без отдельного review нельзя. Gitleaks читает
всю Git history и работает с `--redact`. Trivy сохраняет версию и timestamp vulnerability DB,
SBOM, Critical-CVE и image-secret reports в private каталог. Provider credentials на этом этапе
ещё отсутствуют:

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
VEX_REVIEW_DEADLINE='2026-07-27'
[[ "$(date -u +%F)" < "$VEX_REVIEW_DEADLINE" ]] || {
  echo 'STOP: scoped VEX verdict expired; re-review before continuing'
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
  vex_args=()
  case "$image" in
    "$APP_IMAGE"|"$ML_IMAGE") vex_args=(--vex /repo/security/trivy-app-vex.yaml --show-suppressed) ;;
    "$POSTGRES_IMAGE") vex_args=(--vex /repo/security/trivy-postgres-vex.yaml --show-suppressed) ;;
    "$QDRANT_IMAGE") vex_args=(--vex /repo/security/trivy-qdrant-vex.yaml --show-suppressed) ;;
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
    --format json "${vex_args[@]}" \
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
unset SCAN_DIR VEX_REVIEW_DEADLINE safe_name image vex_args production_images pull_images
```

Любая Gitleaks/image-secret finding, активная Critical CVE, истёкший scoped VEX или отсутствующий
vulnerability DB timestamp блокирует выпуск provider credentials. VEX применяется только к exact
PURL соответствующего app/PostgreSQL/Qdrant image; Redis/Nginx/Certbot/Squid/HAProxy сканируются
без VEX. Основание и срок обязательного re-review зафиксированы в
`docs/security_scan_verdict_20260720.md`. Scanner JSON и SBOM не публикуются автоматически: они
могут содержать package paths и остаются private evidence.

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

Generator принимает только точный reviewed Cloud.ru endpoint и точный tenant
`rosmolodezh.helpdeskeddy.com`; cross-provider substitution, IP literal, другой порт, query,
fragment или credentials в URL блокируют запуск. Сгенерированный Squid config содержит только
два hostname, не содержит API keys и не перезаписывается неявно. До первого `up` его разбирает
тот же закреплённый image, который будет работать в runtime:

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
"${dc[@]}" up -d --no-build postgres redis qdrant runtime-egress-proxy
"${dc[@]}" run --rm --pull never migrate
"${dc[@]}" --profile ml run --rm --pull never index-kb sh -c \
  'python scripts/init_qdrant.py && python scripts/index_kb.py \
   --path data/knowledge_base_seed.json --prune-stale'
"${dc[@]}" --profile ml up -d --no-build app app-ml
"${dc[@]}" --profile ml ps
ready_json="$(curl -fsS --max-time 20 http://127.0.0.1:8001/ready)"
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

collection_counts="$("${dc[@]}" exec -T app-ml python - <<'PY'
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
unset ready_json collection_counts
```

До dispatcher отдельно доказывается фактическая network policy. Проверка не передаёт provider
credentials: она отправляет только HTTP `CONNECT` к двум host из уже проверенных URL. Разрешённые
host должны вернуть proxy status `200`, посторонний host — `403`, а прямой TCP из `app-ml` к
публичному адресу и metadata endpoint обязан завершиться ошибкой:

```bash
set -Eeuo pipefail
"${dc[@]}" exec -T app-ml python - <<'PY'
import os
import socket
from urllib.parse import urlsplit

proxy = ("runtime-egress-proxy", 3128)
allowed = {
    urlsplit(os.environ["CLOUD_RU_CHAT_COMPLETIONS_URL"]).hostname,
    urlsplit(os.environ["HDE_BASE_URL"]).hostname,
}

def connect_status(host: str) -> int:
    with socket.create_connection(proxy, timeout=8) as stream:
        request = f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n"
        stream.sendall(request.encode("ascii"))
        status_line = stream.recv(4096).split(b"\r\n", 1)[0]
    return int(status_line.split()[1])

assert allowed == {
    "foundation-models.api.cloud.ru",
    "rosmolodezh.helpdeskeddy.com",
}
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

app_ml_id="$("${dc[@]}" ps -q app-ml)"
proxy_id="$("${dc[@]}" ps -q runtime-egress-proxy)"
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
"${dc[@]}" exec -T app python -m pip check
"${dc[@]}" exec -T app-ml python -m pip check
test "$("${dc[@]}" exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
   "select version_num from alembic_version"' | tr -d '\r')" = 008_hde_durable_transport
```

Nginx сначала поднимается только в HTTP bootstrap policy: ACME и `/health` доступны, а `/ask`,
`/webhook/*`, `/ready` и admin возвращают `426`.

```bash
set -Eeuo pipefail
"${dc[@]}" --profile ml up -d --no-build nginx edge-relay
test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/health)" = 200
test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/ask)" = 426
test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1/webhook/hde)" = 426

nginx_id="$("${dc[@]}" ps -q nginx)"
relay_id="$("${dc[@]}" ps -q edge-relay)"
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
if "${dc[@]}" exec -T nginx wget -q -T 3 -O /dev/null http://1.1.1.1; then
  echo 'STOP: Nginx direct public egress is reachable'
  exit 1
fi
if "${dc[@]}" exec -T nginx wget -q -T 3 -O /dev/null http://169.254.169.254; then
  echo 'STOP: Nginx metadata egress is reachable'
  exit 1
fi
unset nginx_id relay_id
sudo env -u ADMIN_PUBLIC_HOST -u CERTBOT_EMAIL bash scripts/provision_admin_https.sh
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

Gate 4A выполняется в clean local/CI checkout до deploy; `run_acceptance.py` ниже затем повторно
запускает полный `pytest`. Локальную `.venv` нельзя переносить на server, заменять этот gate
тестами внутри runtime image или устанавливать dev dependencies в production image.

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
python3 scripts/run_runtime_security_acceptance.py \
  --env-file .env.production \
  --expected-git-sha "$TRUSTED_GIT_SHA" \
  --runtime-base-url http://127.0.0.1:8001 \
  --log-container rosmol-app-ml \
  --log-container rosmol-nginx \
  --log-container rosmol-edge-relay \
  --use-sudo-docker \
  --output "$RUNTIME_SECURITY_REPORT"
jq -e --arg sha "$TRUSTED_GIT_SHA" \
  '.passed == true and .expected_git_sha == $sha and
   ([.checks[] | select(.passed != true)] | length) == 0' \
  "$RUNTIME_SECURITY_REPORT" >/dev/null
test "$(stat -c '%a' "$RUNTIME_SECURITY_REPORT")" = 600
git check-ignore -q "$RUNTIME_SECURITY_REPORT"
unset RUNTIME_SECURITY_REPORT
```

Любой skipped/недоступный log scan, неожиданный status, непустая очередь, SHA mismatch или
утечка marker/credential делает report красным и сохраняет dispatcher в состоянии `OFF`.

### Финальный acceptance, привязанный к commit

`run_acceptance.py` намеренно не может пройти без явно заданного trusted SHA, при dirty worktree,
пропущенном шаге, неполном trace coverage или stale quality report. Запускать его удобно с
доверенного локального checkout через SSH tunnel к loopback `app-ml` нового сервера. Так eval не
идёт через HDE/VK и порт `8001` не публикуется наружу. Passing report считается финальным только
для SHA, прошедшего обязательный correction re-release ниже; preliminary report старого SHA
сохраняется как evidence, но не разрешает dispatcher.

В первом локальном терминале:

```powershell
ssh -N -L 18001:127.0.0.1:8001 <NEW_DEPLOY_USER>@<NEW_HOST>
```

Во втором локальном терминале новый `API_AUTH_TOKEN` загружается в process environment через
secret manager/защищённый prompt, а не как literal в command line. Затем из clean checkout того
же commit:

```powershell
$trustedGitSha = '<40_LOWERCASE_HEX_SHA>'
if ((git rev-parse HEAD) -ne $trustedGitSha) { throw 'trusted SHA mismatch' }
if (git status --porcelain --untracked-files=normal) { throw 'worktree is dirty' }
.venv\Scripts\python.exe scripts\run_acceptance.py `
  --expected-git-sha $trustedGitSha `
  --target http://127.0.0.1:18001/ask `
  --ready-url http://127.0.0.1:18001/ready `
  --output-dir reports/final_acceptance_recovery `
  --quality-output-dir reports/pre_pilot_quality_suite_recovery
Remove-Item Env:API_AUTH_TOKEN -ErrorAction SilentlyContinue
```

Passing report должен содержать тот же `release_run_id`, expected/current Git SHA, KB hash и
hashes всех case files. Сам report остаётся локальным ignored artifact; в handoff переносятся
только агрегаты и безопасные hashes.

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
"${old_dc[@]}" stop edge-relay nginx app app-ml

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
  docker-compose.prod.yml docker-compose.admin-tls.yml haproxy nginx security; then
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
VEX_REVIEW_DEADLINE='2026-07-27'
[[ "$(date -u +%F)" < "$VEX_REVIEW_DEADLINE" ]] || {
  echo 'STOP: scoped application VEX verdict expired; re-review before continuing'
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
    --format json --vex /repo/security/trivy-app-vex.yaml --show-suppressed \
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
unset GITLEAKS_IMAGE TRIVY_IMAGE RESCAN_DIR VEX_REVIEW_DEADLINE safe_name image
```

Только после зелёного rescan production env атомарно получает новый SHA; остальные secrets
сохраняются byte-for-byte и не выводятся. Затем выполняются миграция, обязательный reindex frozen
seed с очисткой stale response cache, offline ML check и запуск строго из новых tags:

```bash
set -Eeuo pipefail
cd /opt/rosmol-ai-bot
python3 scripts/generate_production_env.py set-release-sha \
  --env-file .env.production --git-sha "$CORRECTION_GIT_SHA"
python3 scripts/generate_production_env.py validate .env.production
TRUSTED_GIT_SHA="$CORRECTION_GIT_SHA"
dc=(sudo docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.ml.yml -f docker-compose.prod.yml)
"${dc[@]}" run --rm --pull never migrate
"${dc[@]}" --profile ml run --rm --pull never index-kb sh -c \
  'python scripts/init_qdrant.py && python scripts/index_kb.py \
   --path data/knowledge_base_seed.json --prune-stale'
"${dc[@]}" --profile ml run --rm --pull never ml-check --load-models
"${dc[@]}" --profile ml up -d --no-build app app-ml nginx edge-relay
sudo env -u ADMIN_PUBLIC_HOST -u CERTBOT_EMAIL bash scripts/provision_admin_https.sh
```

Для нового SHA с нуля повторяются Gate 3 readiness/egress assertions, Gate 4B live report и
`run_acceptance.py`; output directories включают `CORRECTION_GIT_SHA`. Старые отчёты не
перезаписываются и не объединяются. Только новый полный green chain разрешает переход к HDE
smoke; при любой ошибке остаются `dispatcher OFF` и `NO GO`.

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
Cloud.ru/HDE endpoint. У secretless L4 `edge-relay` есть внешний маршрут для публичного ingress;
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

Наружу ожидаются только `22` из trusted CIDR и `80/443`; `8001` должен слушать только
`127.0.0.1`, а `5432/6379/6333/6334` не должны быть host listeners. Наблюдаемый application
egress во время smoke сопоставляется с согласованными HDE/Cloud.ru endpoints; model/package
downloads к этому моменту уже завершены.

## Gate 7 — admin и handoff

- Admin доступен только по новому HTTPS URL и не публикуется до TLS acceptance.
- В recovery release admin работает read-only; Yonote Apply и live KB mutation не используются.
- Старые presentation `100%` reports не считаются текущим release evidence.
- Проверяются login rate limit, logout/cookie invalidation, quality/ops report без PII и корректный
  security banner.
- В `docs/CURRENT_STATE.md` фиксируются trusted commit, clean host, rotation statuses, migration,
  KB count, gate results, HDE smoke, traffic baseline, residual risks и первая новая cohort boundary.

## Немедленный rollback/stop

При проблеме сначала выключается новый dispatcher rule. Затем сохраняются безопасные logs и
агрегированные counters, без выгрузки secrets/PII. Если проблема только в проверенном коде,
допустим rebuild предыдущего trusted commit на том же чистом host при совместимой schema. Старые
VM, images, volumes, credentials и backups не используются никогда.
