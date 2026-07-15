from __future__ import annotations

from pathlib import Path

PUBLIC_IP = "139.100.225.44"


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_default_nginx_serves_acme_webroot_without_stopping_http() -> None:
    config = _read("nginx/default.conf")

    assert "location ^~ /.well-known/acme-challenge/" in config
    assert "root /var/www/certbot;" in config
    assert "try_files $uri =404;" in config


def test_base_compose_keeps_tls_after_future_routine_deploys() -> None:
    compose = _read("docker-compose.yml")

    assert '${NGINX_TLS_BIND:-127.0.0.1}:${NGINX_TLS_HOST_PORT:-8443}:443' in compose
    assert "./nginx/select-config.sh:/etc/nginx/rosmol/select-config.sh:ro" in compose
    assert "./nginx/admin-tls.conf:/etc/nginx/rosmol/admin-tls.conf:ro" in compose
    assert "./data/private/letsencrypt:/etc/letsencrypt:ro" in compose
    assert "./data/private/acme-webroot:/var/www/certbot:ro" in compose


def test_certbot_compose_uses_ip_capable_version_and_private_rw_mounts() -> None:
    compose = _read("docker-compose.admin-tls.yml")

    assert "certbot/certbot:v5.4.0" in compose
    assert "./data/private/letsencrypt:/etc/letsencrypt" in compose
    assert "./data/private/acme-webroot:/var/www/certbot" in compose
    assert "services:\n  certbot:" in compose
    assert "\n  nginx:" not in compose


def test_nginx_selects_tls_only_when_certificate_and_key_exist() -> None:
    selector = _read("nginx/select-config.sh")

    assert f"live/{PUBLIC_IP}/fullchain.pem" in selector
    assert f"live/{PUBLIC_IP}/privkey.pem" in selector
    assert "[ -s \"$certificate\" ]" in selector
    assert "[ -s \"$private_key\" ]" in selector
    assert "admin-tls.conf" in selector
    assert "default.conf" in selector
    assert 'exec /docker-entrypoint.sh "$@"' in selector


def test_plain_http_redirects_only_admin_page_and_refuses_login_api() -> None:
    config = _read("nginx/admin-tls.conf")
    http_server = config.split("server {\n    listen 443 ssl;", maxsplit=1)[0]

    assert "location = /admin/kb {" in http_server
    assert f"return 308 https://{PUBLIC_IP}$request_uri;" in http_server
    assert "location ^~ /admin/kb/ {" in http_server
    assert "return 426;" in http_server
    assert "location /admin/kb {" not in http_server


def test_https_admin_uses_ml_runtime_tls_headers_and_login_rate_limit() -> None:
    config = _read("nginx/admin-tls.conf")
    https_server = "server {\n    listen 443 ssl;" + config.split(
        "server {\n    listen 443 ssl;", maxsplit=1
    )[1]

    assert f"server_name {PUBLIC_IP};" in https_server
    assert f"live/{PUBLIC_IP}/fullchain.pem" in https_server
    assert f"live/{PUBLIC_IP}/privkey.pem" in https_server
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in https_server
    assert 'add_header Strict-Transport-Security "max-age=86400" always;' in https_server
    assert "location = /admin/kb/login {" in https_server
    assert "client_max_body_size 4k;" in https_server
    assert "limit_req zone=admin_login burst=5 nodelay;" in https_server
    assert "limit_req_status 429;" in https_server
    assert "location /admin/kb {" in https_server
    assert "app-ml:8000" in https_server
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in https_server


def test_tls_config_preserves_existing_http_and_https_routing() -> None:
    config = _read("nginx/admin-tls.conf")

    assert config.count("location = /ask {") == 2
    assert config.count("location ^~ /webhook/ {") == 2
    assert config.count("set $ml_backend app-ml:8000;") == 4
    assert config.count("set $app_backend app:8000;") == 2
    assert config.count("resolver 127.0.0.11 valid=10s ipv6=off;") == 2


def test_renewal_timer_is_persistent_and_tests_nginx_before_reload() -> None:
    service = _read("deploy/systemd/rosmol-admin-tls-renew.service")
    timer = _read("deploy/systemd/rosmol-admin-tls-renew.timer")

    assert "certbot renew --quiet --no-random-sleep-on-renew" in service
    assert "TimeoutStartSec=10min" in service
    assert "nginx nginx -t" in service
    assert service.index("nginx nginx -t") < service.index("nginx nginx -s reload")
    assert "OnCalendar=*-*-* 00,12:00:00" in timer
    assert "RandomizedDelaySec=30min" in timer
    assert "Persistent=true" in timer


def test_provision_script_validates_acme_renewal_and_tls_before_switch() -> None:
    script = _read("scripts/provision_admin_https.sh")

    assert f'PUBLIC_IP="{PUBLIC_IP}"' in script
    assert "--preferred-profile shortlived" in script
    assert '--cert-name "$PUBLIC_IP"' in script
    assert '--ip-address "$PUBLIC_IP"' in script
    assert "renew \\\n    --dry-run" in script
    assert "nginx nginx -t" in script
    tls_check = script.index('log "Проверяю TLS-конфигурацию')
    assert script.index("certbot certonly") < tls_check
    assert tls_check < script.index("run --rm --no-deps nginx nginx -t")
    assert "systemctl enable --now rosmol-admin-tls-renew.timer" in script
    assert "NGINX_TLS_BIND 0.0.0.0" in script
    assert "NGINX_TLS_HOST_PORT 443" in script
    assert "set -e" not in script
    assert "logout" not in script
