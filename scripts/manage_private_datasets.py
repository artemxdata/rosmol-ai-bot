from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.security.private_dataset_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    PRIVATE_DATA_ROOT,
    build_retention_plan,
    complete_human_review,
    dataset_ref,
    freeze_dataset,
    inventory_private_datasets,
    load_registry,
    register_dataset,
    save_registry,
    start_review,
    supersede_dataset,
    validate_registry,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manage the local private dataset registry. This tool never deletes datasets."
        )
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="Registry JSON under data/private.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="Validate the registry without changing it.")
    inventory = subparsers.add_parser(
        "inventory",
        help="Read directory metadata only; do not register, move, or delete anything.",
    )
    inventory.add_argument(
        "--area",
        action="append",
        choices=("tickets", "operator_qa", "yonote", "eval"),
        help="Area to inventory; repeat as needed.",
    )

    register = subparsers.add_parser("register", help="Register one draft dataset entry.")
    register.add_argument("--entry", type=Path, required=True)
    register.add_argument("--at", type=_timestamp)

    review = subparsers.add_parser("review", help="Move a draft dataset to reviewing.")
    review.add_argument("dataset_ref")
    review.add_argument("--at", type=_timestamp)

    complete_review = subparsers.add_parser(
        "complete-review",
        help="Validate and bind one sealed private GoldTicket JSONL artifact.",
    )
    complete_review.add_argument("dataset_ref")
    complete_review.add_argument("--gold-artifact", type=Path, required=True)
    complete_review.add_argument("--at", type=_timestamp)

    freeze = subparsers.add_parser("freeze", help="Freeze an immutable dataset version.")
    freeze.add_argument("dataset_ref")
    freeze.add_argument("--at", type=_timestamp)

    supersede = subparsers.add_parser(
        "supersede",
        help="Mark a frozen version superseded by another frozen version.",
    )
    supersede.add_argument("dataset_ref")
    supersede.add_argument("successor_ref")
    supersede.add_argument("--at", type=_timestamp)

    retention = subparsers.add_parser(
        "retention-plan",
        help="Preview retention candidates and blockers; never delete files.",
    )
    retention.add_argument("--as-of", type=_timestamp)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    registry_path = args.registry
    if args.command == "inventory":
        registry = load_registry(registry_path, allow_missing=True)
        report = inventory_private_datasets(
            PRIVATE_DATA_ROOT,
            registry=registry,
            areas=args.area or ("tickets", "operator_qa", "yonote", "eval"),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0

    registry = load_registry(
        registry_path,
        allow_missing=args.command == "register",
    )
    if args.command == "validate":
        normalized = validate_registry(registry)
        _print_result(
            {
                "valid": True,
                "datasets": len(normalized["datasets"]),
                "registry": _display_path(registry_path),
            }
        )
        return 0
    if args.command == "retention-plan":
        plan = build_retention_plan(registry, as_of=args.as_of)
        print(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    if args.command == "register":
        entry = _read_entry(args.entry)
        updated = register_dataset(registry, entry, now=args.at)
        changed_ref = dataset_ref(entry)
        changed_state = "draft"
    elif args.command == "review":
        updated = start_review(registry, args.dataset_ref, now=args.at)
        changed_ref = args.dataset_ref
        changed_state = "reviewing"
    elif args.command == "complete-review":
        updated = complete_human_review(
            registry,
            args.dataset_ref,
            gold_artifact_path=args.gold_artifact,
            private_root=PRIVATE_DATA_ROOT,
            now=args.at,
        )
        changed_ref = args.dataset_ref
        changed_state = "reviewing:complete"
    elif args.command == "freeze":
        updated = freeze_dataset(
            registry,
            args.dataset_ref,
            private_root=PRIVATE_DATA_ROOT,
            now=args.at,
        )
        changed_ref = args.dataset_ref
        changed_state = "frozen"
    elif args.command == "supersede":
        updated = supersede_dataset(
            registry,
            args.dataset_ref,
            args.successor_ref,
            now=args.at,
        )
        changed_ref = args.dataset_ref
        changed_state = "superseded"
    else:  # pragma: no cover - argparse owns the command choices.
        raise AssertionError(f"unsupported command: {args.command}")

    save_registry(registry_path, updated)
    _print_result(
        {
            "dataset_ref": changed_ref,
            "state": changed_state,
            "registry": _display_path(registry_path),
            "deletion_performed": False,
        }
    )
    return 0


def _read_entry(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("entry spec must be a regular JSON file")
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            value = json.load(
                source,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("entry spec must be readable UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("entry spec must contain a JSON object")
    return value


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _display_path(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=False)
    project_root = Path(__file__).resolve().parents[1]
    if resolved.is_relative_to(project_root):
        return resolved.relative_to(project_root).as_posix()
    return resolved.name


def _print_result(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
