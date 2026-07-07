from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path("reports/presentation_readiness")
DEFAULT_FINAL_ACCEPTANCE = Path("reports/final_acceptance/summary.json")
DEFAULT_QUALITY_REPORT = Path("reports/presentation_quality/presentation_quality_report.json")
DEFAULT_PRE_DEMO_SMOKE = Path(
    "reports/presentation_quality/pre_demo_smoke_latest/pre_demo_smoke.json"
)
DEFAULT_QUALITY_MIN_PASS_RATE = 1.0
DEFAULT_SMOKE_MIN_PASS_RATE = 1.0


@dataclass(frozen=True)
class ReportPath:
    key: str
    path: Path
    title: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a compact leadership-demo readiness report from existing "
            "quality gates."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--final-acceptance", default=str(DEFAULT_FINAL_ACCEPTANCE))
    parser.add_argument("--quality-report", default=str(DEFAULT_QUALITY_REPORT))
    parser.add_argument("--pre-demo-smoke", default=str(DEFAULT_PRE_DEMO_SMOKE))
    parser.add_argument(
        "--quality-min-pass-rate",
        type=float,
        default=DEFAULT_QUALITY_MIN_PASS_RATE,
    )
    parser.add_argument("--smoke-min-pass-rate", type=float, default=DEFAULT_SMOKE_MIN_PASS_RATE)
    parser.add_argument(
        "--allow-not-ready",
        action="store_true",
        help="Write the report but do not fail the process when readiness gates are red.",
    )
    args = parser.parse_args()

    report = build_readiness_report(
        output_dir=Path(args.output_dir),
        final_acceptance=Path(args.final_acceptance),
        quality_report=Path(args.quality_report),
        pre_demo_smoke=Path(args.pre_demo_smoke),
        quality_min_pass_rate=args.quality_min_pass_rate,
        smoke_min_pass_rate=args.smoke_min_pass_rate,
    )
    print(json.dumps(_compact_report(report), ensure_ascii=False, indent=2))

    if not report["ready_for_leadership_demo"] and not args.allow_not_ready:
        raise SystemExit(1)


