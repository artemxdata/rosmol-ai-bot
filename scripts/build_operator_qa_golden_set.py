from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from scripts.analyze_ticket_dataset import (
    build_intent,
    classify_category,
    classify_difficulty,
    classify_topic,
    detect_forum,
    extract_key_phrases,
    load_forum_aliases,
    normalize_question_key,
    score_question_segment,
)
from scripts.build_ticket_answer_bank import (
    RegexOnlyPIIMasker,
    safe_chunk_matches,
    sanitize_text,
)
from scripts.prepare_ticket_eval_sets import build_chunk_index
from src.kb.source_extractors import clean_bot_text, read_xlsx_sheets
from src.security.operator_request import operator_review_reason

DEFAULT_INPUT = Path("data/private/operator_qa/June2026_QA_pairs.xlsx")
DEFAULT_KB_SEED = Path("data/knowledge_base_seed.json")
DEFAULT_FORUMS = Path("data/forums_registry.json")
DEFAULT_OUTPUT_DIR = Path("data/private/operator_qa/analysis")

LOW_SIGNAL_QUESTIONS = {
    "start",
    "/start",
    "назад",
    "тест",
    "билет",
    "оператор",
    "привет",
    "здравствуйте",
    "добрый день",
    "добрый вечер",
    "спасибо",
    "я ищу проект",
    "ищу проект",
}
MAIL_ARTIFACT_RE = re.compile(
    r"(?:отправлено\s+(?:с|из)|sent\s+from\s+my|mail\s+for\s+android)",
    re.IGNORECASE,
)
ATTACHMENT_ONLY_RE = re.compile(
    r"^\s*(?:image[-_ ]?\d*|photo|document|video|audio|story:\s*url|https?://\S+)\s*$",
    re.IGNORECASE,
)
TRANSFER_MARKERS = (
    "передали ваше обращение",
    "передали обращение",
    "передали ваш вопрос",
    "передано специалистам",
    "передали специалистам",
    "ответственным специалистам",
    "профильным специалистам",
    "потребуется некоторое время",
    "как только появится решение",
    "как только поступит ответ",
)
CLARIFY_MARKERS = (
    "уточни, пожалуйста",
    "уточните, пожалуйста",
    "напиши, пожалуйста",
    "напишите, пожалуйста",
    "укажи, пожалуйста",
    "укажите, пожалуйста",
    "пришли, пожалуйста",
    "пришлите, пожалуйста",
    "сообщи, пожалуйста",
    "сообщите, пожалуйста",
    "о каком форуме",
    "название мероприятия",
    "полное название",
)
SCOPE_NOTE_MARKERS = (
    "отвечаю на вопросы по мероприятиям",
    "информировать о деятельности росмолодёжи",
    "вопрос по мероприятиям или деятельности росмолодёжи",
    "не уполномочен",
    "не относится к компетенции росмолодёжи",
)
PERSONAL_OR_MANUAL_MARKERS = (
    "по вашей заявке",
    "статус вашей заявки",
    "ваша заявка находится",
    "в вашем обращении",
    "по вашему обращению",
    "мы проверили",
    "мы видим в системе",
    "для вас был",
    "вам направлено письмо",
    "ваши данные",
    "ваша заявка",
    "ваш аккаунт",
    "ваш порядковый номер",
    "ваш результат",
    "прикладываем скриншот",
    "приложили скриншот",
    "обратитесь к куратору",
    "направьте нам",
    "пришлите нам",
)
INTERNAL_PROCESS_MARKERS = (
    "вторая линия",
    "2 линия",
    "внутренний комментарий",
    "служебная информация",
    "hde",
    "mango",
)
MANUAL_REQUEST_MARKERS = (
    "обращаюсь с предложением",
    "предлагаю сотрудничество",
    "предложить сотрудничество",
    "информационным партнёром",
    "информационным партнером",
    "включить в программу",
    "выступить на день молодёжи",
    "выступить на день молодежи",
    "разместить анонс",
    "делегировать представителя",
    "официальное обращение",
    "прошу вас принять меры",
    "просим рассмотреть возможность",
    "жалоба на",
)
PERSONAL_QUESTION_MARKERS = (
    "меня зовут",
    "пишет вам",
    "пишет: фио",
    "идентификатор пользователя",
    "моя заявка",
    "наша заявка",
    "мой проект",
    "наш проект",
    "почему мою заявку",
    "почему нашу заявку",
    "почему мне отказали",
    "со мной не связался",
)
ANSWER_SHAPE_MARKERS = (
    "благодарим вас за обращение",
    "чтобы найти нужные контакты",
    "если появятся новые вопросы",
    "мы всегда рядом и готовы помочь",
    "в конкурсе могут участвовать граждане",
    "попасть на фестиваль проще простого",
    "росмолодежь tool",
)
FOLLOWUP_FRAGMENT_RE = re.compile(
    r"^(?:спасибо\s+за\s+(?:совет|ответ)|спасибо,?\s+но|"
    r"я\s+уже\s+(?:всё|все)\s+перепробовал|как\s+я\s+уже\s+писал)",
    re.IGNORECASE,
)
OUTDATED_OR_TEMPORAL_RE = re.compile(
    r"(?:\b\d{1,2}[./]\d{1,2}[./]2026\b|\b\d{1,2}\s+"
    r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|"
    r"октября|ноября|декабря)\s+2026\b)",
    re.IGNORECASE,
)
URL_FACT_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
NUMBER_FACT_RE = re.compile(r"(?<!\w)\d{2,}[\d\s.,:%-]{0,18}\d|(?<!\w)\d{2,}(?!\w)")
GENERIC_GREETING_RE = re.compile(
    r"^(?:благодарим|спасибо)\s+(?:за\s+)?(?:ваше\s+)?(?:обращение|ожидание|интерес)",
    re.IGNORECASE,
)
QUESTION_WORD_RE = re.compile(
    r"\b(?:как|где|когда|куда|почему|зачем|можно\s+ли|что\s+делать|подскаж)\b",
    re.IGNORECASE,
)
SUPPORTED_CATEGORY = {"форумы", "гранты", "платформа_фгаис", "техподдержка", "навигация"}
OPERATOR_JOIN_RE = re.compile(
    r"(?:присоединюсь|подключусь|присоединится|подключится)\s+к\s+диалогу",
    re.IGNORECASE,
)
TRAILING_PERSON_SIGNATURE_RE = re.compile(
    r"\s*(?:--|—)\s*[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}\s*$"
)
TICKET_CODE_RE = re.compile(
    r"\b((?:билет|код\s+билета)\s*:?[ \t]*)"
    r"(?=[A-ZА-ЯЁ0-9-]{6,}\b)(?=[A-ZА-ЯЁ0-9-]*[A-ZА-ЯЁ])"
    r"(?=[A-ZА-ЯЁ0-9-]*\d)[A-ZА-ЯЁ0-9-]+\b",
    re.IGNORECASE,
)


