from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, urlsplit

DEFAULT_TEMPLATE = Path(".env.production.example")
DEFAULT_OUTPUT = Path(".env.production")
DEFAULT_EGRESS_PROXY_OUTPUT = Path("data/private/runtime-egress/squid.conf")
MIN_SECRET_LENGTH = 32
GENERATED_SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "QDRANT_API_KEY",
    "API_AUTH_TOKEN",
    "WEBHOOK_AUTH_TOKEN",
    "ADMIN_AUTH_TOKEN",
    "USER_HASH_SECRET",
    "HDE_TRIGGER_PREFIX",
    "HDE_TRANSPORT_EVENT_KEY_SECRET",
    "HDE_TRANSPORT_ENCRYPTION_KEY",
)
REQUIRED_EXTERNAL_KEYS = (
    "RELEASE_GIT_SHA",
    "HDE_BASE_URL",
    "HDE_API_EMAIL",
    "HDE_API_KEY",
    "CLOUD_RU_API_KEY",
    "CLOUD_RU_CHAT_COMPLETIONS_URL",
    "ADMIN_PUBLIC_HOST",
    "CERTBOT_EMAIL",
)
PLACEHOLDER_PATTERNS = (
    re.compile(r"replace[-_ ]?with", re.IGNORECASE),
    re.compile(r"change[-_ ]?me", re.IGNORECASE),
    re.compile(r"example\.test", re.IGNORECASE),
    re.compile(r"(?:^|[^a-z])todo(?:[^a-z]|$)", re.IGNORECASE),
    re.compile(r"^<[^>]+>$"),
)
SAFE_GENERATED_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")
EXTERNAL_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)
CLOUD_RU_ENDPOINT_HOST = "foundation-models.api.cloud.ru"
CLOUD_RU_ENDPOINT_PATH = "/v1/chat/completions"
HDE_ENDPOINT_HOST = "rosmolodezh.helpdeskeddy.com"
YONOTE_ENDPOINT_HOST = "rossmol.yonote.ru"
FULL_RELEASE_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
PRODUCTION_ADMIN_KB_SEED_PATH = (
    "/app/data/private/admin-kb/knowledge_base_seed.json"
)


class EnvValidationError(ValueError):
    pass


def _parse_env(text: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            errors.append(f"line_{line_number}: invalid_assignment")
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            errors.append(f"line_{line_number}: invalid_key")
            continue
        if key in values:
            errors.append(f"{key}: duplicate_key")
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values, errors


def _is_placeholder(value: str) -> bool:
    if not value.strip():
        return True
    return any(pattern.search(value) for pattern in PLACEHOLDER_PATTERNS)


def _new_secret() -> str:
    return secrets.token_urlsafe(48)


def _render_generated_env(template_text: str) -> tuple[str, tuple[str, ...]]:
    template_values, parse_errors = _parse_env(template_text)
    if parse_errors:
        raise EnvValidationError("template_invalid: " + ", ".join(parse_errors))

    generated: dict[str, str] = {}
    for key in GENERATED_SECRET_KEYS:
        if key not in template_values:
            continue
        generated[key] = _new_secret()

    required_generated = set(GENERATED_SECRET_KEYS)
    missing = sorted(required_generated - generated.keys())
    if missing:
        raise EnvValidationError("template_missing_generated_keys: " + ", ".join(missing))

    postgres_user = template_values.get("POSTGRES_USER", "rosmol")
    postgres_db = template_values.get("POSTGRES_DB", "rosmol_ai_bot")
    password = generated["POSTGRES_PASSWORD"]
    generated["POSTGRES_DSN"] = (
        "postgresql://"
        f"{quote(postgres_user, safe='')}:{quote(password, safe='')}"
        f"@postgres:5432/{quote(postgres_db, safe='')}"
    )
    redis_password = generated["REDIS_PASSWORD"]
    generated["REDIS_URL"] = (
        f"redis://:{quote(redis_password, safe='')}@redis:6379/0"
    )

    rendered_lines: list[str] = []
    seen: set[str] = set()
    for raw_line in template_text.splitlines(keepends=True):
        match = re.match(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Z][A-Z0-9_]*)=", raw_line)
        if match is None or match.group("key") not in generated:
            rendered_lines.append(raw_line)
            continue
        key = match.group("key")
        newline = "\r\n" if raw_line.endswith("\r\n") else "\n" if raw_line.endswith("\n") else ""
        rendered_lines.append(f"{match.group('prefix')}{key}={generated[key]}{newline}")
        seen.add(key)

    missing_rendered = sorted(generated.keys() - seen)
    if missing_rendered:
        raise EnvValidationError("template_render_failed: " + ", ".join(missing_rendered))
    return "".join(rendered_lines), tuple(sorted(generated))


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def initialize_env(template: Path, output: Path) -> tuple[str, ...]:
    template_text = template.read_text(encoding="utf-8")
    rendered, generated_keys = _render_generated_env(template_text)
    _atomic_private_write(output, rendered)
    return generated_keys


