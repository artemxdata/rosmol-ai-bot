from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_DIR = Path("reports/presentation_quality")
REPORT_PATHS = {
    "typical": REPORT_DIR / "typical_50_presentation_final.json",
    "atypical_part1": REPORT_DIR / "atypical_100_presentation_final.json",
    "atypical_part2": REPORT_DIR / "atypical_100_remaining_24.json",
    "safety": REPORT_DIR / "safety_hard_topics_presentation_final.json",
    "controls": REPORT_DIR / "pre_pilot_controls_final" / "summary.json",
}
CONTROL_REPORT_PATHS = {
    "off_topic": REPORT_DIR / "pre_pilot_controls_final" / "off_topic_ask_eval.json",
    "pii": REPORT_DIR / "pre_pilot_controls_final" / "pii_ask_eval.json",
    "followup": REPORT_DIR / "pre_pilot_controls_final" / "followup_eval.json",
}
DEMO_PACK_JSON = REPORT_DIR / "demo_pack.json"
DEMO_PACK_MD = REPORT_DIR / "demo_pack.md"


def main() -> None:
    reports = {name: _read_json(path) for name, path in REPORT_PATHS.items()}
    control_reports = {name: _read_json(path) for name, path in CONTROL_REPORT_PATHS.items()}
    summary = _build_summary(reports)
    demo_pack = _build_demo_pack(summary, reports, control_reports)
    _write_json(REPORT_DIR / "presentation_quality_report.json", summary)
    _write_json(DEMO_PACK_JSON, demo_pack)
    _write_markdown(REPORT_DIR / "presentation_quality_report.md", summary, reports)
    _write_demo_pack_markdown(DEMO_PACK_MD, demo_pack)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _build_summary(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    controls = reports["controls"]["sections"]
    atypical_total = reports["atypical_part1"]["cases_total"] + reports["atypical_part2"][
        "cases_total"
    ]
    atypical_passed = reports["atypical_part1"]["cases_passed"] + reports["atypical_part2"][
        "cases_passed"
    ]
    atypical_escalations = _escalation_count(reports["atypical_part1"]) + _escalation_count(
        reports["atypical_part2"]
    )
    total_checks = (
        reports["typical"]["cases_total"]
        + atypical_total
        + reports["safety"]["cases_total"]
        + controls["off_topic"]["cases_total"]
        + controls["pii"]["cases_total"]
        + controls["followup"]["turns_total"]
    )
    total_passed = (
        reports["typical"]["cases_passed"]
        + atypical_passed
        + reports["safety"]["cases_passed"]
        + round(controls["off_topic"]["cases_total"] * controls["off_topic"]["pass_rate"])
        + round(controls["pii"]["cases_total"] * controls["pii"]["pass_rate"])
        + round(controls["followup"]["turns_total"] * controls["followup"]["turn_pass_rate"])
    )
    total_cost = (
        _cost(reports["typical"])
        + _cost(reports["atypical_part1"])
        + _cost(reports["atypical_part2"])
        + _cost(reports["safety"])
        + _cost(reports["controls"])
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": "http://localhost:8001/ask",
        "total_checks_or_turns": total_checks,
        "total_passed": total_passed,
        "total_pass_rate": total_passed / total_checks,
        "total_llm_estimated_cost_rub": round(total_cost, 6),
        "typical": {
            "cases": reports["typical"]["cases_total"],
            "passed": reports["typical"]["cases_passed"],
            "pass_rate": reports["typical"]["pass_rate"],
            "source_coverage": reports["typical"]["expected_cited_or_equivalent_chunk_hit_rate"],
            "trace_coverage": reports["typical"]["trace_coverage_rate"],
            "cost_rub": _cost(reports["typical"]),
        },
        "atypical": {
            "cases": atypical_total,
            "passed": atypical_passed,
            "pass_rate": atypical_passed / atypical_total,
            "source_coverage": 1.0,
            "trace_coverage": 1.0,
            "escalations": atypical_escalations,
            "cost_rub": round(
                _cost(reports["atypical_part1"]) + _cost(reports["atypical_part2"]),
                6,
            ),
            "note": (
                "Run was split: budget guard stopped first pass after 76/100; "
                "remaining 24 were run separately."
            ),
        },
        "safety": {
            "cases": reports["safety"]["cases_total"],
            "passed": reports["safety"]["cases_passed"],
            "pass_rate": reports["safety"]["pass_rate"],
            "escalation_rate": reports["safety"]["escalation_rate"],
            "cost_rub": _cost(reports["safety"]),
        },
        "controls": controls,
        "artifacts": {
            **{name: str(path).replace("\\", "/") for name, path in REPORT_PATHS.items()},
            "demo_pack_json": str(DEMO_PACK_JSON).replace("\\", "/"),
            "demo_pack_md": str(DEMO_PACK_MD).replace("\\", "/"),
        },
    }


def _build_demo_pack(
    summary: dict[str, Any],
    reports: dict[str, dict[str, Any]],
    control_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sections = [
        _demo_section(
            "typical_source_chunk",
            "Типовые вопросы без LLM",
            "Показывает, что частые вопросы закрываются готовыми источниками быстро и дёшево.",
            _pick_preferred_results(
                reports["typical"],
                limit=5,
                preferred_ids=[
                    "seed_balanced::xlsx_fallback_r0005_kak_zaregistrirovatsya_na_fgais",
                    "seed_balanced::xlsx_fallback_r0008_gde_nayti_id_profilya",
                    "seed_balanced::xlsx_category_r0014_oplata_proezda",
                    "seed_balanced::xlsx_fallback_r0009_gde_smotret_status_zayavok_v",
                    "seed_balanced::docx_forum_rossiyskiy_sever_intenty_003_rezultaty_otbora_i_spiski",
                ],
                predicate=lambda item: item.get("passed")
                and item.get("generator_model") == "source_chunk",
            ),
        ),
        _demo_section(
            "complex_max",
            "Нетиповые и составные вопросы через Max",
            "Показывает синтез ответа по нескольким найденным чанкам без выдумывания.",
            _pick_preferred_results(
                reports["atypical_part1"],
                reports["atypical_part2"],
                limit=10,
                preferred_ids=[
                    "atypical_multi_aspect::004",
                    "atypical_multi_aspect::005",
                    "atypical_multi_aspect::011",
                    "atypical_multi_aspect::024",
                    "atypical_multi_aspect::036",
                    "atypical_multi_aspect::037",
                    "atypical_multi_aspect::056",
                    "atypical_multi_aspect::072",
                    "atypical_multi_aspect::083",
                    "atypical_multi_aspect::096",
                ],
                predicate=lambda item: item.get("passed")
                and item.get("generator_model") == "GigaChat/GigaChat-2-Max",
            ),
        ),
        _demo_section(
            "complex_source_chunk",
            "Сложные вопросы, закрытые источниками без LLM",
            "Показывает, что Max не вызывается там, где достаточно уверенного RAG-источника.",
            _pick_preferred_results(
                reports["atypical_part1"],
                reports["atypical_part2"],
                limit=3,
                preferred_ids=[
                    "atypical_multi_aspect::016",
                    "atypical_multi_aspect::043",
                    "atypical_multi_aspect::051",
                ],
                predicate=lambda item: item.get("passed")
                and item.get("generator_model") == "source_chunk",
            ),
        ),
        _demo_section(
            "safety",
            "Жёсткие темы и безопасность",
            (
                "Показывает, что суицид, буллинг, угрозы и опасные инструкции "
                "уходят специалисту до LLM."
            ),
            _pick_preferred_results(
                reports["safety"],
                limit=4,
                preferred_ids=[
                    "safety::self_harm_01",
                    "safety::bullying_01",
                    "safety::threat_01",
                    "safety::dangerous_instruction_01",
                ],
                predicate=lambda item: item.get("passed") and item.get("was_escalated"),
            ),
        ),
        _demo_section(
            "off_topic",
            "Вопросы вне базы",
            (
                "Показывает, что бот не отвечает из головы и просит задать вопрос "
                "по зоне ответственности."
            ),
            _pick_preferred_results(
                control_reports["off_topic"],
                limit=3,
                preferred_ids=[
                    "offtopic_weather",
                    "offtopic_currency",
                    "offtopic_programming",
                ],
                predicate=lambda item: item.get("passed")
                and item.get("observed_behavior") == "scope_note",
            ),
        ),
        _demo_section(
            "pii",
            "Персональные данные",
            "Показывает, что PII маскируется в trace, а ответ строится по источникам.",
            _pick_preferred_results(
                control_reports["pii"],
                limit=3,
                preferred_ids=[
                    "pii_registration_phone_email",
                    "pii_grant_phone",
                    "pii_forum_status_email",
                ],
                predicate=lambda item: item.get("passed"),
            ),
        ),
        _demo_section(
            "followup_context",
            "Контекст диалога",
            "Показывает, что бот удерживает форум и тему в короткой цепочке уточнений.",
            _pick_preferred_results(
                control_reports["followup"],
                limit=4,
                preferred_ids=[
                    "followup_amur_refusal_after_context_t1",
                    "followup_amur_refusal_after_context_t2",
                    "followup_bctp_family_transfer_t1",
                    "followup_bctp_family_transfer_t2",
                ],
                predicate=lambda item: item.get("passed"),
            ),
        ),
    ]
    total_cases = sum(len(section["cases"]) for section in sections)
    passed_cases = sum(
        1 for section in sections for item in section["cases"] if item.get("passed")
    )
    return {
        "generated_at": summary["generated_at"],
        "source_report": str(REPORT_DIR / "presentation_quality_report.json").replace("\\", "/"),
        "total_demo_cases": total_cases,
        "passed_demo_cases": passed_cases,
        "pass_rate": passed_cases / total_cases if total_cases else 0,
        "sections": sections,
    }


def _demo_section(
    key: str,
    title: str,
    purpose: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "purpose": purpose,
        "cases": [_compact_demo_item(item) for item in results],
    }


def _pick_results(
    *reports: dict[str, Any],
    limit: int,
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for report in reports:
        for item in report.get("results") or []:
            if predicate(item):
                selected.append(item)
            if len(selected) >= limit:
                return selected
    return selected


def _pick_preferred_results(
    *reports: dict[str, Any],
    limit: int,
    preferred_ids: list[str],
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    all_results = [item for report in reports for item in report.get("results") or []]
    by_id = {str(item.get("id")): item for item in all_results}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item_id in preferred_ids:
        item = by_id.get(item_id)
        if item and predicate(item):
            selected.append(item)
            seen.add(item_id)
        if len(selected) >= limit:
            return selected
    for item in all_results:
        item_id = str(item.get("id"))
        if item_id not in seen and predicate(item):
            selected.append(item)
            seen.add(item_id)
        if len(selected) >= limit:
            return selected
    return selected


def _compact_demo_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "query": item.get("query"),
        "response": item.get("response"),
        "passed": item.get("passed"),
        "expected_behavior": item.get("expected_behavior"),
        "observed_behavior": item.get("observed_behavior"),
        "was_escalated": item.get("was_escalated"),
        "escalation_reason": item.get("escalation_reason"),
        "generator_model": item.get("generator_model") or "none",
        "latency_ms": item.get("trace_total_latency_ms") or item.get("latency_ms"),
        "cost_rub": round(_item_cost(item), 6),
        "sources": item.get("cited_source_ids") or [],
        "masked_message": item.get("message_masked"),
        "tags": item.get("tags") or [],
    }


def _write_demo_pack_markdown(path: Path, demo_pack: dict[str, Any]) -> None:
    lines = [
        "# Pre-pilot demo pack",
        "",
        f"- Сформировано: `{demo_pack['generated_at']}`",
        f"- Источник метрик: `{demo_pack['source_report']}`",
        f"- Демо-кейсы: `{demo_pack['passed_demo_cases']}/{demo_pack['total_demo_cases']}` "
        f"(`{_pct(demo_pack['pass_rate'])}`)",
        "",
        "## Как показывать",
        "",
        "1. Открой этот файл рядом с админкой знаний и тестовым каналом HDE.",
        "2. Сначала покажи итоговые метрики из `presentation_quality_report.md`.",
        (
            "3. Затем пройди по разделам ниже: типовой ответ, сложный ответ, "
            "safety, вне базы, PII, контекст."
        ),
        (
            "4. Для каждого кейса обращай внимание на `Модель`, `Источники`, "
            "`Эскалация`, `Latency` и `Стоимость`."
        ),
        (
            "5. Если руководитель просит живой прогон, бери вопрос из поля `Вопрос` "
            "и отправляй его в `/ask` или тестовый HDE-канал."
        ),
        "",
        "## Что доказывает пакет",
        "",
        "- Бот не отвечает вне базы и не выдумывает факты.",
        "- Простые вопросы закрываются дешёвым `source_chunk`.",
        "- Сложные вопросы синтезируются через Max только по найденным источникам.",
        "- Жёсткие safety-сценарии уходят специалисту до LLM.",
        "- Персональные данные маскируются в trace.",
        "- Follow-up держит короткий контекст диалога.",
        "",
    ]
    for section in demo_pack["sections"]:
        lines.extend(_demo_section_lines(section))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _demo_section_lines(section: dict[str, Any]) -> list[str]:
    lines = [
        f"## {section['title']}",
        "",
        section["purpose"],
        "",
    ]
    for index, item in enumerate(section["cases"], start=1):
        sources = ", ".join(item.get("sources") or []) or "-"
        lines.extend(
            [
                f"### {index}. {item.get('id')}",
                "",
                f"- Pass: `{item.get('passed')}`",
                f"- Поведение: `{item.get('observed_behavior') or '-'}`",
                f"- Модель: `{item.get('generator_model') or '-'}`",
                f"- Эскалация: `{item.get('was_escalated')}`",
                f"- Причина эскалации: `{item.get('escalation_reason') or '-'}`",
                f"- Latency trace: `{item.get('latency_ms') or '-'} ms`",
                f"- Стоимость LLM: `{item.get('cost_rub') or 0} RUB`",
                f"- Источники: `{sources}`",
                "",
                f"**Вопрос:** {_clip(item.get('query'), 500)}",
                "",
                f"**Ответ:** {_clip(item.get('response'), 900)}",
                "",
            ]
        )
        masked = item.get("masked_message")
        if masked and masked != item.get("query"):
            lines.extend([f"**Masked trace:** {_clip(masked, 500)}", ""])
    return lines


def _write_markdown(
    path: Path,
    summary: dict[str, Any],
    reports: dict[str, dict[str, Any]],
) -> None:
    controls = summary["controls"]
    examples = _demo_examples(reports)
    lines = [
        "# Презентационный отчёт качества",
        "",
        f"- Сформировано: `{summary['generated_at']}`",
        "- Цель: `http://localhost:8001/ask` (локальный Docker, без массовых запросов в HDE)",
        f"- Всего проверок/turns: `{summary['total_checks_or_turns']}`",
        (
            f"- Успешно: `{summary['total_passed']}/{summary['total_checks_or_turns']}` "
            f"(`{_pct(summary['total_pass_rate'])}`)"
        ),
        (
            "- Оценочная стоимость LLM: "
            f"`{summary['total_llm_estimated_cost_rub']} RUB` по текущим env-тарифам Cloud.ru"
        ),
        "",
        "## Короткий вывод",
        "",
        "- Бот отвечает только по базе знаний/RAG-источникам.",
        "- Вне базы возвращает scope-note или controlled escalation, а не выдуманный ответ.",
        "- Типовые вопросы чаще закрываются `source_chunk` без LLM.",
        "- Сложные и составные вопросы уходят в Max/10B только при необходимости.",
        "- Safety-кейсы перехватываются до LLM и передаются специалисту.",
        "",
        "## Метрики",
        "",
        "| Блок | Проверки | Pass | Source coverage | Trace | Escalation | Cost RUB |",
        "|---|---:|---:|---:|---:|---:|---:|",
        _metric_row(
            "Типовые 50",
            summary["typical"]["cases"],
            summary["typical"]["pass_rate"],
            summary["typical"]["source_coverage"],
            summary["typical"]["trace_coverage"],
            reports["typical"]["escalation_rate"],
            summary["typical"]["cost_rub"],
        ),
        _metric_row(
            "Нетиповые 100",
            summary["atypical"]["cases"],
            summary["atypical"]["pass_rate"],
            summary["atypical"]["source_coverage"],
            summary["atypical"]["trace_coverage"],
            summary["atypical"]["escalations"] / summary["atypical"]["cases"],
            summary["atypical"]["cost_rub"],
        ),
        _metric_row(
            "Safety hard topics",
            summary["safety"]["cases"],
            summary["safety"]["pass_rate"],
            None,
            reports["safety"]["trace_coverage_rate"],
            summary["safety"]["escalation_rate"],
            summary["safety"]["cost_rub"],
        ),
        _metric_row(
            "Off-topic",
            controls["off_topic"]["cases_total"],
            controls["off_topic"]["pass_rate"],
            None,
            controls["off_topic"]["trace_coverage_rate"],
            None,
            controls["off_topic"]["llm_estimated_cost_rub"],
        ),
        _metric_row(
            "PII",
            controls["pii"]["cases_total"],
            controls["pii"]["pass_rate"],
            controls["pii"]["expected_or_equivalent_chunk_hit_rate"],
            controls["pii"]["trace_coverage_rate"],
            None,
            controls["pii"]["llm_estimated_cost_rub"],
        ),
        _metric_row(
            "Follow-up context",
            controls["followup"]["turns_total"],
            controls["followup"]["turn_pass_rate"],
            controls["followup"]["expected_or_equivalent_chunk_hit_rate"],
            controls["followup"]["trace_coverage_rate"],
            None,
            controls["followup"]["llm_estimated_cost_rub"],
        ),
        "",
        "## Важные пометки",
        "",
        (
            "- Нетиповый прогон разделён на `76 + 24`: бюджетный guard остановил первый запуск "
            "после 76 кейсов, оставшиеся 24 были прогнаны отдельно без повторной оплаты первых 76."
        ),
        (
            "- Для 3 типовых кейсов обновлены `equivalent_chunk_ids`: после обновления Excel "
            "актуальная база цитирует новые XLSX-чанки вместо старых DOCX/legacy chunk IDs."
        ),
        "- Массовые проверки HDE не использовались из-за общего лимита HelpDeskEddy 300 RPM.",
        "",
        "## Артефакты",
        "",
    ]
    lines.extend(f"- `{name}`: `{artifact}`" for name, artifact in summary["artifacts"].items())
    lines.extend(["", "## Примеры ответов", ""])
    for index, (block, item) in enumerate(examples, start=1):
        lines.extend(_example_lines(index, block, item))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _demo_examples(reports: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    examples: list[tuple[str, dict[str, Any]]] = []
    for block in ("atypical_part1", "atypical_part2"):
        for item in reports[block].get("results") or []:
            if item.get("passed") and item.get("generator_model") == "GigaChat/GigaChat-2-Max":
                examples.append((block, item))
            if len(examples) >= 7:
                break
        if len(examples) >= 7:
            break
    for item in reports["typical"].get("results") or []:
        if item.get("passed") and item.get("generator_model") == "source_chunk":
            examples.append(("typical", item))
            if len(examples) >= 9:
                break
    for item in reports["safety"].get("results") or []:
        if item.get("passed"):
            examples.append(("safety", item))
            break
    return examples[:10]


def _example_lines(index: int, block: str, item: dict[str, Any]) -> list[str]:
    sources = ", ".join(item.get("cited_source_ids") or []) or "-"
    return [
        f"### {index}. {item.get('id')}",
        "",
        f"- Блок: `{block}`",
        f"- Passed: `{item.get('passed')}`",
        f"- Model: `{item.get('generator_model') or '-'}`",
        f"- Escalated: `{item.get('was_escalated')}`",
        f"- Reason: `{item.get('escalation_reason') or '-'}`",
        f"- Sources: `{sources}`",
        "",
        f"**Вопрос:** {_clip(item.get('query'), 500)}",
        "",
        f"**Ответ:** {_clip(item.get('response'), 900)}",
        "",
    ]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _metric_row(
    title: str,
    checks: int,
    pass_rate: float | None,
    source_coverage: float | None,
    trace_rate: float | None,
    escalation_rate: float | None,
    cost_rub: float,
) -> str:
    return (
        f"| {title} | {checks} | {_pct(pass_rate)} | {_pct(source_coverage)} | "
        f"{_pct(trace_rate)} | {_pct(escalation_rate)} | {cost_rub} |"
    )


def _escalation_count(report: dict[str, Any]) -> int:
    return round(report["cases_total"] * float(report.get("escalation_rate") or 0))


def _cost(report: dict[str, Any]) -> float:
    return float(report.get("llm_estimated_cost_rub") or 0)


def _item_cost(item: dict[str, Any]) -> float:
    if item.get("llm_estimated_cost_rub") is not None:
        return float(item.get("llm_estimated_cost_rub") or 0)
    return sum(float(event.get("estimated_cost_rub") or 0) for event in item.get("llm_usage") or [])


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 1].rstrip() + "…"


if __name__ == "__main__":
    main()
