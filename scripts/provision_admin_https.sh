#!/usr/bin/env bash

ENV_FILE="${ROSMOL_ENV_FILE:-.env.production}"
PUBLIC_HOST="${ADMIN_PUBLIC_HOST:-}"
CERTBOT_EMAIL_VALUE="${CERTBOT_EMAIL:-}"
NGINX_HTTP_BIND_VALUE="${NGINX_BIND:-}"
NGINX_HTTP_PORT_VALUE="${NGINX_HOST_PORT:-}"
NGINX_HTTPS_BIND_VALUE="${NGINX_TLS_BIND:-}"
NGINX_HTTPS_PORT_VALUE="${NGINX_TLS_HOST_PORT:-}"
CERT_NAME="rosmol-admin"
PROJECT_DIR="/opt/rosmol-ai-bot"
CERTIFICATE="data/private/letsencrypt/live/${CERT_NAME}/fullchain.pem"
PRIVATE_KEY="data/private/letsencrypt/live/${CERT_NAME}/privkey.pem"

BASE_COMPOSE=(
  docker compose
  --env-file "$ENV_FILE"
  -f docker-compose.yml
  -f docker-compose.ml.yml
  -f docker-compose.prod.yml
)

TLS_COMPOSE=(
  docker compose
  --env-file "$ENV_FILE"
  -f docker-compose.yml
  -f docker-compose.ml.yml
  -f docker-compose.prod.yml
  -f docker-compose.admin-tls.yml
)

log() {
  printf '\n[%s] %s\n' "rosmol-admin-https" "$1"
}

fail() {
  printf '\n[%s] ERROR: %s\n' "rosmol-admin-https" "$1" >&2
  return 1
}

read_env_value() {
  local key="$1"
  local line
  local source="$ENV_FILE"
  if [ ! -f "$source" ]; then
    source="${PROJECT_DIR}/${ENV_FILE}"
  fi
  line="$(grep -m1 "^${key}=" "$source" 2>/dev/null || true)"
  printf '%s' "${line#*=}"
}

restore_http_nginx() {
  log "Возвращаю рабочий HTTP Nginx после неуспешного TLS-переключения"
  : > data/private/letsencrypt/.force-http
  if "${BASE_COMPOSE[@]}" --profile ml exec -T nginx \
    cp /etc/nginx/rosmol/default.conf /etc/nginx/conf.d/default.conf && \
    "${BASE_COMPOSE[@]}" --profile ml exec -T nginx nginx -t; then
    "${BASE_COMPOSE[@]}" --profile ml exec -T nginx nginx -s reload
    return $?
  fi

  "${BASE_COMPOSE[@]}" --profile ml up -d --no-build --force-recreate nginx
}

main() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "запусти скрипт от root"
    return 1
  fi

  if [ ! -f "$ENV_FILE" ] && [ ! -f "${PROJECT_DIR}/${ENV_FILE}" ]; then
    fail "не найден production env-файл $ENV_FILE"
    return 1
  fi

  if [ -z "$PUBLIC_HOST" ]; then
    PUBLIC_HOST="$(read_env_value ADMIN_PUBLIC_HOST)"
  fi
  if [ -z "$CERTBOT_EMAIL_VALUE" ]; then
    CERTBOT_EMAIL_VALUE="$(read_env_value CERTBOT_EMAIL)"
  fi
  if [ -z "$NGINX_HTTP_BIND_VALUE" ]; then
    NGINX_HTTP_BIND_VALUE="$(read_env_value NGINX_BIND)"
  fi
  if [ -z "$NGINX_HTTP_PORT_VALUE" ]; then
    NGINX_HTTP_PORT_VALUE="$(read_env_value NGINX_HOST_PORT)"
  fi
  if [ -z "$NGINX_HTTPS_BIND_VALUE" ]; then
    NGINX_HTTPS_BIND_VALUE="$(read_env_value NGINX_TLS_BIND)"
  fi
  if [ -z "$NGINX_HTTPS_PORT_VALUE" ]; then
    NGINX_HTTPS_PORT_VALUE="$(read_env_value NGINX_TLS_HOST_PORT)"
  fi
  if [ -z "$CERTBOT_EMAIL_VALUE" ]; then
    fail "CERTBOT_EMAIL is required for the ACME account and renewal notices"
    return 1
  fi
  case "${PUBLIC_HOST},${CERTBOT_EMAIL_VALUE}" in
    *replace-with*|*example.test*|*your-*)
      fail "ADMIN_PUBLIC_HOST/CERTBOT_EMAIL still contain template placeholder values"
      return 1
      ;;
  esac
  if [ "$NGINX_HTTP_BIND_VALUE" != "0.0.0.0" ] || \
    [ "$NGINX_HTTP_PORT_VALUE" != "80" ] || \
    [ "$NGINX_HTTPS_BIND_VALUE" != "0.0.0.0" ] || \
    [ "$NGINX_HTTPS_PORT_VALUE" != "443" ]; then
    fail "production TLS provisioning requires NGINX_BIND=0.0.0.0:80 and NGINX_TLS_BIND=0.0.0.0:443"
    return 1
  fi

  if [ -z "$PUBLIC_HOST" ]; then
    fail "задай ADMIN_PUBLIC_HOST с новым DNS-именем или публичным IPv4-адресом"
    return 1
  fi

  case "$PUBLIC_HOST" in
    *[!A-Za-z0-9.-]*)
      fail "ADMIN_PUBLIC_HOST должен содержать только DNS-имя или IPv4-адрес без схемы и пути"
      return 1
      ;;
  esac

  cd "$PROJECT_DIR" || {
    fail "не найден каталог ${PROJECT_DIR}"
    return 1
  }

  if [ ! -f "$ENV_FILE" ]; then
    fail "не найден production env-файл $ENV_FILE"
    return 1
  fi

  if [ -n "$(find "$ENV_FILE" -maxdepth 0 -perm /077 -print -quit)" ]; then
    fail "$ENV_FILE должен быть недоступен для group/other (mode 0600 или строже)"
    return 1
  fi

  install -d -m 0700 data/private/letsencrypt || return 1
  install -d -m 0755 data/private/acme-webroot/.well-known/acme-challenge || return 1
  rm -f data/private/letsencrypt/.force-http

  log "Включаю ACME webroot; приложение, routing и KB не перезапускаются"
  # Compose recreates nginx only when its service definition actually changed.
  # A safe rerun after recovery therefore has no unnecessary interruption.
  "${BASE_COMPOSE[@]}" --profile ml up -d --no-build nginx edge-relay || return 1

  local probe_name="rosmol-admin-acme-probe"
  local probe_value="rosmol-admin-acme-ok"
  printf '%s' "$probe_value" > "data/private/acme-webroot/.well-known/acme-challenge/${probe_name}"

  local observed_probe
  # Some providers do not support connecting from a server to its own public
  # IP. Validate the mounted Nginx webroot locally; Certbot performs the real
  # external ACME validation immediately afterwards.
  observed_probe="$(curl -fsS --max-time 20 "http://127.0.0.1/.well-known/acme-challenge/${probe_name}")" || {
    fail "локальный HTTP-01 webroot недоступен через Nginx"
    return 1
  }

  if [ "$observed_probe" != "$probe_value" ]; then
    fail "HTTP-01 challenge вернул неожиданный ответ"
    return 1
  fi

  rm -f "data/private/acme-webroot/.well-known/acme-challenge/${probe_name}"

  local -a certbot_target_args
  if [[ "$PUBLIC_HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    certbot_target_args=(--preferred-profile shortlived --ip-address "$PUBLIC_HOST")
  else
    certbot_target_args=(--domains "$PUBLIC_HOST")
  fi

  log "Получаю новый сертификат Let's Encrypt для явно заданного endpoint"
  "${TLS_COMPOSE[@]}" --profile ml --profile tls run --rm --no-deps --pull never certbot certonly \
    --non-interactive \
    --agree-tos \
    --email "$CERTBOT_EMAIL_VALUE" \
    --webroot \
    --webroot-path /var/www/certbot \
    --cert-name "$CERT_NAME" \
    "${certbot_target_args[@]}" || return 1

  if [ ! -s "$CERTIFICATE" ] || [ ! -s "$PRIVATE_KEY" ]; then
    fail "Certbot не создал ожидаемые файлы сертификата"
    return 1
  fi

  log "Проверяю автоматическое продление через Let's Encrypt staging"
  "${TLS_COMPOSE[@]}" --profile ml --profile tls run --rm --no-deps --pull never certbot renew \
    --dry-run \
    --no-random-sleep-on-renew || return 1

  log "Проверяю TLS-конфигурацию до переключения рабочего Nginx"
  "${BASE_COMPOSE[@]}" --profile ml run --rm --no-deps --pull never nginx nginx -t || return 1

  if command -v ufw >/dev/null 2>&1; then
    ufw allow 443/tcp || return 1
  fi

  log "Подключаю сертификат штатным reload без остановки приложения"
  "${BASE_COMPOSE[@]}" --profile ml exec -T nginx \
    cp /etc/nginx/rosmol/admin-tls.conf /etc/nginx/conf.d/default.conf || {
    restore_http_nginx
    return 1
  }

  "${BASE_COMPOSE[@]}" --profile ml exec -T nginx nginx -t || {
    restore_http_nginx
    return 1
  }

  "${BASE_COMPOSE[@]}" --profile ml exec -T nginx nginx -s reload || {
    restore_http_nginx
    return 1
  }

  install -m 0644 deploy/systemd/rosmol-admin-tls-renew.service \
    /etc/systemd/system/rosmol-admin-tls-renew.service || return 1
  install -m 0644 deploy/systemd/rosmol-admin-tls-renew.timer \
    /etc/systemd/system/rosmol-admin-tls-renew.timer || return 1
  systemctl daemon-reload || return 1
  systemctl enable --now rosmol-admin-tls-renew.timer || return 1

  log "Проверяю renewal и безопасный HTTPS-доступ"
  systemctl start rosmol-admin-tls-renew.service || return 1
  curl -fsS --max-time 20 -o /dev/null "https://${PUBLIC_HOST}/admin/kb" || return 1

  log "Готово: новый HTTPS endpoint админ-панели работает без SSH-туннеля"
}

main "$@"
