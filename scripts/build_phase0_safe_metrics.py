from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval import social_ticket_benchmark as phase0


def build_phase0_safe_metrics(
    *,
    manifest_path: Path,
    ask_report_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Write a query/response-free Phase 0 projection before billing arrives."""

    manifest = _read_object(manifest_path, label="Phase 0 manifest")
    report = _read_object(ask_report_path, label="Phase 0 ask report")
    phase0_run = report.get("phase0_run")
    if not isinstance(phase0_run, Mapping) or any(
        (
            phase0_run.get("completed") is not True,
            phase0_run.get("manifest_file_sha256")
            != _file_sha256(manifest_path),
            phase0_run.get("transport_mode") != "server_local",
        )
    ):
        raise ValueError(
            "Phase 0 report is not a completed server-local run bound to the manifest"
        )

    safe = phase0.build_safe_phase0_metrics(manifest, report)
    safe["billing_handoff"] = {
        "status": "pending",
        "approval_id": phase0_run.get("approval_id"),
        "eval_run_id": report.get("eval_run_id"),
        "runtime_git_sha": phase0_run.get("runtime_git_sha"),
        "cases_file_sha256": phase0_run.get("cases_file_sha256"),
        "run_started_at": report.get("run_started_at"),
        "run_completed_at": report.get("run_completed_at"),
        "runner_estimated_rub": report.get("llm_estimated_cost_rub"),
        "hard_cap_rub": phase0.PHASE0_COST_CAP_RUB,
    }
    _write_exclusive(output_path, safe)
    return safe


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError("safe Phase 0 output must be absent")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a query/response-free preliminary Phase 0 metrics file."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ask-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    safe = build_phase0_safe_metrics(
        manifest_path=args.manifest,
        ask_report_path=args.ask_report,
        output_path=args.output,
    )
    joint = ((safe.get("phase0_gate") or {}).get("joint_bypass") or {})
    print(f"SAFE_REPORT={args.output.resolve()}")
    print(f"SHA256={_file_sha256(args.output)}")
    print(f"JOINT_BYPASS_RATE={joint.get('post_stratified_rate')}")
    print("BILLING_RECONCILIATION=pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
