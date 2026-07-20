from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.release_provenance import build_release_provenance, valid_git_sha

DEFAULT_OUTPUT_DIR = Path("reports/final_acceptance")
DEFAULT_QUALITY_OUTPUT_DIR = Path("reports/pre_pilot_quality_suite")
DEFAULT_KB_SEED_PATH = Path("data/knowledge_base_seed.json")
DEFAULT_READY_URLS = (
    "http://localhost:8080/ready",
    "http://localhost:8001/ready",
)
DEFAULT_RUFF_TIMEOUT_SEC = 120
DEFAULT_PYTEST_TIMEOUT_SEC = 240
DEFAULT_KB_TIMEOUT_SEC = 120
DEFAULT_READY_TIMEOUT_SEC = 30
DEFAULT_QUALITY_TIMEOUT_SEC = 900


@dataclass
class StepResult:
    name: str
    ok: bool
    elapsed_sec: float
    command: list[str] | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str = ""
    skipped: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local pre-pilot acceptance gate and write a compact report.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--quality-output-dir", default=str(DEFAULT_QUALITY_OUTPUT_DIR))
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--kb-seed", default=str(DEFAULT_KB_SEED_PATH))
    parser.add_argument("--expected-git-sha", default="")
    parser.add_argument("--max-llm-cost-rub", type=float, default=80.0)
    parser.add_argument("--ready-url", action="append", dest="ready_urls")
    parser.add_argument("--ruff-timeout-sec", type=int, default=DEFAULT_RUFF_TIMEOUT_SEC)
    parser.add_argument("--pytest-timeout-sec", type=int, default=DEFAULT_PYTEST_TIMEOUT_SEC)
    parser.add_argument("--kb-timeout-sec", type=int, default=DEFAULT_KB_TIMEOUT_SEC)
    parser.add_argument("--ready-timeout-sec", type=int, default=DEFAULT_READY_TIMEOUT_SEC)
    parser.add_argument("--quality-timeout-sec", type=int, default=DEFAULT_QUALITY_TIMEOUT_SEC)
    parser.add_argument("--skip-ruff", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--skip-ready", action="store_true")
    parser.add_argument("--skip-quality", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    kb_seed_path = Path(args.kb_seed)
    release_run_id = f"acceptance-{uuid4()}"
    expected_git_sha = args.expected_git_sha.strip()
    expected_git_sha_valid = valid_git_sha(expected_git_sha)
    provenance = build_release_provenance(
        release_run_id=release_run_id,
        target=args.target,
        kb_seed_path=kb_seed_path,
        expected_git_sha=expected_git_sha or None,
    )

    steps: list[StepResult] = []
    if args.skip_ruff:
        steps.append(_skipped_step("ruff"))
    else:
        steps.append(
            _run_command("ruff", [_ruff_command(), "check", "."], timeout_sec=args.ruff_timeout_sec)
        )
    if args.skip_pytest:
        steps.append(_skipped_step("pytest"))
    else:
        steps.append(
            _run_command(
                "pytest",
                [sys.executable, "-m", "pytest"],
                timeout_sec=args.pytest_timeout_sec,
            )
        )

    steps.append(
        _run_command(
            "kb_validation",
            [
                sys.executable,
                "scripts/index_kb.py",
                "--validate-only",
                "--path",
                str(kb_seed_path),
            ],
            timeout_sec=args.kb_timeout_sec,
        )
    )

    if args.skip_ready:
        steps.append(_skipped_step("readiness"))
    else:
        ready_urls = tuple(args.ready_urls or DEFAULT_READY_URLS)
        steps.append(
            _check_ready(
                ready_urls,
                timeout_sec=args.ready_timeout_sec,
                expected_git_sha=expected_git_sha,
            )
        )

    quality_summary: dict[str, Any] | None = None
    if args.skip_quality:
        steps.append(_skipped_step("pre_pilot_quality_suite"))
    else:
        quality_output_dir = Path(args.quality_output_dir)
        quality_cmd = [
            sys.executable,
            "-m",
            "eval.run_pre_pilot_quality_suite",
            "--target",
            args.target,
            "--output-dir",
            str(quality_output_dir),
            "--max-llm-cost-rub",
            str(args.max_llm_cost_rub),
            "--kb-seed",
            str(kb_seed_path),
            "--release-run-id",
            release_run_id,
        ]
        if expected_git_sha:
            quality_cmd.extend(["--expected-git-sha", expected_git_sha])
        quality_result = _run_command(
            "pre_pilot_quality_suite",
            quality_cmd,
            timeout_sec=args.quality_timeout_sec,
        )
        steps.append(quality_result)
        summary_path = quality_output_dir / "summary.json"
        if summary_path.exists():
            quality_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    quality_provenance_match, quality_provenance_errors = _quality_provenance_matches(
        quality_summary,
        release_run_id=release_run_id,
        target=args.target,
        provenance=provenance,
        expected_git_sha=expected_git_sha,
    )
    skipped_steps = [step.name for step in steps if step.skipped]

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "release_run_id": release_run_id,
        "expected_git_sha": expected_git_sha or None,
        "expected_git_sha_valid": expected_git_sha_valid,
        "passed": all(step.ok for step in steps)
        and not skipped_steps
        and expected_git_sha_valid
        and quality_summary is not None
        and quality_summary.get("passed") is True
        and quality_provenance_match
        and provenance.get("complete") is True,
        "target": args.target,
        "provenance": provenance,
        "skipped_steps": skipped_steps,
        "quality_provenance_match": quality_provenance_match,
        "quality_provenance_errors": quality_provenance_errors,
        "steps": [asdict(step) for step in steps],
        "quality_summary": quality_summary,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_markdown(output_dir / "summary.md", report)
    print(json.dumps(_compact_report(report, output_dir=output_dir), ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


def _ruff_command() -> str:
    sibling = Path(sys.executable).with_name("ruff.exe" if sys.platform == "win32" else "ruff")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("ruff")
    if found:
        return found
    return "ruff"


def _skipped_step(name: str) -> StepResult:
    return StepResult(
        name=name,
        ok=False,
        skipped=True,
        elapsed_sec=0.0,
        error="skipped_by_cli",
    )


def _quality_provenance_matches(
    quality_summary: dict[str, Any] | None,
    *,
    release_run_id: str,
    target: str,
    provenance: dict[str, Any],
    expected_git_sha: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(quality_summary, dict):
        return False, ["quality_summary_missing"]
    quality_provenance = quality_summary.get("provenance")
    if not isinstance(quality_provenance, dict):
        return False, ["quality_provenance_missing"]

    if quality_summary.get("release_run_id") != release_run_id:
        errors.append("release_run_id_mismatch")
    if quality_summary.get("target") != target:
        errors.append("target_mismatch")
    if not expected_git_sha:
        errors.append("expected_git_sha_missing")
    elif not valid_git_sha(expected_git_sha):
        errors.append("expected_git_sha_invalid")
    else:
        if provenance.get("git_sha") != expected_git_sha:
            errors.append("acceptance_git_sha_mismatch")
        if provenance.get("expected_git_sha") != expected_git_sha:
            errors.append("acceptance_expected_git_sha_mismatch")
        if quality_summary.get("expected_git_sha") != expected_git_sha:
            errors.append("quality_expected_git_sha_mismatch")
        if quality_provenance.get("git_sha") != expected_git_sha:
            errors.append("quality_git_sha_mismatch")
        if quality_provenance.get("expected_git_sha") != expected_git_sha:
            errors.append("quality_provenance_expected_git_sha_mismatch")
    if quality_provenance.get("complete") is not True:
        errors.append("quality_provenance_incomplete")
    if quality_provenance.get("git_sha") != provenance.get("git_sha"):
        errors.append("git_sha_mismatch")

    quality_kb = quality_provenance.get("kb_seed") or {}
    current_kb = provenance.get("kb_seed") or {}
    if quality_kb.get("sha256") != current_kb.get("sha256"):
        errors.append("kb_seed_hash_mismatch")

    case_files = quality_provenance.get("case_files")
    if not isinstance(case_files, dict) or not case_files:
        errors.append("case_hashes_missing")
    elif any(not _valid_sha256((item or {}).get("sha256")) for item in case_files.values()):
        errors.append("case_hash_invalid")
    return not errors, errors


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _run_command(name: str, command: list[str], *, timeout_sec: int) -> StepResult:
    started = perf_counter()
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        return StepResult(
            name=name,
            ok=completed.returncode == 0,
            elapsed_sec=round(perf_counter() - started, 3),
            command=command,
            stdout_tail=_tail(completed.stdout),
            stderr_tail=_tail(completed.stderr),
            error="" if completed.returncode == 0 else f"exit_code={completed.returncode}",
        )
    except Exception as exc:
        return StepResult(
            name=name,
            ok=False,
            elapsed_sec=round(perf_counter() - started, 3),
            command=command,
            error=f"{type(exc).__name__}: {exc}",
        )


def _check_ready(
    urls: tuple[str, ...],
    *,
    timeout_sec: int = DEFAULT_READY_TIMEOUT_SEC,
    expected_git_sha: str,
) -> StepResult:
    started = perf_counter()
    checks: list[dict[str, Any]] = []
    ok = True
    for url in urls:
        try:
            with urlopen(url, timeout=timeout_sec) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            reported_git_sha = payload.get("release_git_sha")
            item_ok = (
                response.status == 200
                and payload.get("status") == "ready"
                and valid_git_sha(expected_git_sha)
                and reported_git_sha == expected_git_sha
            )
            checks.append(
                {
                    "url": url,
                    "status_code": response.status,
                    "ok": item_ok,
                    "expected_git_sha": expected_git_sha,
                    "reported_git_sha": reported_git_sha,
                    "body": payload,
                }
            )
            ok = ok and item_ok
        except (OSError, URLError, json.JSONDecodeError) as exc:
            ok = False
            checks.append({"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return StepResult(
        name="readiness",
        ok=ok,
        elapsed_sec=round(perf_counter() - started, 3),
        stdout_tail=json.dumps(checks, ensure_ascii=False, indent=2),
    )


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Final Acceptance Report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Release run: `{report['release_run_id']}`",
        f"- Expected Git SHA: `{report.get('expected_git_sha')}`",
        f"- Git SHA: `{(report.get('provenance') or {}).get('git_sha')}`",
        f"- Target: `{report['target']}`",
        f"- Passed: `{report['passed']}`",
        f"- Skipped steps: `{', '.join(report.get('skipped_steps') or []) or '-'}`",
        f"- Quality provenance match: `{report.get('quality_provenance_match')}`",
        "",
        "## Steps",
        "",
        "| Step | Status | Time, sec | Notes |",
        "|---|---:|---:|---|",
    ]
    for step in report["steps"]:
        notes = step.get("error") or _first_line(step.get("stdout_tail") or "")
        status = "SKIP" if step.get("skipped") else ("OK" if step["ok"] else "FAIL")
        lines.append(
            f"| `{step['name']}` | {status} | "
            f"{step['elapsed_sec']} | {_escape_table(notes)} |"
        )

    quality = report.get("quality_summary") or {}
    if quality:
        lines.extend(
            [
                "",
                "## Quality Suite",
                "",
                f"- Passed: `{quality.get('passed')}`",
                f"- Cost, RUB: `{quality.get('llm_estimated_cost_rub')}`",
                f"- Completed sections: `{', '.join(quality.get('completed_sections') or [])}`",
                "",
                "| Section | Cases/Turns | Pass | Trace | Sources | Cost RUB |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for name, section in (quality.get("sections") or {}).items():
            cases = section.get("cases_total") or section.get("turns_total")
            pass_rate = section.get("pass_rate", section.get("turn_pass_rate"))
            lines.append(
                f"| `{name}` | {cases} | {_pct(pass_rate)} | "
                f"{_pct(section.get('trace_coverage_rate'))} | "
                f"{_pct(section.get('expected_or_equivalent_chunk_hit_rate'))} | "
                f"{section.get('llm_estimated_cost_rub')} |"
            )

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _compact_report(
    report: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    quality = report.get("quality_summary") or {}
    return {
        "passed": report["passed"],
        "release_run_id": report.get("release_run_id"),
        "expected_git_sha": report.get("expected_git_sha"),
        "expected_git_sha_valid": report.get("expected_git_sha_valid"),
        "steps": {step["name"]: step["ok"] for step in report["steps"]},
        "skipped_steps": report.get("skipped_steps"),
        "quality_provenance_match": report.get("quality_provenance_match"),
        "quality_passed": quality.get("passed"),
        "quality_cost_rub": quality.get("llm_estimated_cost_rub"),
        "quality_sections": quality.get("completed_sections"),
        "report": str(output_dir / "summary.md").replace("\\", "/"),
    }


def _tail(value: str, *, max_chars: int = 4000) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else ""


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")[:200]


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


if __name__ == "__main__":
    main()
