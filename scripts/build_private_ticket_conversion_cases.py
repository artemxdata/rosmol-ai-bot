from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TYPICAL_LABEL = "Типовой"
ATYPICAL_LABEL = "Нетиповой"
IN_SCOPE_CATEGORIES = {
    "форумы",
    "гранты",
    "платформа_фгаис",
    "техподдержка",
    "навигация",
}
ESCALATION_REASONS = {
    "operator_requested",
    "technical_issue",
    "legal_or_financial_risk",
    "personal_status",
    "open_or_in_progress",
    "unsafe_or_abusive",
}
GENERIC_APPLICATION_QUERIES = {
    "подать заявку на участие",
    "как подать заявку",
    "хочу подать заявку",
}
SERVICE_OR_JUNK_MARKERS = (
    "недоставленное сообщение",
    "undelivered",
    "delivery status notification",
    "mail delivery",
    "на диске осталось",
    "mail delivery subsystem",
    "выбери, что тебя интересует",
)
ABUSIVE_MARKERS = (
    "нахуй",
    "хуй",
    "пизд",
    "ебан",
    "ёбан",
    "говн",
    "ворье",
    "ворьё",
    "фашист",
)
SAFETY_OPERATOR_MARKERS = (
    "вскрою вены",
    "вскрыть вены",
    "суицид",
    "самоуб",
    "меня обижает",
    "меня обижают",
    "буллинг",
    "травят",
    "запрещенная информация",
    "запрещённая информация",
    "пропаганда экстремизма",
    "нарушение конфиденциальности",
    "нарушающий правила ркн",
    "нарушает правила ркн",
    "канал наруш",
)
OFFTOPIC_MARKERS = (
    "билет на матч",
    "билеты на матч",
    "матчи сборной",
    "матч сборной",
    "сборной россии",
    "погода",
    "курс валют",
    "гороскоп",
)
FILE_EXTENSION_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|jpg|jpeg|png|zip|rar)\b", re.I)
NON_USER_ANSWER_MARKERS = (
    "благодарим за ожидание",
    "благодарим вас за обращение",
    "наши коллеги сообщают",
    "на вашему обращению",
    "на ваше обращение",
    "поступил ответ",
    "служба заботы росмолодежи",
    "служба заботы росмолодёжи",
    "надеюсь, мне удалось помочь",
    "я люблю быть полезным",
    "заботливый бот создан",
    "спрашивай о мероприятиях",
    "если у вас возникнут еще",
    "если у вас возникнут ещё",
    "пожалуйста, не расстраивайтесь",
    "понимаем ваше желание",
    "благодарим за информацию",
    "пересылаемое сообщение",
    "федеральное агентство по делам молодежи",
    "федеральное агентство по делам молодёжи",
    "причиной возврата мероприятий",
    "команды-победители проекта",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a private 2026 ticket conversion eval set."
    )
    parser.add_argument(
        "--input",
        default="data/private/tickets/analysis_2026_full/tickets_normalized.jsonl",
    )
    parser.add_argument(
        "--output",
        default=(
            "data/private/tickets/eval_2026_full/"
            "conversion_2026_50_typical_100_atypical.json"
        ),
    )
    parser.add_argument("--typical", type=int, default=50)
    parser.add_argument("--atypical", type=int, default=100)
    args = parser.parse_args()

    records = load_records(Path(args.input))
    typical_pool = [
        item
        for item in records
        if item.get("typical_atypical") == TYPICAL_LABEL
        and item.get("answerable_by_kb")
        and not item.get("should_escalate")
    ]
    if len(typical_pool) < args.typical:
        seen_ids = {id(item) for item in typical_pool}
        typical_pool.extend(
            item
            for item in records
            if item.get("typical_atypical") == TYPICAL_LABEL
            and not item.get("should_escalate")
            and id(item) not in seen_ids
        )

    atypical_pool = [
        item for item in records if item.get("typical_atypical") == ATYPICAL_LABEL
    ]
    cases = stratified_cases(typical_pool, args.typical, "typical")
    cases.extend(stratified_cases(atypical_pool, args.atypical, "atypical"))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(output_path),
                "cases": len(cases),
                "pools": {
                    "typical": len(typical_pool),
                    "atypical": len(atypical_pool),
                },
                "by_type": Counter(case["_typical_atypical"] for case in cases),
                "expected_behavior": Counter(case["expected_behavior"] for case in cases),
                "by_type_behavior": _string_key_counter(
                    Counter(
                        (case["_typical_atypical"], case["expected_behavior"])
                        for case in cases
                    )
                ),
                "categories": Counter(case["_category"] for case in cases),
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            query = compact_text(item.get("question_candidate") or "")
            if len(query) < 8:
                continue
            if _is_non_user_answer_candidate(normalize_text(query)):
                continue
            if len(query) > 900:
                item["question_candidate"] = query[:900]
            records.append(item)
    return records