def update_release_git_sha(path: Path, git_sha: str) -> None:
    normalized_sha = git_sha.strip()
    if not FULL_RELEASE_GIT_SHA.fullmatch(normalized_sha) or normalized_sha == "0" * 40:
        raise EnvValidationError("release_git_sha_invalid")
    if path.is_symlink():
        raise EnvValidationError("env_file_must_not_be_symlink")
    current_errors = validate_env(path)
    if current_errors:
        raise EnvValidationError(
            "existing_production_env_invalid: " + ", ".join(current_errors)
        )

    original = path.read_text(encoding="utf-8")
    values, parse_errors = _parse_env(original)
    if parse_errors or "RELEASE_GIT_SHA" not in values:
        raise EnvValidationError("release_git_sha_field_missing")
    if values["RELEASE_GIT_SHA"] == normalized_sha:
        raise EnvValidationError("release_git_sha_must_change")

    rendered: list[str] = []
    replacements = 0
    for raw_line in original.splitlines(keepends=True):
        match = re.match(
            r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>RELEASE_GIT_SHA)=",
            raw_line,
        )
        if match is None:
            rendered.append(raw_line)
            continue
        newline = "\r\n" if raw_line.endswith("\r\n") else "\n" if raw_line.endswith("\n") else ""
        rendered.append(f"{match.group('prefix')}RELEASE_GIT_SHA={normalized_sha}{newline}")
        replacements += 1
    if replacements != 1:
        raise EnvValidationError("release_git_sha_field_not_unique")

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        _atomic_private_write(temporary, "".join(rendered))
        replacement_errors = validate_env(temporary)
        if replacement_errors:
            raise EnvValidationError(
                "updated_production_env_invalid: " + ", ".join(replacement_errors)
            )
        os.replace(temporary, path)
        if os.name == "posix":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_https_url(key: str, value: str, errors: list[str]) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        errors.append(f"{key}: must_be_https_url_without_credentials")
        return

    host = parsed.hostname or ""
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        errors.append(f"{key}: must_be_https_url_without_credentials")
        return

    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        errors.append(f"{key}: must_use_external_dns_name")
        return
    if not EXTERNAL_DNS_NAME.fullmatch(host):
        errors.append(f"{key}: must_use_external_dns_name")


def _validate_provider_endpoint_binding(
    key: str,
    value: str,
    errors: list[str],
) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if key == "CLOUD_RU_CHAT_COMPLETIONS_URL":
        if host != CLOUD_RU_ENDPOINT_HOST or parsed.path != CLOUD_RU_ENDPOINT_PATH:
            errors.append(f"{key}: must_match_reviewed_cloud_ru_endpoint")
    elif key == "HDE_BASE_URL":
        if host != HDE_ENDPOINT_HOST or parsed.path not in {"", "/"}:
            errors.append(f"{key}: must_match_reviewed_hde_tenant_endpoint")
    elif key == "YONOTE_BASE_URL":
        if host != YONOTE_ENDPOINT_HOST or parsed.path not in {"", "/"}:
            errors.append(f"{key}: must_match_reviewed_yonote_endpoint")