def build_readiness_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    final_acceptance: Path = DEFAULT_FINAL_ACCEPTANCE,
    quality_report: Path = DEFAULT_QUALITY_REPORT,
    pre_demo_smoke: Path = DEFAULT_PRE_DEMO_SMOKE,
    quality_min_pass_rate: float = DEFAULT_QUALITY_MIN_PASS_RATE,
    smoke_min_pass_rate: float = DEFAULT_SMOKE_MIN_PASS_RATE,
) -> dict[str, Any]:
    reports = [
        ReportPath("final_acceptance", final_acceptance, "Final acceptance"),
        ReportPath("presentation_quality", quality_report, "Presentation quality"),
        ReportPath("pre_demo_smoke", pre_demo_smoke, "Pre-demo smoke"),
    ]
    loaded = {item.key: _load_report(item) for item in reports}

    final_data = loaded["final_acceptance"]["data"]
    quality_data = loaded["presentation_quality"]["data"]
    smoke_data = loaded["pre_demo_smoke"]["data"]

    gates = [
        _gate(
            "final_acceptance_passed",
            loaded["final_acceptance"]["exists"] and final_data.get("passed") is True,
            "Полный локальный acceptance gate должен быть зелёным.",
        ),
        _gate(
            "presentation_quality_100_percent",
            loaded["presentation_quality"]["exists"]
            and _float(quality_data.get("total_pass_rate")) >= quality_min_pass_rate,
            (
                "Pass rate презентационного отчёта качества должен быть >= "
                f"{quality_min_pass_rate:.0%}."
            ),
        ),
        _gate(
            "pre_demo_smoke_100_percent",
            loaded["pre_demo_smoke"]["exists"]
            and _float(smoke_data.get("pass_rate")) >= smoke_min_pass_rate,
            f"Быстрый pre-demo smoke должен иметь pass rate >= {smoke_min_pass_rate:.0%}.",
        ),
        _gate(
            "trace_required_and_available",
            loaded["pre_demo_smoke"]["exists"]
            and smoke_data.get("require_trace") is True
            and not smoke_data.get("trace_error"),
            "Pre-demo smoke должен проверять trace, источники, эскалации и PII masking.",
        ),
    ]

    ready = all(item["ok"] for item in gates)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "ready_for_leadership_demo": ready,
        "decision": "ready" if ready else "not_ready",
        "gates": gates,
        "inputs": {key: _input_metadata(value) for key, value in loaded.items()},
        "metrics": {
            "final_acceptance": _final_acceptance_metrics(final_data),
            "presentation_quality": _quality_metrics(quality_data),
            "pre_demo_smoke": _smoke_metrics(smoke_data),
        },
        "demo_links": {
            "admin_panel": "http://139.100.225.44/admin/kb",
            "local_admin_panel": "http://127.0.0.1/admin/kb",
            "quality_report": str(quality_report.with_suffix(".md")).replace("\\", "/"),
            "demo_pack": "reports/presentation_quality/demo_pack.md",
            "pre_demo_smoke": str(pre_demo_smoke.with_suffix(".md")).replace("\\", "/"),
            "demo_runbook": "docs/presentation_demo_runbook.md",
            "leadership_pack": "docs/leadership_demo_pack.md",
        },
        "demo_scope": {
            "included": [
                "RAG-бот на опубликованной базе знаний и read-only Yonote-источниках.",
                (
                    "Админ-панель для поиска, правки чанков, reindex, validation, "
                    "quality и ops-отчётов."
                ),
                "FastAPI /ask API, trace, cited sources, PII masking и safety escalation.",
                "Тестовый HDE/VK-контур только для коротких smoke-проверок.",
                "Процесс еженедельной продуктовой калибровки по деперсонализированным тикетам.",
            ],
            "not_in_monday_demo": [
                "Автономная запись, удаление или редактирование документов внутри Yonote.",
                "Массовое production-включение HDE. У HDE общий лимит 300 RPM на систему.",
                "Рассылки и маркетинговые кампании. Это отдельный модуль consent/opt-out/audit.",
                "Обещание 100% закрытия всех будущих новых вопросов без операторов.",
            ],
        },
        "commands": {
            "local_pre_demo_smoke": (
                ".venv\\Scripts\\python.exe scripts\\run_pre_demo_smoke.py "
                "--target http://localhost:8001/ask "
                "--output-dir reports/presentation_quality/pre_demo_smoke_latest "
                "--fail-under 1.0"
            ),
            "build_readiness": (
                ".venv\\Scripts\\python.exe scripts\\build_presentation_readiness.py"
            ),
            "full_acceptance": (
                ".venv\\Scripts\\python.exe scripts\\run_acceptance.py "
                "--target http://localhost:8001/ask "
                "--max-llm-cost-rub 80"
            ),
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_markdown(output_dir / "summary.md", report)
    return report


def _load_report(item: ReportPath) -> dict[str, Any]:
    exists = item.path.exists()
    data: dict[str, Any] = {}
    error = ""
    if exists:
        try:
            payload = json.loads(item.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                error = "JSON root is not an object."
            else:
                data = payload
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    return {
        "title": item.title,
        "path": str(item.path).replace("\\", "/"),
        "exists": exists,
        "error": error,
        "data": data,
    }


def _gate(name: str, ok: bool, description: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "description": description}


def _input_metadata(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": report.get("title"),
        "path": report.get("path"),
        "exists": report.get("exists"),
        "error": report.get("error"),
    }


def _final_acceptance_metrics(data: dict[str, Any]) -> dict[str, Any]:
    steps = data.get("steps") or []
    failed_steps = [
        str(step.get("name"))
        for step in steps
        if isinstance(step, dict) and step.get("ok") is not True
    ]
    quality_summary = data.get("quality_summary") or {}
    return {
        "passed": data.get("passed"),
        "generated_at": data.get("generated_at"),
        "target": data.get("target"),
        "steps_total": len(steps),
        "failed_steps": failed_steps,
        "quality_suite_passed": quality_summary.get("passed"),
    }


def _quality_metrics(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": data.get("generated_at"),
        "target": data.get("target"),
        "total_checks_or_turns": data.get("total_checks_or_turns"),
        "total_passed": data.get("total_passed"),
        "total_pass_rate": _float(data.get("total_pass_rate")),
        "typical_pass_rate": _nested_float(data, "typical", "pass_rate"),
        "atypical_pass_rate": _nested_float(data, "atypical", "pass_rate"),
        "safety_pass_rate": _nested_float(data, "safety", "pass_rate"),
        "llm_estimated_cost_rub": _float(data.get("total_llm_estimated_cost_rub")),
    }


def _smoke_metrics(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": data.get("generated_at"),
        "target": data.get("target"),
        "cases_total": data.get("cases_total"),
        "passed": data.get("passed"),
        "pass_rate": _float(data.get("pass_rate")),
        "require_trace": data.get("require_trace"),
        "trace_error": data.get("trace_error"),
        "failed": data.get("failed") or [],
        "llm_estimated_cost_rub": _float(data.get("llm_estimated_cost_rub")),
    }


def _nested_float(data: dict[str, Any], section: str, key: str) -> float:
    nested = data.get(section)
    if not isinstance(nested, dict):
        return 0.0
    return _float(nested.get(key))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready_for_leadership_demo": report["ready_for_leadership_demo"],
        "decision": report["decision"],
        "gates": {gate["name"]: gate["ok"] for gate in report["gates"]},
        "quality_pass_rate": report["metrics"]["presentation_quality"]["total_pass_rate"],
        "smoke_pass_rate": report["metrics"]["pre_demo_smoke"]["pass_rate"],
        "report": "reports/presentation_readiness/summary.md",
    }


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    status = "Готово к презентации" if report["ready_for_leadership_demo"] else "Не готово"
    quality = report["metrics"]["presentation_quality"]
    smoke = report["metrics"]["pre_demo_smoke"]
    acceptance = report["metrics"]["final_acceptance"]

    lines = [
        "# Готовность к презентации",
        "",
        f"Статус: **{status}**.",
        "",
        (
            "Это короткий контрольный лист перед демонстрацией руководству. "
            "Он не заменяет большой отчёт качества, а собирает главный вердикт "
            "из уже прогнанных gate-ов."
        ),
        "",
        "## Главные цифры",
        "",
        f"- Final acceptance: `{acceptance.get('passed')}`.",
        (
            f"- Presentation quality: `{quality.get('total_passed')}/"
            f"{quality.get('total_checks_or_turns')}` "
            f"({quality.get('total_pass_rate'):.1%})."
        ),
        (
            f"- Pre-demo smoke: `{smoke.get('passed')}/{smoke.get('cases_total')}` "
            f"({smoke.get('pass_rate'):.1%}), trace required: `{smoke.get('require_trace')}`."
        ),
        (
            "- Оценочная стоимость LLM в большом presentation quality report: "
            f"`{quality.get('llm_estimated_cost_rub'):.6f} RUB`."
        ),
        (
            "- Оценочная стоимость LLM в быстром smoke: "
            f"`{smoke.get('llm_estimated_cost_rub'):.6f} RUB`."
        ),
        "",
        "## Gates",
        "",
    ]

    for gate in report["gates"]:
        lines.append(
            f"- {'OK' if gate['ok'] else 'FAIL'} `{gate['name']}`: {gate['description']}"
        )

    lines.extend(
        [
            "",
            "## Что показывать",
            "",
            f"- Админ-панель: `{report['demo_links']['admin_panel']}`.",
            f"- Runbook показа: `{report['demo_links']['demo_runbook']}`.",
            f"- Пакет для руководства: `{report['demo_links']['leadership_pack']}`.",
            f"- Большой отчёт качества: `{report['demo_links']['quality_report']}`.",
            f"- Пакет живых примеров: `{report['demo_links']['demo_pack']}`.",
            f"- Быстрый smoke: `{report['demo_links']['pre_demo_smoke']}`.",
            "",
            "## Границы демо",
            "",
            "Входит в демонстрацию:",
        ]
    )
    lines.extend(f"- {item}" for item in report["demo_scope"]["included"])
    lines.extend(["", "Не обещаем как готовое к понедельнику:"])
    lines.extend(f"- {item}" for item in report["demo_scope"]["not_in_monday_demo"])
    lines.extend(
        [
            "",
            "## Команды перед показом",
            "",
            "```powershell",
            report["commands"]["local_pre_demo_smoke"],
            report["commands"]["build_readiness"],
            "```",
            "",
            "Если нужно полностью перепроверить релиз локально:",
            "",
            "```powershell",
            report["commands"]["full_acceptance"],
            "```",
            "",
        ]
    )

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