def stratified_cases(
    items: list[dict[str, Any]],
    limit: int,
    prefix: str,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        key = (
            str(item.get("category") or "unknown"),
            str(item.get("topic") or "unknown"),
            str(item.get("escalation_reason") or "none"),
        )
        buckets[key].append(item)

    ordered_keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    while len(selected) < limit:
        progressed = False
        for key in ordered_keys:
            bucket = buckets[key]
            while bucket:
                item = bucket.pop(0)
                fingerprint = query_fingerprint(item.get("question_candidate") or "")
                if fingerprint in seen:
                    continue
                selected.append(item)
                seen.add(fingerprint)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return [build_case(item, prefix, index) for index, item in enumerate(selected, 1)]


def build_case(item: dict[str, Any], prefix: str, index: int) -> dict[str, Any]:
    behavior, expected_escalated, expected_reason = expected_behavior(item)
    return {
        "id": f"{prefix}-{index:03d}-{item.get('ticket_hash')}",
        "query": compact_text(item.get("question_candidate") or item.get("title_masked") or ""),
        "user_id": f"private-2026-{prefix}-{index:03d}",
        "channel": "api",
        "expected_behavior": behavior,
        "expected_escalated": expected_escalated,
        "expected_reason": expected_reason,
        "tags": [
            "private_2026",
            f"type:{item.get('typical_atypical')}",
            f"category:{item.get('category')}",
            f"topic:{item.get('topic')}",
            f"difficulty:{item.get('difficulty')}",
            f"answerable:{bool(item.get('answerable_by_kb'))}",
            f"escalation_reason:{item.get('escalation_reason') or 'none'}",
            f"expected_reason:{expected_reason}",
        ],
        "source_ticket_ids": [item.get("ticket_hash")],
        "_typical_atypical": item.get("typical_atypical"),
        "_category": item.get("category"),
        "_topic": item.get("topic"),
        "_difficulty": item.get("difficulty"),
        "_answerable_by_kb": bool(item.get("answerable_by_kb")),
        "_source_escalation_reason": item.get("escalation_reason"),
        "_expected_reason": expected_reason,
    }


def expected_behavior(item: dict[str, Any]) -> tuple[str, bool, str]:
    reason = item.get("escalation_reason")
    query_normalized = normalize_text(item.get("question_candidate") or "")
    if _is_service_or_junk(query_normalized):
        return "escalate", True, "service_or_junk"
    if _is_attachment_only(query_normalized):
        return "escalate", True, "attachment_only"
    if _is_abusive(query_normalized):
        return "escalate", True, "unsafe_or_abusive"
    if _is_safety_or_operator_only(query_normalized):
        return "escalate", True, "safety_or_operator_only"
    if _is_vague_or_internal(query_normalized):
        return "escalate", True, "vague_or_internal"
    if _is_private_eval_offtopic(query_normalized):
        return "scope_note", False, "offtopic"
    if item.get("should_escalate") or reason in ESCALATION_REASONS:
        return "escalate", True, str(reason or "should_escalate")

    if item.get("category") not in IN_SCOPE_CATEGORIES:
        return "scope_note", False, "out_of_scope_category"
    if item.get("needs_clarification") or query_normalized in GENERIC_APPLICATION_QUERIES:
        return "clarify", False, "needs_clarification"
    if len(query_normalized) < 18 and not item.get("forum_normalized"):
        return "clarify", False, "too_short_without_context"
    if item.get("answerable_by_kb") is False:
        return "escalate", True, "not_answerable_by_kb"
    return "answer", False, "answerable_by_eval"


def _is_service_or_junk(normalized: str) -> bool:
    return any(marker in normalized for marker in SERVICE_OR_JUNK_MARKERS)


def _is_abusive(normalized: str) -> bool:
    return any(marker in normalized for marker in ABUSIVE_MARKERS)


def _is_safety_or_operator_only(normalized: str) -> bool:
    return any(marker in normalized for marker in SAFETY_OPERATOR_MARKERS) or bool(
        re.search(r"\bменя\b.{0,40}\bобиж(?:ает|ают|али|ал|ала)\b", normalized)
    )


def _is_private_eval_offtopic(normalized: str) -> bool:
    if any(marker in normalized for marker in OFFTOPIC_MARKERS):
        return True
    return False


def _is_vague_or_internal(normalized: str) -> bool:
    if normalized in {
        "мельник мми",
        "фестивалим и концертим",
        "канал нарушающий правила ркн",
        "нарушение конфиденциальности",
        "запрос оф.письма",
        "запрос оф письма",
        "запрос официального письма",
    }:
        return True
    if normalized.startswith(("proposal:", "subject: urgent:", "subject:")):
        return True
    return False


def _is_attachment_only(normalized: str) -> bool:
    if not normalized:
        return False
    extension_count = len(FILE_EXTENSION_RE.findall(normalized))
    attachment_markers = (
        "и еще",
        "и ещё",
        "файл",
        "файла",
        "файлов",
        "вложени",
        "прикреп",
    )
    if extension_count >= 2:
        return True
    if extension_count >= 1 and any(marker in normalized for marker in attachment_markers):
        return True
    return False


def _is_non_user_answer_candidate(normalized: str) -> bool:
    return any(marker in normalized for marker in NON_USER_ANSWER_MARKERS)


def compact_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_text(text: object) -> str:
    return compact_text(text).casefold().replace("ё", "е")


def query_fingerprint(text: object) -> str:
    return re.sub(r"[^\w]+", " ", normalize_text(text))[:180]


def _json_default(value: object) -> object:
    if isinstance(value, Counter):
        return dict(value)
    return str(value)


def _string_key_counter(counter: Counter[tuple[object, ...]]) -> dict[str, int]:
    return {" / ".join(str(part) for part in key): value for key, value in counter.items()}


if __name__ == "__main__":
    main()