def _runtime_egress_endpoint_keys(values: dict[str, str]) -> tuple[str, ...]:
    keys = ["HDE_BASE_URL", "CLOUD_RU_CHAT_COMPLETIONS_URL"]
    if values.get("YONOTE_SYNC_ENABLED", "").strip().casefold() == "true":
        keys.append("YONOTE_BASE_URL")
    return tuple(keys)


def _runtime_egress_proxy_config(values: dict[str, str]) -> str:
    hosts: set[str] = set()
    for key in _runtime_egress_endpoint_keys(values):
        value = values.get(key, "")
        url_errors: list[str] = []
        _validate_https_url(key, value, url_errors)
        if url_errors:
            raise EnvValidationError(
                "runtime_egress_endpoint_invalid: " + ", ".join(url_errors)
            )
        hostname = urlsplit(value).hostname
        if hostname is None:  # pragma: no cover - guarded by _validate_https_url
            raise EnvValidationError(f"runtime_egress_endpoint_invalid: {key}")
        hosts.add(hostname.casefold())

    destinations = " ".join(sorted(hosts))
    return (
        "# Generated from validated production endpoint hostnames. No credentials.\n"
        "visible_hostname runtime-egress-proxy\n"
        "http_port 3128\n"
        "acl SSL_ports port 443\n"
        "acl CONNECT method CONNECT\n"
        f"acl runtime_destinations dstdomain {destinations}\n"
        "pinger_enable off\n"
        "cache_mem 16 MB\n"
        "maximum_object_size_in_memory 512 KB\n"
        "http_access deny !CONNECT\n"
        "http_access deny CONNECT !SSL_ports\n"
        "http_access allow CONNECT runtime_destinations\n"
        "http_access deny all\n"
        "cache deny all\n"
        "access_log stdio:/var/log/squid/access.log\n"
        "cache_log /var/log/squid/cache.log\n"
        "logfile_rotate 0\n"
        "forwarded_for delete\n"
    )


def generate_runtime_egress_proxy_config(env_path: Path, output: Path) -> int:
    validation_errors = validate_env(env_path)
    if validation_errors:
        raise EnvValidationError(
            "production_env_invalid: " + ", ".join(validation_errors)
        )
    values, parse_errors = _parse_env(env_path.read_text(encoding="utf-8"))
    if parse_errors:  # pragma: no cover - validate_env already returns these errors
        raise EnvValidationError("production_env_invalid")
    config = _runtime_egress_proxy_config(values)
    _atomic_private_write(output, config)
    return len(
        {
            urlsplit(values[key]).hostname.casefold()
            for key in _runtime_egress_endpoint_keys(values)
            if urlsplit(values[key]).hostname
        }
    )


def _validate_postgres(values: dict[str, str], errors: list[str]) -> None:
    dsn = values.get("POSTGRES_DSN", "")
    parsed = urlsplit(dsn)
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or parsed.hostname != "postgres"
        or parsed.port != 5432
        or parsed.username != values.get("POSTGRES_USER")
        or parsed.password != values.get("POSTGRES_PASSWORD")
        or parsed.path.lstrip("/") != values.get("POSTGRES_DB")
        or parsed.query
        or parsed.fragment
    ):
        errors.append("POSTGRES_DSN: must_match_internal_postgres_credentials")


def _validate_redis(values: dict[str, str], errors: list[str]) -> None:
    parsed = urlsplit(values.get("REDIS_URL", ""))
    if (
        parsed.scheme != "redis"
        or parsed.hostname != "redis"
        or parsed.port != 6379
        or parsed.username not in {None, ""}
        or parsed.password != values.get("REDIS_PASSWORD")
        or parsed.path != "/0"
        or parsed.query
        or parsed.fragment
    ):
        errors.append("REDIS_URL: must_match_internal_redis_credentials")


