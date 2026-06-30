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

DEFAULT_OUTPUT_DIR = Path("reports/final_acceptance")
DEFAULT_QUALITY_OUTPUT_DIR = Path("reports/pre_pilot_quality_suite")
DEFAULT_READY_URLS = (
    "http://localhost:8080/ready",
    "http://localhost:8001/ready",
)


@dataclass
class StepResult:
    name: str
    ok: bool
    elapsed_sec: float
    command: list[str] | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str = ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local pre-pilot acceptance gate and write a compact report.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--quality-output-dir", default=str(DEFAULT_QUALITY_OUTPUT_DIR))
    parser.add_argument("--target", default="http://localhost:8001/ask")
    parser.add_argument("--max-llm-cost-rub", type=float, default=80.0)
    parser.add_argument("--ready-url", action="append", dest="ready_urls")
    parser.add_argument("--skip-ruff", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--skip-ready", action="store_true")
    parser.add_argument("--skip-quality", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    steps: list[StepResult] = []
    if not args.skip_ruff:
        steps.append(_run_command("ruff", [_ruff_command(), "check", "."], timeout_sec=120))
    if not args.skip_pytest:
        steps.append(_run_command("pytest", [sys.executable, "-m", "pytest"], timeout_sec=240))

    steps.append(
        _run_command(
            "kb_validation",
            [sys.executable, "scripts/index_kb.py", "--validate-only"],
            timeout_sec=120,
        )
    )

    if not args.skip_ready:
        ready_urls = tuple(args.ready_urls or DEFAULT_READY_URLS)
        steps.append(_check_ready(ready_urls))

    quality_summary: dict[str, Any] | None = None
    if not args.skip_quality:
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
        ]
        quality_result = _run_command(
            "pre_pilot_quality_suite",
            quality_cmd,
            timeout_sec=900,
        )
        steps.append(quality_result)
        summary_path = quality_output_dir / "summary.json"
        if summary_path.exists():
            quality_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": all(step.ok for step in steps)
        and (quality_summary is None or quality_summary.get("passed") is True),
        "target": args.target,
        "steps": [asdict(step) for step in steps],
        "quality_summary": quality_summary,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_markdown(output_dir / "summary.md", report)
    print(json.dumps(_compact_report(report), ensure_ascii=False, indent=2))
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


def _check_ready(urls: tuple[str, ...]) -> StepResult:
    started = perf_counter()
    checks: list[dict[str, Any]] = []
    ok = True
    for url in urls:
        try:
            with urlopen(url, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            item_ok = response.status == 200 and payload.get("status") == "ready"
            checks.append(
                {
                    "url": url,
                    "status_code": response.status,
                    "ok": item_ok,
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
        f"- Target: `{report['target']}`",
        f"- Passed: `{report['passed']}`",
        "",
        "## Steps",
        "",
        "| Step | Status | Time, sec | Notes |",
        "|---|---:|---:|---|",
    ]
    for step in report["steps"]:
        notes = step.get("error") or _first_line(step.get("stdout_tail") or "")
        lines.append(
            f"| `{step['name']}` | {'OK' if step['ok'] else 'FAIL'} | "
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


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    quality = report.get("quality_summary") or {}
    return {
        "passed": report["passed"],
        "steps": {step["name"]: step["ok"] for step in report["steps"]},
        "quality_passed": quality.get("passed"),
        "quality_cost_rub": quality.get("llm_estimated_cost_rub"),
        "quality_sections": quality.get("completed_sections"),
        "report": str(DEFAULT_OUTPUT_DIR / "summary.md").replace("\\", "/"),
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
