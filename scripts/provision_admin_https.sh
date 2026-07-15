#!/usr/bin/env bash

PUBLIC_IP="139.100.225.44"
PROJECT_DIR="/opt/rosmol-ai-bot"
CERTIFICATE="data/private/letsencrypt/live/${PUBLIC_IP}/fullchain.pem"
PRIVATE_KEY="data/private/letsencrypt/live/${PUBLIC_IP}/privkey.pem"

BASE_COMPOSE=(
  docker compose
  -f docker-compose.yml
  -f docker-compose.ml.yml
)

TLS_COMPOSE=(
  docker compose
  -f docker-compose.yml
  -f docker-compose.ml.yml
  -f docker-compose.admin-tls.yml
)

log() {
  printf '\n[%s] %s\n' "rosmol-admin-https" "$1"
}

fail() {
  printf '\n[%s] ERROR: %s\n' "rosmol-admin-https" "$1" >&2
  return 1
}

set_env_value() {
  local key="$1"
  local value="$2"

  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
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

  "${BASE_COMPOSE[@]}" --profile ml up -d --force-recreate nginx
}

main() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "запусти скрипт от root"
    return 1
  fi

  cd "$PROJECT_DIR" || {
    fail "не найден каталог ${PROJECT_DIR}"
    return 1
  }

  if [ ! -f .env ]; then
    fail "не найден серверный .env"
    return 1
  fi

  install -d -m 0700 data/private/letsencrypt || return 1
  install -d -m 0755 data/private/acme-webroot/.well-known/acme-challenge || return 1
  rm -f data/private/letsencrypt/.force-http

  set_env_value NGINX_TLS_BIND 0.0.0.0 || return 1
  set_env_value NGINX_TLS_HOST_PORT 443 || return 1

  log "Включаю ACME webroot; приложение, routing и KB не перезапускаются"
  "${BASE_COMPOSE[@]}" --profile ml up -d --force-recreate nginx || return 1

  local probe_name="rosmol-admin-acme-probe"
  local probe_value="rosmol-admin-acme-ok"
  printf '%s' "$probe_value" > "data/private/acme-webroot/.well-known/acme-challenge/${probe_name}"

  local observed_probe
  observed_probe="$(curl -fsS --max-time 20 "http://${PUBLIC_IP}/.well-known/acme-challenge/${probe_name}")" || {
    fail "публичный HTTP-01 challenge недоступен"
    return 1
  }

  if [ "$observed_probe" != "$probe_value" ]; then
    fail "HTTP-01 challenge вернул неожиданный ответ"
    return 1
  fi

  rm -f "data/private/acme-webroot/.well-known/acme-challenge/${probe_name}"

  log "Получаю доверенный короткоживущий сертификат Let's Encrypt для IP"
  "${TLS_COMPOSE[@]}" --profile ml --profile tls run --rm --no-deps certbot certonly \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email \
    --preferred-profile shortlived \
    --webroot \
    --webroot-path /var/www/certbot \
    --cert-name "$PUBLIC_IP" \
    --ip-address "$PUBLIC_IP" || return 1

  if [ ! -s "$CERTIFICATE" ] || [ ! -s "$PRIVATE_KEY" ]; then
    fail "Certbot не создал ожидаемые файлы сертификата"
    return 1
  fi

  log "Проверяю автоматическое продление через Let's Encrypt staging"
  "${TLS_COMPOSE[@]}" --profile ml --profile tls run --rm --no-deps certbot renew \
    --dry-run \
    --no-random-sleep-on-renew || return 1

  log "Проверяю TLS-конфигурацию до переключения рабочего Nginx"
  "${BASE_COMPOSE[@]}" --profile ml run --rm --no-deps nginx nginx -t || return 1

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
  curl -fsS --max-time 20 -o /dev/null "https://${PUBLIC_IP}/admin/kb" || return 1

  log "Готово: https://${PUBLIC_IP}/admin/kb работает без SSH-туннеля"
}

main "$@"