def validate_env(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ["env_file: unreadable"]
    values, errors = _parse_env(text)

    if values.get("APP_ENV") != "production":
        errors.append("APP_ENV: must_equal_production")

    admin_read_only = values.get("ADMIN_READ_ONLY", "true").strip().casefold()
    admin_mutations_enabled = (
        values.get("ADMIN_MUTATIONS_ENABLED", "false").strip().casefold()
    )
    if admin_read_only not in {"true", "false"}:
        errors.append("ADMIN_READ_ONLY: must_be_true_or_false")
    if admin_mutations_enabled not in {"true", "false"}:
        errors.append("ADMIN_MUTATIONS_ENABLED: must_be_true_or_false")
    if admin_read_only == "false":
        if admin_mutations_enabled != "true":
            errors.append(
                "ADMIN_MUTATIONS_ENABLED: must_be_true_when_admin_is_writable"
            )
        if (
            values.get("ADMIN_KB_SEED_PATH", "").strip()
            != PRODUCTION_ADMIN_KB_SEED_PATH
        ):
            errors.append(
                "ADMIN_KB_SEED_PATH: must_use_isolated_private_workspace"
            )
    elif admin_mutations_enabled == "true":
        errors.append(
            "ADMIN_MUTATIONS_ENABLED: must_be_false_when_admin_is_read_only"
        )

    required_generated = list(GENERATED_SECRET_KEYS)
    for key in required_generated:
        value = values.get(key, "")
        if _is_placeholder(value):
            errors.append(f"{key}: missing_or_placeholder")
        elif len(value) < MIN_SECRET_LENGTH:
            errors.append(f"{key}: too_short")
        elif not SAFE_GENERATED_VALUE.fullmatch(value):
            errors.append(f"{key}: unsafe_characters")

    present_secret_values = [values[key] for key in required_generated if values.get(key)]
    if len(present_secret_values) != len(set(present_secret_values)):
        errors.append("generated_secrets: values_must_be_distinct")

    for key in REQUIRED_EXTERNAL_KEYS:
        value = values.get(key, "")
        if _is_placeholder(value):
            errors.append(f"{key}: missing_or_placeholder")

    for key in ("HDE_API_KEY", "CLOUD_RU_API_KEY"):
        value = values.get(key, "")
        if value and not _is_placeholder(value) and len(value) < MIN_SECRET_LENGTH:
            errors.append(f"{key}: too_short")

    for key in ("HDE_BASE_URL", "CLOUD_RU_CHAT_COMPLETIONS_URL"):
        value = values.get(key, "")
        if value and not _is_placeholder(value):
            issue_count = len(errors)
            _validate_https_url(key, value, errors)
            if len(errors) == issue_count:
                _validate_provider_endpoint_binding(key, value, errors)

    yonote_flag = values.get("YONOTE_SYNC_ENABLED", "").strip().casefold()
    if yonote_flag not in {"", "false", "true"}:
        errors.append("YONOTE_SYNC_ENABLED: must_be_true_or_false")
    yonote_enabled = yonote_flag == "true"
    yonote_token = values.get("YONOTE_API_TOKEN", "").strip()
    yonote_mode = values.get("YONOTE_SYNC_MODE", "manual").strip()
    yonote_collection_names = values.get("YONOTE_COLLECTION_NAMES", "").strip()
    if yonote_enabled:
        if _is_placeholder(yonote_token):
            errors.append("YONOTE_API_TOKEN: missing_or_placeholder")
        if yonote_mode != "manual":
            errors.append("YONOTE_SYNC_MODE: must_equal_manual")
        delimiter = ";" if ";" in yonote_collection_names else "|"
        if not any(item.strip() for item in yonote_collection_names.split(delimiter)):
            errors.append("YONOTE_COLLECTION_NAMES: missing")
        yonote_url = values.get("YONOTE_BASE_URL", "")
        if _is_placeholder(yonote_url):
            errors.append("YONOTE_BASE_URL: missing_or_placeholder")
        else:
            issue_count = len(errors)
            _validate_https_url("YONOTE_BASE_URL", yonote_url, errors)
            if len(errors) == issue_count:
                _validate_provider_endpoint_binding(
                    "YONOTE_BASE_URL",
                    yonote_url,
                    errors,
                )
    elif yonote_token:
        errors.append("YONOTE_API_TOKEN: must_be_empty_when_sync_disabled")

    email_pattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    for key in ("HDE_API_EMAIL", "CERTBOT_EMAIL"):
        value = values.get(key, "")
        if value and not _is_placeholder(value) and not email_pattern.fullmatch(value):
            errors.append(f"{key}: invalid_email")

    host = values.get("ADMIN_PUBLIC_HOST", "")
    if host and not _is_placeholder(host):
        if ":" in host or "/" in host or host.lower() == "localhost" or "." not in host:
            errors.append("ADMIN_PUBLIC_HOST: must_be_dns_name")

    release_git_sha = values.get("RELEASE_GIT_SHA", "")
    if release_git_sha and not _is_placeholder(release_git_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", release_git_sha) or release_git_sha == "0" * 40:
            errors.append("RELEASE_GIT_SHA: must_be_nonzero_full_lowercase_git_sha")

    _validate_postgres(values, errors)
    _validate_redis(values, errors)

    if os.name == "posix":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            errors.append("env_file: stat_failed")
        else:
            if mode & 0o077:
                errors.append("env_file: permissions_must_be_0600")

    return sorted(set(errors))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate local production secrets without printing them, then validate env.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="Create a new private env file.")
    init_parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    init_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    validate_parser = subparsers.add_parser("validate", help="Fail on placeholders or weak config.")
    validate_parser.add_argument("path", type=Path, nargs="?", default=DEFAULT_OUTPUT)
    egress_parser = subparsers.add_parser(
        "render-egress-proxy",
        help="Create a private Squid allowlist from validated provider endpoint hostnames.",
    )
    egress_parser.add_argument("--env-file", type=Path, default=DEFAULT_OUTPUT)
    egress_parser.add_argument("--output", type=Path, default=DEFAULT_EGRESS_PROXY_OUTPUT)
    release_parser = subparsers.add_parser(
        "set-release-sha",
        help="Atomically replace only RELEASE_GIT_SHA in an existing validated private env.",
    )
    release_parser.add_argument("--env-file", type=Path, default=DEFAULT_OUTPUT)
    release_parser.add_argument("--git-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "init":
        try:
            generated_keys = initialize_env(args.template, args.output)
        except FileExistsError:
            print(f"ERROR: refusing to overwrite existing env file: {args.output}", file=sys.stderr)
            return 2
        except (OSError, EnvValidationError) as exc:
            print(f"ERROR: production env was not created: {exc}", file=sys.stderr)
            return 2
        print(f"Created private production env: {args.output}")
        print("Generated local fields: " + ", ".join(generated_keys))
        print(
            "Provider and release identity fields remain unset; fill them securely, "
            "then run the validate command."
        )
        return 0

    if args.command == "render-egress-proxy":
        try:
            destination_count = generate_runtime_egress_proxy_config(
                args.env_file,
                args.output,
            )
        except FileExistsError:
            print(
                f"ERROR: refusing to overwrite existing proxy config: {args.output}",
                file=sys.stderr,
            )
            return 2
        except (OSError, EnvValidationError) as exc:
            print(f"ERROR: runtime egress config was not created: {exc}", file=sys.stderr)
            return 2
        print(f"Created private runtime egress config: {args.output}")
        print(f"Allowed HTTPS destination count: {destination_count}")
        return 0

    if args.command == "set-release-sha":
        try:
            update_release_git_sha(args.env_file, args.git_sha)
        except (OSError, EnvValidationError) as exc:
            print(f"ERROR: production release identity was not updated: {exc}", file=sys.stderr)
            return 2
        print(f"Updated production release identity: {args.env_file}")
        return 0

    errors = validate_env(args.path)
    if errors:
        print(f"Production env validation FAILED ({len(errors)} issue(s)).", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Production env validation passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