def build_operator_golden_set(
    input_path: Path = DEFAULT_INPUT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    kb_seed_path: Path = DEFAULT_KB_SEED,
    forums_path: Path = DEFAULT_FORUMS,
    calibration_size: int = 200,
    holdout_size: int = 100,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    masker = RegexOnlyPIIMasker()
    forums = load_forum_aliases(forums_path)
    kb_records = _read_json_array(kb_seed_path)
    rows = load_operator_qa_rows(input_path)
    normalized = [
        normalize_operator_pair(row, masker=masker, forums=forums)
        for row in rows
    ]

    unique_records = deduplicate_records(normalized)
    eligible = [record for record in unique_records if record["golden_eligible"]]
    requested = calibration_size + holdout_size
    candidate_pool_size = min(len(eligible), max(requested * 2, requested))
    candidate_pool = balanced_records(eligible)[:candidate_pool_size]
    chunks = build_chunk_index(kb_records)
    for record in candidate_pool:
        enrich_with_chunk_matches(record, chunks)
    selected = balanced_records(candidate_pool)[:requested]
    calibration = selected[:calibration_size]
    holdout = selected[calibration_size:requested]

    _assign_case_ids(calibration, "operator_qa_calibration")
    _assign_case_ids(holdout, "operator_qa_holdout")
    behavior_matrix = build_behavior_matrix(normalized)
    profile = build_profile(
        normalized,
        unique_records=unique_records,
        eligible=eligible,
        calibration=calibration,
        holdout=holdout,
        input_path=input_path,
    )

    write_json(output_dir / "profile.json", profile)
    write_jsonl(output_dir / "operator_pairs_normalized.jsonl", normalized)
    write_json(output_dir / "operator_golden_calibration.json", calibration)
    write_json(output_dir / "operator_golden_holdout.json", holdout)
    write_json(output_dir / "operator_behavior_matrix.json", behavior_matrix)
    write_review_csv(output_dir / "operator_golden_review.csv", selected)
    (output_dir / "analysis_report.md").write_text(
        build_report(profile, behavior_matrix),
        encoding="utf-8",
    )

    summary = {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "rows": len(rows),
        "unique_pairs": len(unique_records),
        "golden_eligible": len(eligible),
        "calibration": len(calibration),
        "holdout": len(holdout),
        "behavior_counts": profile["behavior_counts"],
        "exclusion_counts": profile["exclusion_counts"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def load_operator_qa_rows(path: Path) -> list[dict[str, str]]:
    sheets = read_xlsx_sheets(path)
    if not sheets:
        raise ValueError("xlsx workbook has no readable sheets")
    first_sheet = next(iter(sheets.values()))
    if not first_sheet:
        raise ValueError("xlsx first sheet is empty")
    headers = [cell.strip() for cell in first_sheet[0].cells]
    required = {"ticket_id", "date", "department", "user_question", "operator_answer"}
    missing = sorted(required - set(headers))
    if missing:
        raise ValueError(f"xlsx is missing required columns: {', '.join(missing)}")

    rows: list[dict[str, str]] = []
    for source_row in first_sheet[1:]:
        row = {header: source_row.cell(index) for index, header in enumerate(headers)}
        if row.get("user_question") or row.get("operator_answer"):
            rows.append(row)
    return rows


def normalize_operator_pair(
    row: dict[str, str],
    *,
    masker: RegexOnlyPIIMasker,
    forums: list[Any],
) -> dict[str, Any]:
    raw_question = clean_bot_text(row.get("user_question") or "")
    raw_answer = clean_bot_text(row.get("operator_answer") or "")
    question = _sanitize_operator_text(_strip_mail_artifacts(raw_question), masker)
    answer = _strip_operator_boilerplate(_sanitize_operator_text(raw_answer, masker))
    forum = detect_forum(question, forums)
    category = classify_category(question)
    if forum and not str(forum).startswith("Гранты"):
        category = "форумы"
    topic = classify_topic(question)
    behavior = classify_operator_behavior(question, answer)
    difficulty = classify_difficulty(
        question,
        typical="",
        should_escalate=behavior == "escalate",
    )
    exclusions = golden_exclusion_reasons(
        question=question,
        answer=answer,
        behavior=behavior,
        category=category,
    )
    temporal = bool(OUTDATED_OR_TEMPORAL_RE.search(answer))
    ticket_hash = _stable_hash(str(row.get("ticket_id") or question))
    return {
        "ticket_hash": ticket_hash,
        "date": row.get("date") or "",
        "department": row.get("department") or "",
        "query": question,
        "reference_answer": answer,
        "expected_behavior": behavior,
        "category": category,
        "topic": topic,
        "intent": build_intent(category, topic),
        "forum_normalized": forum,
        "difficulty": difficulty,
        "expected_answer_contains": [],
        "reference_key_phrases": extract_key_phrases(answer, limit=3),
        "reference_facts": extract_reference_facts(answer),
        "candidate_chunk_ids": [],
        "candidate_chunk_matches": [],
        "official_overlap_score": 0.0,
        "temporal_review_required": temporal,
        "golden_eligible": not exclusions and behavior == "answer",
        "golden_exclusion_reasons": exclusions,
        "fallback_candidate": False,
        "fallback_status": "not_reviewed",
        "source_type": "sanitized_operator_qa",
        "source_month": "2026-06",
        "review_status": "candidate",
        "tags": [
            "operator_qa",
            f"behavior:{behavior}",
            f"category:{category}",
            f"topic:{topic}",
            f"difficulty:{difficulty}",
            *( [f"forum:{forum}"] if forum else [] ),
        ],
    }


def enrich_with_chunk_matches(
    record: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> None:
    matches = safe_chunk_matches(
        {
            "category": record.get("category"),
            "forum_normalized": record.get("forum_normalized"),
        },
        str(record.get("query") or ""),
        str(record.get("reference_answer") or ""),
        chunks,
        top_matches=5,
    )
    top_match_score = float(matches[0]["score"]) if matches else 0.0
    grounded_candidate = (
        top_match_score >= 0.20
        and not record.get("temporal_review_required")
        and not record.get("golden_exclusion_reasons")
    )
    record["candidate_chunk_ids"] = [match["chunk_id"] for match in matches]
    record["candidate_chunk_matches"] = matches
    record["official_overlap_score"] = round(top_match_score, 6)
    record["fallback_candidate"] = grounded_candidate
    record["fallback_status"] = "candidate" if grounded_candidate else "not_eligible"


def classify_operator_behavior(question: str, answer: str) -> str:
    normalized_question = _normalize(question)
    normalized_answer = _normalize(answer)
    if any(marker in normalized_question for marker in MANUAL_REQUEST_MARKERS):
        return "escalate"
    if any(marker in normalized_answer for marker in SCOPE_NOTE_MARKERS):
        return "scope_note"
    if any(marker in normalized_answer for marker in TRANSFER_MARKERS):
        return "escalate"
    clarify_positions = [
        normalized_answer.find(marker)
        for marker in CLARIFY_MARKERS
        if marker in normalized_answer
    ]
    if clarify_positions and min(clarify_positions) <= 90:
        return "clarify"
    if is_low_signal_question(question) and len(answer) < 220:
        return "clarify"
    return "answer"


def golden_exclusion_reasons(
    *,
    question: str,
    answer: str,
    behavior: str,
    category: str,
) -> list[str]:
    reasons: list[str] = []
    normalized_question = _normalize(question)
    normalized_answer = _normalize(answer)
    if len(question) < 8 or is_low_signal_question(question):
        reasons.append("low_signal_question")
    if len(question) > 700:
        reasons.append("question_too_long_for_golden")
    if score_question_segment(question) <= 0:
        reasons.append("answer_shaped_or_weak_question")
    if FOLLOWUP_FRAGMENT_RE.search(question):
        reasons.append("followup_without_context")
    if len(answer) < 45:
        reasons.append("answer_too_short")
    if len(answer) > 1800:
        reasons.append("answer_too_long")
    if behavior != "answer":
        reasons.append(f"behavior_{behavior}")
    if OPERATOR_JOIN_RE.search(answer):
        reasons.append("operator_join_placeholder")
    if OUTDATED_OR_TEMPORAL_RE.search(answer):
        reasons.append("temporal_answer_requires_review")
    if category not in SUPPORTED_CATEGORY:
        reasons.append("unsupported_category")
    if any(marker in normalized_answer for marker in PERSONAL_OR_MANUAL_MARKERS):
        reasons.append("personal_or_manual_answer")
    if any(marker in normalized_answer for marker in INTERNAL_PROCESS_MARKERS):
        reasons.append("internal_process_answer")
    if any(marker in normalized_question for marker in PERSONAL_QUESTION_MARKERS):
        reasons.append("personal_status_question")
    if operator_review_reason(question):
        reasons.append("routing_requires_operator")
    if any(marker in normalized_question for marker in MANUAL_REQUEST_MARKERS):
        reasons.append("manual_request_question")
    if any(
        marker in normalized_question
        for marker in (*ANSWER_SHAPE_MARKERS, *TRANSFER_MARKERS)
    ):
        reasons.append("answer_shaped_question")
    if any(
        placeholder in question
        for placeholder in ("[EMAIL]", "[ТЕЛЕФОН]", "[ДОКУМЕНТ]", "[ИМЯ]", "[ID]")
    ):
        reasons.append("question_contains_sensitive_placeholder")
    if answer.count("[") and any(
        placeholder in answer
        for placeholder in ("[EMAIL]", "[ТЕЛЕФОН]", "[ДОКУМЕНТ]", "[ИМЯ]", "[ID]")
    ):
        reasons.append("contains_sensitive_placeholder")
    if MAIL_ARTIFACT_RE.search(answer):
        reasons.append("mail_artifact")
    if question and _normalize(question) == _normalize(answer):
        reasons.append("question_equals_answer")
    return sorted(set(reasons))


def is_low_signal_question(question: str) -> bool:
    normalized = normalize_question_key(question)
    if normalized in LOW_SIGNAL_QUESTIONS:
        return True
    if not normalized or ATTACHMENT_ONLY_RE.fullmatch(question):
        return True
    words = re.findall(r"[a-zа-я0-9]{2,}", normalized)
    return len(words) <= 1 and QUESTION_WORD_RE.search(normalized) is None


def deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_question: dict[str, dict[str, Any]] = {}
    for record in records:
        key = normalize_question_key(record["query"])
        if not key:
            continue
        current = best_by_question.get(key)
        if current is None or _record_rank(record) > _record_rank(current):
            best_by_question[key] = record
    return list(best_by_question.values())


def balanced_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (
            str(record.get("category") or "unknown"),
            str(record.get("topic") or "unknown"),
            str(record.get("forum_normalized") or "unknown"),
        )
        groups[key].append(record)
    for values in groups.values():
        values.sort(key=_record_rank, reverse=True)

    keys = sorted(groups)
    result: list[dict[str, Any]] = []
    while any(groups.values()):
        for key in keys:
            if groups[key]:
                result.append(groups[key].pop(0))
    return result


def build_behavior_matrix(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (
            str(record.get("category") or "другое"),
            str(record.get("topic") or "прочее"),
            str(record.get("expected_behavior") or "answer"),
        )
        grouped[key].append(record)

    matrix: list[dict[str, Any]] = []
    for (category, topic, behavior), items in sorted(
        grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])
    ):
        examples = sorted(items, key=_record_rank, reverse=True)[:5]
        matrix.append(
            {
                "category": category,
                "topic": topic,
                "behavior": behavior,
                "frequency": len(items),
                "safe_fallback_candidates": sum(item["fallback_candidate"] for item in items),
                "example_queries": [item["query"] for item in examples],
                "operator_answer_examples": [item["reference_answer"] for item in examples[:3]],
                "policy": behavior_policy(behavior),
            }
        )
    return matrix


def behavior_policy(behavior: str) -> str:
    return {
        "answer": (
            "Ответить как бот только подтверждёнными фактами; "
            "публичный Yonote/RAG приоритетен."
        ),
        "clarify": "Задать один короткий уточняющий вопрос, не передавать оператору сразу.",
        "scope_note": "Коротко обозначить тематику бота и предложить вопрос по Росмолодёжи.",
        "escalate": "Передать оператору только персональный, ручной или рискованный кейс.",
    }.get(behavior, "Требуется ручная проверка правила.")


def build_profile(
    records: list[dict[str, Any]],
    *,
    unique_records: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    calibration: list[dict[str, Any]],
    holdout: list[dict[str, Any]],
    input_path: Path,
) -> dict[str, Any]:
    exclusions = Counter(
        reason for record in records for reason in record["golden_exclusion_reasons"]
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "input": str(input_path),
        "rows": len(records),
        "unique_questions": len(unique_records),
        "duplicate_question_rows": len(records) - len(unique_records),
        "golden_eligible": len(eligible),
        "calibration_cases": len(calibration),
        "holdout_cases": len(holdout),
        "behavior_counts": dict(
            Counter(record["expected_behavior"] for record in records).most_common()
        ),
        "category_counts": dict(Counter(record["category"] for record in records).most_common()),
        "topic_counts": dict(Counter(record["topic"] for record in records).most_common()),
        "forum_counts": dict(
            Counter(
                record.get("forum_normalized") or "unknown" for record in records
            ).most_common(50)
        ),
        "department_counts": dict(
            Counter(record.get("department") or "unknown" for record in records).most_common()
        ),
        "exclusion_counts": dict(exclusions.most_common()),
        "fallback_candidates": sum(record["fallback_candidate"] for record in records),
        "temporal_review_required": sum(record["temporal_review_required"] for record in records),
    }


def build_report(profile: dict[str, Any], behavior_matrix: list[dict[str, Any]]) -> str:
    lines = [
        "# Operator Q/A Golden Set Analysis",
        "",
        f"- Generated: `{profile['generated_at']}`",
        f"- Source rows: `{profile['rows']}`",
        f"- Unique questions: `{profile['unique_questions']}`",
        f"- Golden eligible: `{profile['golden_eligible']}`",
        f"- Calibration: `{profile['calibration_cases']}`",
        f"- Sealed holdout: `{profile['holdout_cases']}`",
        f"- Safe fallback candidates before editorial approval: `{profile['fallback_candidates']}`",
        f"- Temporal answers requiring source review: `{profile['temporal_review_required']}`",
        "",
        "## Behavior Mix",
        "",
        *_counter_lines(profile["behavior_counts"]),
        "",
        "## Exclusions",
        "",
        *_counter_lines(profile["exclusion_counts"]),
        "",
        "## Largest Behavior Groups",
        "",
    ]
    for item in behavior_matrix[:30]:
        lines.append(
            f"- `{item['frequency']}` {item['category']} / {item['topic']} / "
            f"{item['behavior']} — {item['policy']}"
        )
    lines.extend(
        [
            "",
            "## Safety Rules",
            "",
            "- Operator answers are evaluation references, not automatically published facts.",
            "- Public Yonote/XLSX/DOCX sources remain the primary answer layer.",
            "- Fallback candidates require factual and editorial approval before indexing.",
            "- Personal statuses, manual actions, internal processes and temporal "
            "claims are excluded.",
            "- Holdout cases must remain unchanged until the final blind evaluation.",
            "",
        ]
    )
    return "\n".join(lines)


def write_review_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "id",
        "query",
        "reference_answer",
        "category",
        "topic",
        "forum_normalized",
        "difficulty",
        "official_overlap_score",
        "candidate_chunk_ids",
        "temporal_review_required",
        "fallback_candidate",
        "review_status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    key: ", ".join(record.get(key) or [])
                    if key == "candidate_chunk_ids"
                    else record.get(key)
                    for key in fieldnames
                }
            )


def _record_rank(record: dict[str, Any]) -> tuple[float, int, int, str]:
    return (
        float(record.get("official_overlap_score") or 0.0),
        int(bool(record.get("forum_normalized"))),
        min(len(str(record.get("reference_answer") or "")), 1200),
        str(record.get("ticket_hash") or ""),
    )


def _assign_case_ids(records: list[dict[str, Any]], prefix: str) -> None:
    for index, record in enumerate(records, start=1):
        record["id"] = f"{prefix}_{index:03d}"
        record["expected_behavior"] = "answer"
        record["expected_escalated"] = False
        record["forbidden_answer_contains"] = [
            "я думаю",
            "скорее всего",
            "не найдено в источниках",
        ]


def _strip_mail_artifacts(text: str) -> str:
    return MAIL_ARTIFACT_RE.split(text, maxsplit=1)[0].strip()


def _strip_operator_boilerplate(text: str) -> str:
    value = text.strip()
    value = re.sub(
        r"^(?:приветствуем(?:\s+вас)?(?:,\s*[^,!]{2,60})?|"
        r"благодарим(?:\s+вас)?|спасибо)"
        r"[^.!?]{0,180}(?:служб[уаы]\s+заботы\s+росмолод[ёе]жи|"
        r"обращени[ея]|ожидани[ея])[^.!?]*[.!]\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )
    return value.strip()


def _sanitize_operator_text(text: str, masker: RegexOnlyPIIMasker) -> str:
    value = sanitize_text(text, masker)
    value = TRAILING_PERSON_SIGNATURE_RE.sub("", value)
    value = TICKET_CODE_RE.sub(r"\1[ID]", value)
    return " ".join(value.split()).strip()


def extract_reference_facts(text: str) -> list[str]:
    facts: list[str] = []
    for regex in (URL_FACT_RE, OUTDATED_OR_TEMPORAL_RE, NUMBER_FACT_RE):
        for match in regex.finditer(text):
            value = " ".join(match.group(0).split()).strip(" .,;:")
            if value and value not in facts:
                facts.append(value)
    return facts[:12]


def _normalize(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return [dict(item) for item in payload]


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _counter_lines(values: dict[str, int], limit: int = 30) -> list[str]:
    return [f"- `{count}` {name}" for name, count in list(values.items())[:limit]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build private calibration and holdout golden sets from structured operator Q/A."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--kb-seed", type=Path, default=DEFAULT_KB_SEED)
    parser.add_argument("--forums", type=Path, default=DEFAULT_FORUMS)
    parser.add_argument("--calibration-size", type=int, default=200)
    parser.add_argument("--holdout-size", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    build_operator_golden_set(
        args.input,
        args.output_dir,
        kb_seed_path=args.kb_seed,
        forums_path=args.forums,
        calibration_size=args.calibration_size,
        holdout_size=args.holdout_size,
    )


if __name__ == "__main__":
    main()
