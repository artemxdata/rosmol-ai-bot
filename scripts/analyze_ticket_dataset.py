from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.kb.source_extractors import read_xlsx_sheets  # noqa: E402
from src.graph.response_profiles import infer_response_profile  # noqa: E402
from src.models import QueryAnalysis  # noqa: E402
from src.response_contract import ResponseProfileName  # noqa: E402


DEFAULT_INPUT = Path("data/private/tickets/RAG_Dataset.xlsx")
DEFAULT_OUTPUT_DIR = Path("data/private/tickets/analysis")
DEFAULT_FORUMS = Path("data/forums_registry.json")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = PROJECT_ROOT / "data" / "private"
ARTIFACT_MANIFEST_NAME = "artifact_manifest.json"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "1.0.0"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")
PASSPORT_RE = re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)")
SNILS_RE = re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{3}\s?\d{2}(?!\d)")
VK_ID_RE = re.compile(r"(?<!\w)(?:vk[_\s-]?id|id)\s*[:=]?\s*\d{4,}(?!\w)", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<![\w@])@[a-zа-яё0-9_][a-zа-яё0-9_.-]{2,}", re.IGNORECASE)
LONG_ID_RE = re.compile(r"(?<!\d)\d{11,20}(?!\d)")
FIO_CONTEXT_RE = re.compile(
    r"\b(?:фио|ф\.?\s*и\.?\s*о\.?|меня зовут)\s*[:=-]?\s*"
    r"[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2}",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")

BOILERPLATE_PHRASES = [
    "заботливый бот росмолодёжи",
    "заботливый бот росмолодежи",
    "заботливый бот создан",
    "чем я могу быть полезен",
    "перевожу на оператора",
    "пожалуйста, ожидайте",
    "служба заботы отдыхает",
    "присоединюсь к диалогу",
    "благодарим за обращение в службу заботы",
    "если у вас есть к нам вопросы",
    "для оценки качества обслуживания",
    "мы уже занимаемся вашим вопросом",
    "вернемся с ответом в течение 15 минут",
    "вернёмся с ответом в течение 15 минут",
    "выбери, что тебя интересует",
    "все мероприятия росмолодёжи собраны",
    "все мероприятия росмолодежи собраны",
    "надеюсь, мне удалось помочь",
    "я люблю быть полезным",
    "ваше сообщение не доставлено",
    "недоставленное сообщение",
    "mail failure",
    "recent login from a new device",
    "вход с нового устройства",
    "код проверки для двухфакторной авторизации",
    "ответ на форму",
    "ваш запрос направлен в службу заботы",
    "мы рады, что вопрос решён",
    "мы рады, что вопрос решен",
    "приветствуем вас и благодарим за обратную связь",
    "в списке нет нужного мероприятия? мы поможем",
    "я помощник росмолодёжи",
    "я помощник росмолодежи",
    "я не совсем понял вопрос",
    "вот актуальные контакты",
    "служба заботы всегда рядом",
    "кто-то входит в ваш аккаунт",
    "2 new comments in",
    "[support] null",
]

SUPPORT_ANSWER_MARKERS = [
    "можно",
    "необходимо",
    "нужно",
    "для этого",
    "для того",
    "чтобы",
    "перейдите",
    "проверьте",
    "обратитесь",
    "подать заяв",
    "подается",
    "подаётся",
    "регистрация",
    "личный кабинет",
    "поступил ответ",
    "обрати внимание",
    "если",
]

ANSWER_OPENING_MARKERS = [
    "здесь все зависит",
    "здесь всё зависит",
    "для этого",
    "для того чтобы",
    "обрати внимание",
    "подать заявку можно",
    "регистрация проходит",
    "перейдите",
    "проверьте",
    "необходимо",
    "поступил ответ",
    "к сожалению",
]

USER_REQUEST_MARKERS = [
    " я ",
    " мне ",
    " меня ",
    " мой ",
    " моя ",
    " мои ",
    " моего ",
    " моему ",
    "прошу",
    "подскажите",
    "помогите",
    "не могу",
    "не успел",
    "не пришло",
    "не получил",
    "почему",
    "можете пожалуйста",
    "хочу",
    "возник",
    "буду ждать",
]

QUESTION_MARKERS = [
    "как ",
    "где ",
    "что ",
    "когда ",
    "куда ",
    "почему ",
    "можно ли",
    "что делать",
    "как быть",
    "подскажите",
    "помогите",
]

THREAD_ARTIFACT_MARKERS = [
    "commented",
    "служба заботы росмолодёжи:",
    "служба заботы росмолодежи:",
    "запрос отправлял",
    "запрос отправляла",
    "отправлено из",
    "с уважением",
]
THREAD_TIMESTAMP_RE = re.compile(r"\b\d{4}\s*г\.\s*,?\s*\d{1,2}:\d{2}\b", re.IGNORECASE)

LOW_SIGNAL_TITLE_MARKERS = [
    "личное сообщение",
    "входящий вызов",
    "служба заботы",
    "обращение",
    "без темы",
    "image-",
    ".jpeg",
    ".jpg",
    ".png",
    "re:",
    "fw:",
    "fwd:",
    "ответ на форму",
    "ваше сообщение не доставлено",
    "недоставленное сообщение",
    "mail failure",
    "recent login from a new device",
    "вход с нового устройства",
    "код проверки",
    "двухфакторной авторизации",
    "request from",
]

PROFANITY_MARKERS = [
    "хер",
    "пизд",
    "дерьм",
    "мраз",
    "урод",
    "бред",
    "маразм",
]

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "гранты",
        (
            "грант",
            "средств",
            "субсид",
            "проект",
            "отчет",
            "отчёт",
            "договор",
            "смет",
        ),
    ),
    (
        "платформа_фгаис",
        (
            "фгаис",
            "myrosmol",
            "личный кабинет",
            "профил",
            "id проф",
            "авторизац",
            "регистрац",
            "заявк",
        ),
    ),
    (
        "техподдержка",
        (
            "ошибк",
            "не работает",
            "не могу войти",
            "парол",
            "код",
            "кнопк",
            "доступ",
            "сайт",
            "техничес",
        ),
    ),
    (
        "форумы",
        (
            "форум",
            "смен",
            "мероприят",
            "участ",
            "прожив",
            "проезд",
            "трансфер",
            "питани",
            "письмо-вызов",
            "письмо вызов",
        ),
    ),
    (
        "навигация",
        (
            "оператор",
            "куда обратиться",
            "контакт",
            "телефон",
            "почт",
            "статус",
            "специалист",
        ),
    ),
]

TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("регистрация_и_заявка", ("регистрац", "подать заяв", "заявк", "дедлайн")),
    ("статус_заявки", ("статус", "результат", "отбор", "прошел", "прошёл", "резерв")),
    ("письмо_и_уведомления", ("письмо", "уведомлен", "не пришло", "почт")),
    ("личный_кабинет_и_профиль", ("профил", "личный кабинет", "id проф", "аккаунт")),
    ("доступ_и_техническая_ошибка", ("не могу войти", "ошибк", "парол", "код", "кнопк")),
    ("возраст_и_условия_участия", ("возраст", "лет", "ограничен", "кто может")),
    ("проезд_и_трансфер", ("проезд", "дорог", "трансфер", "перелет", "перелёт")),
    ("проживание_и_питание", ("прожив", "питани", "гостиниц", "общежит")),
    ("документы", ("документ", "паспорт", "справк", "согласие")),
    ("грантовая_отчетность", ("отчет", "отчёт", "договор", "смет", "вернуть средств")),
    ("контакты_и_оператор", ("оператор", "контакт", "телефон", "куда писать")),
]

ESCALATION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("personal_status", ("мой статус", "статус заявки", "почему отказ", "результат отбора")),
    ("technical_issue", ("не могу войти", "не работает", "ошибка", "код не приходит")),
    ("operator_requested", ("оператор", "специалист", "живой человек")),
    ("legal_or_financial_risk", ("вернуть средства", "суд", "жалоб", "прокурат", "мвд")),
    ("unsafe_or_abusive", tuple(PROFANITY_MARKERS)),
]

ASSISTANT_CLARIFICATION_MARKERS = (
    "уточните, пожалуйста",
    "уточни, пожалуйста",
    "пожалуйста, уточните",
    "пожалуйста, уточни",
    "уточните название",
    "уточни название",
    "название форума, о котором",
    "даты какого мероприятия",
    "о каком мероприятии",
    "какого мероприятия",
)
ASSISTANT_DATA_REQUEST_RE = re.compile(
    r"^(?:здравствуйте[,!.\s-]*)?"
    r"(?:пожалуйста[,!.\s-]*)?"
    r"(?:подскажите|напишите|пришлите|укажите|уточните)"
    r"(?:,?\s+пожалуйста)?[,!:\s-]+"
    r"(?:номер|id\b|идентификатор|почт|email\b|телефон|фио\b|"
    r"название|дату рождения|ссылк|скриншот|текст ошибки|какая ошибка|"
    r"что именно)",
    re.IGNORECASE,
)
USER_PROBLEM_RE = re.compile(
    r"\b(?:не работает|не получается|не могу|ошибка|не приходит|не приш[её]л|"
    r"не открывается|не загружается|не отображается|пропал[аио]?|завис(?:ло|ла)?)\b",
    re.IGNORECASE,
)
ASSISTANT_INSTRUCTION_MARKERS = (
    "попробуйте",
    "проверьте",
    "перейдите",
    "обратитесь",
    "ожидайте",
    "повторите",
    "необходимо",
    "для этого",
    "к сожалению",
)
PROBLEM_EXPLANATION_MARKERS = (
    "из-за",
    "потому что",
    "так как",
    "по причине",
    "возникает",
    "связана с",
    "связан с",
    "объясняется",
)
ASSISTANT_STATUS_CHECK_MARKERS = (
    "удалось",
    "получилось",
    "подавали заяв",
    "подали заяв",
    "зарегистрировал",
    "получили",
    "видите",
    "пробовали",
    "проверили",
    "актуальн",
    "решил",
    "сохраняется",
    "осталась",
    "осталось",
    "нужен",
    "нужна",
    "нужны",
    "интересует",
    "планируете",
    "хотите",
)
ASSISTANT_STATUS_SUBJECTS = (
    "проблема",
    "ошибка",
    "вопрос",
    "ситуация",
    "обращение",
)
ASSISTANT_PROMISE_RE = re.compile(
    r"^(?:здравствуйте[,!.\s-]*)?"
    r"(?:я|мы)\s+(?:уже\s+)?"
    r"(?:передал(?:а|и)?|передам|передадим|уточню|уточним|"
    r"проверю|проверим|узнаю|узнаем|направил(?:а|и)?|направлю|направим|"
    r"свяжусь|свяжемся|вернусь|вернемся|вернёмся)\b",
    re.IGNORECASE,
)
STRONG_USER_REQUEST_MARKERS = (
    "прошу",
    "подскажите",
    "подскажи",
    "помогите",
    "помоги",
    "не могу",
    "не успел",
    "не успела",
    "не пришло",
    "не получил",
    "не получила",
    "можете пожалуйста",
    "можешь пожалуйста",
    "хочу",
    "возник",
)
STRONG_USER_QUESTION_STARTERS = (
    "как ",
    "где ",
    "что ",
    "когда ",
    "куда ",
    "почему ",
    "кто ",
    "сколько ",
    "какой ",
    "какая ",
    "какие ",
    "можно ли",
    "есть ли",
    "будет ли",
    "будут ли",
    "нужен ли",
    "нужна ли",
    "нужны ли",
)
TOPICAL_USER_QUESTION_MARKERS = (
    "форум",
    "мероприят",
    "заяв",
    "регистрац",
    "трансфер",
    "проезд",
    "прожив",
    "питан",
    "документ",
    "паспорт",
    "справк",
    "грант",
    "программ",
    "дат",
    "отбор",
    "участ",
)
TOPICAL_YES_NO_RE = re.compile(
    r"\b(?:будет|будут|есть|нужен|нужна|нужны|предусмотрен[аоы]?|"
    r"включен[аоы]?|оплачивается|компенсируется)\b",
    re.IGNORECASE,
)

DialogueRole = Literal["user", "assistant", "system", "unknown"]
AssistantKind = Literal["bot", "operator", "unknown"]
RoleConfidence = Literal["high", "medium", "low"]
CRITICAL_FORBIDDEN_RESPONSE_PROFILES: dict[str, tuple[str, ...]] = {
    "dates": ("application", "selection_status", "travel"),
    "application": ("dates", "selection_status", "travel"),
    "selection_status": ("application", "dates", "travel"),
    "travel": ("application", "selection_status"),
}


@dataclass(frozen=True)
class ForumAlias:
    normalized: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ReconstructedTurn:
    index: int
    role: DialogueRole
    assistant_kind: AssistantKind | None
    text_masked: str
    role_confidence: RoleConfidence
    role_reason: str
    question_score: int
    answer_score: int
    has_pii: bool
    pii_types: tuple[str, ...]

    def to_private_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.index,
            "role": self.role,
            "assistant_kind": self.assistant_kind,
            "text_masked": self.text_masked,
            "role_confidence": self.role_confidence,
            "role_reason": self.role_reason,
            "question_score": self.question_score,
            "answer_score": self.answer_score,
            "has_pii": self.has_pii,
            "pii_types": list(self.pii_types),
        }


@dataclass(frozen=True)
class ProductSplitAssignment:
    split: str
    duplicate_component_id: str
    crosses_boundary: bool
    forced_by_role_review: bool


@dataclass(frozen=True)
class ProductSplitPlan:
    assignments: dict[str, ProductSplitAssignment]
    validation_cutoff: tuple[int, int, int, int, int, int] | None
    holdout_cutoff: tuple[int, int, int, int, int, int] | None


def analyze_dataset(
    input_path: Path,
    output_dir: Path,
    forums_path: Path,
    *,
    max_golden: int,
    max_pairs: int,
) -> dict[str, Any]:
    ensure_private_output_dir(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    forums = load_forum_aliases(forums_path)
    rows = load_ticket_rows(input_path)
    normalized = [normalize_ticket(row, forums) for row in rows]

    profile = build_profile(normalized, input_path)
    taxonomy_rows = build_taxonomy(normalized)
    top_questions = build_top_questions(normalized)
    golden = build_golden_candidates(normalized, max_items=max_golden)
    pairs = build_reranker_pairs(golden, normalized, max_pairs=max_pairs)
    product_split_plan = build_product_split_plan(normalized, forums)
    product_splits, product_split_summary = build_product_eval_splits(
        normalized,
        forums,
        split_plan=product_split_plan,
    )
    conversation_splits, conversation_split_summary = (
        build_product_conversation_splits(
            normalized,
            forums,
            split_plan=product_split_plan,
        )
    )
    role_review_queue = build_product_role_review_queue(normalized)
    gap_report = build_gap_report(profile, taxonomy_rows, top_questions)

    staging_dir = create_artifact_staging_dir(output_dir)
    try:
        write_json(staging_dir / "dataset_profile.json", profile)
        write_jsonl(staging_dir / "tickets_normalized.jsonl", normalized)
        write_csv(staging_dir / "intent_taxonomy.csv", taxonomy_rows)
        write_markdown(
            staging_dir / "top_questions.md",
            top_questions_to_markdown(top_questions),
        )
        write_json(staging_dir / "golden_set_candidates.json", golden)
        write_jsonl(staging_dir / "reranker_calibration_pairs.jsonl", pairs)
        for split_name, cases in product_splits.items():
            write_jsonl(staging_dir / f"product_{split_name}_cases.jsonl", cases)
        write_json(staging_dir / "product_split_summary.json", product_split_summary)
        for split_name, conversations in conversation_splits.items():
            write_json_array(
                staging_dir / f"product_{split_name}_conversations.json",
                conversations,
            )
        write_json(
            staging_dir / "product_conversation_split_summary.json",
            conversation_split_summary,
        )
        write_jsonl(
            staging_dir / "product_role_review_queue.jsonl",
            role_review_queue,
        )
        write_markdown(staging_dir / "kb_gap_report.md", gap_report)
        write_markdown(
            staging_dir / "analysis_summary.md",
            build_summary(profile, golden, pairs, output_dir),
        )
        artifact_manifest = build_artifact_manifest(
            staging_dir,
            input_path=input_path,
            forums_path=forums_path,
        )
        write_json(staging_dir / ARTIFACT_MANIFEST_NAME, artifact_manifest)
        promote_staged_artifacts(staging_dir, output_dir)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    validate_artifact_manifest(output_dir)

    return {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "tickets_total": profile["tickets_total"],
        "golden_candidates": len(golden),
        "reranker_pairs": len(pairs),
        "product_eval_candidates": product_split_summary["total"],
        "product_split_counts": product_split_summary["split_counts"],
        "product_conversation_candidates": conversation_split_summary["total"],
        "product_conversation_split_counts": conversation_split_summary["split_counts"],
        "role_review_queue": len(role_review_queue),
        "artifact_manifest": str(output_dir / ARTIFACT_MANIFEST_NAME),
        "artifact_count": artifact_manifest["artifact_count"],
        "top_categories": profile["category_counts"],
        "top_escalation_reasons": profile["escalation_reason_counts"],
    }


def ensure_private_output_dir(output_dir: Path) -> Path:
    resolved = output_dir.resolve()
    private_root = PRIVATE_DATA_ROOT.resolve()
    if not resolved.is_relative_to(private_root):
        raise ValueError(
            f"Private ticket artifacts must stay under {private_root}"
        )
    return resolved


def load_ticket_rows(path: Path) -> list[dict[str, str]]:
    sheets = read_xlsx_sheets(path)
    if not sheets:
        raise ValueError("xlsx workbook has no readable sheets")
    first_sheet = next(iter(sheets.values()))
    if not first_sheet:
        raise ValueError("xlsx first sheet is empty")

    headers = [cell.strip() for cell in first_sheet[0].cells]
    rows: list[dict[str, str]] = []
    for source_row in first_sheet[1:]:
        row = {header: source_row.cell(index) for index, header in enumerate(headers)}
        if not any(value for value in row.values()):
            continue
        if rows and _is_ticket_continuation(row, rows[-1]):
            rows[-1]["messages"] = _join_message_parts(
                rows[-1].get("messages") or "",
                row.get("messages") or "",
            )
            continue
        rows.append(row)
    return rows


def _is_ticket_continuation(row: dict[str, str], previous: dict[str, str]) -> bool:
    ticket_id = str(row.get("id") or "").strip()
    if not ticket_id or ticket_id != str(previous.get("id") or "").strip():
        return False
    if str(row.get("unique_id") or "").strip():
        return False
    return not any(
        str(value or "").strip()
        for key, value in row.items()
        if key not in {"id", "messages"}
    )


def _join_message_parts(left: str, right: str) -> str:
    parts = [str(part or "").strip() for part in (left, right)]
    return "\n".join(part for part in parts if part)


def normalize_ticket(row: dict[str, str], forums: list[ForumAlias]) -> dict[str, Any]:
    ticket_id_raw = row.get("id") or row.get("unique_id") or ""
    title_raw = compact_text(row.get("title") or "")
    messages_raw = compact_text(row.get("messages") or "")
    ticket_hash = private_id_hash(ticket_id_raw or messages_raw or title_raw)
    segments = split_message_segments(messages_raw)
    reconstructed_turns = reconstruct_dialogue_turns(segments)
    high_confidence_user_segments = [
        turn.text_masked
        for turn in reconstructed_turns
        if turn.role == "user" and turn.role_confidence == "high"
    ]
    meaningful_segments = [segment for segment in segments if not is_boilerplate(segment)]
    text_for_classification = " ".join([title_raw, *meaningful_segments[:8]])

    title_masked, title_pii = mask_pii(title_raw)
    messages_masked, messages_pii = mask_pii(messages_raw)
    question_candidate = choose_question_candidate(
        "",
        high_confidence_user_segments,
    )
    title_role_review_candidate = (
        choose_question_candidate(title_raw, [])
        if not question_candidate
        else ""
    )
    title_role_review_masked, title_role_review_pii = mask_pii(
        title_role_review_candidate
    )
    answer_candidate = choose_answer_candidate(meaningful_segments, question_candidate)
    question_masked, question_pii = mask_pii(question_candidate)
    answer_masked, answer_pii = mask_pii(answer_candidate)

    category = classify_category(text_for_classification)
    topic = classify_topic(text_for_classification)
    query_category = classify_category(question_candidate)
    query_topic = classify_topic(question_candidate)
    query_forum = detect_forum(question_candidate, forums)
    query_escalation_reason = classify_escalation(question_candidate, {})
    query_should_escalate = query_escalation_reason is not None
    query_needs_clarification = needs_clarification(question_candidate)
    response_profile = (
        infer_response_profile(
            QueryAnalysis(
                category=query_category,
                is_technical=query_category == "техподдержка",
            ),
            question_candidate,
        ).value
        if question_candidate
        else "unresolved"
    )
    forum = detect_forum(text_for_classification, forums)
    escalation_reason = classify_escalation(text_for_classification, row)
    should_escalate = escalation_reason is not None
    difficulty = classify_difficulty(
        text_for_classification,
        typical=row.get("typical_atypical") or "",
        should_escalate=should_escalate,
    )
    answerable_by_kb = (
        bool(question_candidate)
        and
        bool(answer_candidate)
        and not should_escalate
        and category in {"форумы", "гранты", "платформа_фгаис", "навигация"}
    )

    pii_types = sorted(set(title_pii + messages_pii + question_pii + answer_pii))
    role_counts = Counter(turn.role for turn in reconstructed_turns)
    review_required_turns_count = sum(
        turn.role == "unknown" or turn.role_confidence != "high"
        for turn in reconstructed_turns
    )
    high_confidence_user_turns_count = sum(
        turn.role == "user" and turn.role_confidence == "high"
        for turn in reconstructed_turns
    )
    if high_confidence_user_turns_count == 0:
        role_reconstruction_status = "unresolved"
    elif review_required_turns_count:
        role_reconstruction_status = "partial"
    else:
        role_reconstruction_status = "complete"
    return {
        "ticket_id": f"ticket::{ticket_hash}",
        "ticket_hash": ticket_hash,
        "unique_id": (
            f"ticket::{private_id_hash(row['unique_id'])}"
            if row.get("unique_id")
            else ""
        ),
        "created_at": row.get("date_created") or "",
        "updated_at": row.get("date_updated") or "",
        "closed_at": row.get("date_closed") or "",
        "channel": row.get("department") or "",
        "status": row.get("status") or "",
        "typical_atypical": row.get("typical_atypical") or "",
        "responsible_present": bool(row.get("responsible_name")),
        "title_masked": title_masked,
        "title_role_review_candidate": title_role_review_masked,
        "title_requires_role_review": bool(title_role_review_masked),
        "title_role_review_has_pii": bool(title_role_review_pii),
        "title_role_review_pii_types": title_role_review_pii,
        "question_candidate": question_masked,
        "answer_candidate": answer_masked,
        "messages_masked": messages_masked,
        "dialogue_turns": [
            turn.to_private_dict()
            for turn in reconstructed_turns
        ],
        "dialogue_turns_count": len(reconstructed_turns),
        "user_turns_count": role_counts["user"],
        "high_confidence_user_turns_count": high_confidence_user_turns_count,
        "assistant_turns_count": role_counts["assistant"],
        "system_turns_count": role_counts["system"],
        "unknown_turns_count": role_counts["unknown"],
        "review_required_turns_count": review_required_turns_count,
        "role_reconstruction_status": role_reconstruction_status,
        "multi_turn_user_candidate": high_confidence_user_turns_count >= 2,
        "message_segments_count": len(segments),
        "meaningful_segments_count": len(meaningful_segments),
        "category": category,
        "topic": topic,
        "intent": build_intent(category, topic),
        "subintent": "",
        "query_category": query_category,
        "query_topic": query_topic,
        "query_forum_normalized": query_forum,
        "query_needs_clarification": query_needs_clarification,
        "query_should_escalate": query_should_escalate,
        "query_escalation_reason": query_escalation_reason,
        "response_profile": response_profile,
        "forum_normalized": forum,
        "has_pii": bool(pii_types),
        "pii_types": pii_types,
        "answerable_by_kb": answerable_by_kb,
        "needs_clarification": needs_clarification(text_for_classification),
        "should_escalate": should_escalate,
        "escalation_reason": escalation_reason,
        "difficulty": difficulty,
        "quality_notes": build_quality_notes(row, question_candidate, answer_candidate),
    }


def split_message_segments(messages: str) -> list[str]:
    if not messages:
        return []
    segments = [compact_text(segment) for segment in re.split(r"\s+---\s+", messages)]
    return [segment for segment in segments if segment]


def reconstruct_dialogue_turns(segments: list[str]) -> list[ReconstructedTurn]:
    """Recover conservative message roles without inventing missing speaker metadata."""

    turns: list[ReconstructedTurn] = []
    previous_role: DialogueRole | None = None
    previous_text = ""
    for index, segment in enumerate(segments):
        turn = classify_segment_role(
            segment,
            index=index,
            previous_role=previous_role,
            previous_text=previous_text,
        )
        turns.append(turn)
        previous_role = turn.role
        previous_text = segment
    return turns


def classify_segment_role(
    segment: str,
    *,
    index: int,
    previous_role: DialogueRole | None = None,
    previous_text: str = "",
) -> ReconstructedTurn:
    """Classify only strong role signals and fail closed to ``unknown``."""

    role_text = _content_after_leading_boilerplate(segment)
    mixed_service_prefix = role_text != compact_text(segment)
    normalized = normalize_for_match(role_text)
    question_score = score_question_segment(role_text)
    answer_score = score_answer_segment(role_text)
    explicit_user_signal = _has_strong_user_intent(normalized)
    strong_user = (
        question_score >= 6
        and question_score - answer_score >= 4
        and explicit_user_signal
    )

    if is_boilerplate(segment) and not mixed_service_prefix:
        role: DialogueRole = "system"
        confidence: RoleConfidence = "high"
        reason = "canonical_system_copy"
        assistant_kind: AssistantKind | None = "bot"
    elif _looks_like_assistant_data_request(normalized):
        role = "assistant"
        confidence = "medium"
        reason = "assistant_data_request"
        assistant_kind = "unknown"
    elif _looks_like_assistant_status_check(normalized):
        role = "assistant"
        confidence = "medium"
        reason = "assistant_status_check"
        assistant_kind = "unknown"
    elif _looks_like_assistant_promise(normalized):
        role = "assistant"
        confidence = "medium"
        reason = "assistant_followup_promise"
        assistant_kind = "unknown"
    elif (
        _looks_like_assistant_clarification(normalized)
        and not explicit_user_signal
    ):
        role = "assistant"
        confidence = "medium"
        reason = "assistant_clarification_prompt"
        assistant_kind = "unknown"
    elif _looks_like_problem_explanation(normalized):
        role = "assistant"
        confidence = "medium"
        reason = "assistant_problem_explanation"
        assistant_kind = "unknown"
    elif (
        problem_confidence := _user_problem_report_confidence(normalized)
    ) is not None:
        role = "user"
        confidence = problem_confidence
        reason = (
            "user_request_after_system_copy"
            if mixed_service_prefix
            else "reported_problem"
            if problem_confidence == "high"
            else "ambiguous_reported_problem"
        )
        assistant_kind = None
    elif strong_user:
        role = "user"
        confidence = "high"
        reason = (
            "user_request_after_system_copy"
            if mixed_service_prefix
            else "strong_question_or_request"
        )
        assistant_kind = None
    elif answer_score >= 4 and answer_score - question_score >= 4:
        role = "assistant"
        confidence = "high"
        reason = "strong_answer_copy"
        assistant_kind = "unknown"
    elif (
        previous_role in {"assistant", "system"}
        and _looks_like_assistant_clarification(normalize_for_match(previous_text))
        and _looks_like_short_clarification_reply(segment)
    ):
        role = "user"
        confidence = "medium"
        reason = "short_reply_after_clarification"
        assistant_kind = None
    else:
        role = "unknown"
        confidence = "low"
        reason = "ambiguous_without_speaker_metadata"
        assistant_kind = None

    text_for_turn = role_text if role == "user" and mixed_service_prefix else segment
    text_masked, pii_types = mask_pii(text_for_turn)
    return ReconstructedTurn(
        index=index,
        role=role,
        assistant_kind=assistant_kind,
        text_masked=text_masked,
        role_confidence=confidence,
        role_reason=reason,
        question_score=question_score,
        answer_score=answer_score,
        has_pii=bool(pii_types),
        pii_types=tuple(pii_types),
    )


def _looks_like_assistant_clarification(normalized: str) -> bool:
    return (
        "?" in normalized
        and any(marker in normalized for marker in ASSISTANT_CLARIFICATION_MARKERS)
    )


def _looks_like_assistant_data_request(normalized: str) -> bool:
    return bool(ASSISTANT_DATA_REQUEST_RE.match(normalized))


def _looks_like_assistant_status_check(normalized: str) -> bool:
    if "?" not in normalized:
        return False
    padded = f" {normalized} "
    second_person = any(
        marker in padded
        for marker in (
            " вы ",
            " вам ",
            " вас ",
            " у вас ",
            " ваш ",
            " ваша ",
            " ваши ",
        )
    )
    issue_subject = any(
        normalized.startswith(subject)
        for subject in ASSISTANT_STATUS_SUBJECTS
    )
    return (
        second_person or issue_subject
    ) and any(marker in normalized for marker in ASSISTANT_STATUS_CHECK_MARKERS)


def _looks_like_assistant_promise(normalized: str) -> bool:
    return bool(ASSISTANT_PROMISE_RE.match(normalized))


def _has_strong_user_intent(normalized: str) -> bool:
    padded = f" {normalized} "
    if any(marker in padded for marker in STRONG_USER_REQUEST_MARKERS):
        return True

    leading = re.sub(
        r"^(?:здравствуйте|добрый день|добрый вечер|привет)[,!.:\s-]*",
        "",
        normalized,
    )
    leading = re.sub(r"^(?:а|и)\s+", "", leading)
    if any(leading.startswith(marker) for marker in STRONG_USER_QUESTION_STARTERS):
        return True

    return (
        "?" in normalized
        and bool(TOPICAL_YES_NO_RE.search(normalized))
        and any(marker in normalized for marker in TOPICAL_USER_QUESTION_MARKERS)
    )


def _looks_like_user_problem_report(normalized: str) -> bool:
    return _user_problem_report_confidence(normalized) is not None


def _user_problem_report_confidence(
    normalized: str,
) -> Literal["high", "medium"] | None:
    if not USER_PROBLEM_RE.search(normalized):
        return None
    if len(normalized) > 240:
        return None
    if any(marker in normalized for marker in ASSISTANT_INSTRUCTION_MARKERS):
        return None
    if _looks_like_problem_explanation(normalized):
        return None
    if any(
        marker in f" {normalized} "
        for marker in (" не могу ", " у меня ", " мне ", " мой ", " моя ")
    ):
        return "high"
    if len(normalized.split()) <= 8:
        return "medium"
    return None


def _looks_like_problem_explanation(normalized: str) -> bool:
    return any(marker in normalized for marker in PROBLEM_EXPLANATION_MARKERS)


def _content_after_leading_boilerplate(segment: str) -> str:
    compact = compact_text(segment)
    normalized = normalize_for_match(compact)
    prefix_end = 0
    for phrase in BOILERPLATE_PHRASES:
        position = normalized.find(phrase)
        if 0 <= position <= 80:
            prefix_end = max(prefix_end, position + len(phrase))
    if not prefix_end:
        return compact
    suffix = compact[prefix_end:].lstrip(" \t\r\n,.:;!?—-–|")
    if len(suffix) < 8 or is_boilerplate(suffix):
        return compact
    return suffix


def _looks_like_short_clarification_reply(text: str) -> bool:
    normalized = normalize_for_match(text)
    if not 1 <= len(normalized) <= 120:
        return False
    if len(normalized.split()) > 12:
        return False
    if is_boilerplate(text) or URL_RE.search(text):
        return False
    if any(marker in normalized for marker in THREAD_ARTIFACT_MARKERS):
        return False
    return score_answer_segment(text) <= 0


def is_boilerplate(text: str) -> bool:
    normalized = normalize_for_match(text)
    if len(normalized) < 4:
        return True
    if URL_RE.fullmatch(text.strip()):
        return True
    return any(phrase in normalized for phrase in BOILERPLATE_PHRASES)


def choose_question_candidate(title: str, segments: list[str]) -> str:
    candidates: list[tuple[int, int, str]] = []
    for index, segment in enumerate(segments):
        score = score_question_segment(segment)
        if score > 0:
            candidates.append((score, -index, segment))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1], -len(item[2])))[2][:1000]

    title_score = score_question_segment(title)
    if (
        8 <= len(title) <= 500
        and not is_low_signal_title(title)
        and (
            title_score > 0
            or title_score == 0
            and _is_request_like_title(title)
        )
    ):
        return title
    return ""


def score_question_segment(segment: str) -> int:
    normalized = normalize_for_match(segment)
    padded_prefix = f" {normalized[:240]} "
    if not normalized or is_boilerplate(segment):
        return -100

    score = 0
    user_signal = sum(marker in padded_prefix for marker in USER_REQUEST_MARKERS)
    leading = re.sub(
        r"^(?:здравствуйте|добрый день|добрый вечер|привет)[,!.:\s-]*",
        "",
        normalized,
    )
    leading_question_signal = sum(
        leading.startswith(marker.strip())
        for marker in QUESTION_MARKERS
    )
    if leading.startswith("вопрос ") or leading.startswith("вопрос по"):
        leading_question_signal += 1

    score += 8 * user_signal
    score += 7 * leading_question_signal
    if "?" in segment:
        score += 6
    if _looks_like_user_problem_report(normalized):
        score += 6
    if re.search(r"\b(?:не могу|не получается|не пришло|не получил|не успел)\b", normalized):
        score += 4
    if len(segment) > 220 and not user_signal and not leading_question_signal:
        score -= 12

    score -= 9 * sum(marker in normalized for marker in ANSWER_OPENING_MARKERS)
    score -= 12 * sum(marker in normalized for marker in THREAD_ARTIFACT_MARKERS)
    if THREAD_TIMESTAMP_RE.search(normalized):
        score -= 12
    if len(segment) > 500:
        score -= 4
    if len(segment) > 1000:
        score -= 8
    if len(segment) < 8:
        score -= 8
    return score


def is_low_signal_title(title: str) -> bool:
    normalized = normalize_for_match(title)
    if not normalized:
        return True
    if any(marker in normalized for marker in LOW_SIGNAL_TITLE_MARKERS):
        return True
    words = re.findall(r"[a-zа-я0-9]{3,}", normalized)
    return len(words) <= 1


def _is_request_like_title(title: str) -> bool:
    normalized = normalize_for_match(title)
    if len(normalized) > 180:
        return False
    return any(
        marker in normalized
        for marker in (
            "вопрос",
            "заявк",
            "ошиб",
            "не работ",
            "регистрац",
            "статус",
            "документ",
            "трансфер",
            "проезд",
            "прожив",
            "питан",
            "грант",
            "помощ",
            "участ",
            "доступ",
            "парол",
            "письм",
            "результат",
            "срок",
            "дата",
            "как ",
            "когда ",
            "где ",
            "почему ",
            "можно ",
        )
    )


def choose_answer_candidate(segments: list[str], question: str) -> str:
    candidates: list[tuple[int, int, str]] = []
    question_norm = normalize_for_match(question)
    for index, segment in enumerate(segments):
        norm = normalize_for_match(segment)
        if norm == question_norm or is_boilerplate(segment):
            continue
        if len(segment) < 50:
            continue
        score = score_answer_segment(segment)
        if score > 0:
            candidates.append((score, index, segment))
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: (item[0], item[1]))
    return best[2][:2000]


def score_answer_segment(segment: str) -> int:
    normalized = f" {normalize_for_match(segment)} "
    score = min(4, len(segment) // 180)
    score += 4 * sum(marker in normalized for marker in SUPPORT_ANSWER_MARKERS)
    score -= 6 * sum(marker in normalized for marker in USER_REQUEST_MARKERS)
    score -= 8 * sum(marker in normalized for marker in THREAD_ARTIFACT_MARKERS)
    if THREAD_TIMESTAMP_RE.search(normalized):
        score -= 8
    if "?" in segment:
        score -= 4
    user_fragment_start = r"^\s*(?:\(|и\s+|а\s+|не\s+могу|не\s+успел|прошу|подскажите|хочу)"
    if re.match(user_fragment_start, segment, re.IGNORECASE):
        score -= 5
    return score


def classify_category(text: str) -> str:
    normalized = normalize_for_match(text)
    scores: Counter[str] = Counter()
    for category, markers in CATEGORY_RULES:
        for marker in markers:
            if marker in normalized:
                scores[category] += 1
    if not scores:
        return "другое"
    return scores.most_common(1)[0][0]


def classify_topic(text: str) -> str:
    normalized = normalize_for_match(text)
    scores: Counter[str] = Counter()
    for topic, markers in TOPIC_RULES:
        for marker in markers:
            if marker in normalized:
                scores[topic] += 1
    if not scores:
        return "прочее"
    return scores.most_common(1)[0][0]


def classify_escalation(text: str, row: dict[str, str]) -> str | None:
    normalized = normalize_for_match(text)
    if (row.get("status") or "").strip() in {"open", "v-processe"}:
        return "open_or_in_progress"
    for reason, markers in ESCALATION_RULES:
        if any(marker in normalized for marker in markers):
            return reason
    return None


def classify_difficulty(text: str, *, typical: str, should_escalate: bool) -> str:
    normalized = normalize_for_match(text)
    multi_topic_markers = sum(
        1 for _topic, markers in TOPIC_RULES if any(marker in normalized for marker in markers)
    )
    if should_escalate or "нетиповой" in normalize_for_match(typical) or multi_topic_markers >= 3:
        return "complex"
    if len(text) > 350 or multi_topic_markers == 2:
        return "medium"
    return "simple"


def detect_forum(text: str, forums: list[ForumAlias]) -> str | None:
    normalized = normalize_for_match(text)
    matches: list[tuple[int, str]] = []
    for forum in forums:
        for alias in forum.aliases:
            alias_norm = normalize_for_match(alias)
            if alias_norm and alias_norm in normalized:
                matches.append((len(alias_norm), forum.normalized))
    if not matches:
        return None
    return sorted(matches, reverse=True)[0][1]


def needs_clarification(text: str) -> bool:
    normalized = normalize_for_match(text)
    if len(normalized) < 20:
        return True
    vague = ("вопрос", "помогите", "не понятно", "что делать", "как быть")
    return any(marker == normalized for marker in vague)


def build_profile(records: list[dict[str, Any]], input_path: Path) -> dict[str, Any]:
    return {
        "input_file": str(input_path),
        "tickets_total": len(records),
        "status_counts": dict(Counter(item["status"] or "empty" for item in records).most_common()),
        "channel_counts": dict(
            Counter(item["channel"] or "empty" for item in records).most_common()
        ),
        "typical_counts": dict(
            Counter(item["typical_atypical"] or "empty" for item in records).most_common()
        ),
        "category_counts": dict(Counter(item["category"] for item in records).most_common()),
        "topic_counts": dict(Counter(item["topic"] for item in records).most_common()),
        "response_profile_counts": dict(
            Counter(
                item.get("response_profile") or "generic"
                for item in records
            ).most_common()
        ),
        "forum_counts": dict(
            Counter(item["forum_normalized"] or "unknown" for item in records).most_common(50)
        ),
        "difficulty_counts": dict(Counter(item["difficulty"] for item in records).most_common()),
        "escalation_reason_counts": dict(
            Counter(item["escalation_reason"] or "none" for item in records).most_common()
        ),
        "answerable_by_kb_count": sum(1 for item in records if item["answerable_by_kb"]),
        "should_escalate_count": sum(1 for item in records if item["should_escalate"]),
        "has_pii_count": sum(1 for item in records if item["has_pii"]),
        "segment_counts": {
            "avg": round(
                sum(item["message_segments_count"] for item in records) / len(records), 2
            )
            if records
            else 0,
            "max": max((item["message_segments_count"] for item in records), default=0),
        },
    }


def build_taxonomy(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        key = (
            item["category"],
            item["topic"],
            item["intent"],
            item.get("response_profile") or "generic",
            item["forum_normalized"] or "",
        )
        grouped[key].append(item)

    rows: list[dict[str, Any]] = []
    for (category, topic, intent, response_profile, forum), items in sorted(
        grouped.items(), key=lambda pair: len(pair[1]), reverse=True
    ):
        examples = [
            item["question_candidate"]
            for item in items
            if item.get("question_candidate")
        ][:5]
        escalation_rate = sum(1 for item in items if item["should_escalate"]) / len(items)
        rows.append(
            {
                "category": category,
                "topic": topic,
                "intent": intent,
                "subintent": "",
                "response_profile": response_profile,
                "forum_normalized": forum,
                "frequency": len(items),
                "example_queries": " || ".join(examples),
                "operator_copy_summary": summarize_answers(items),
                "operator_copy_status": "weak_unreviewed",
                "should_escalate_default": escalation_rate >= 0.5,
            }
        )
    return rows


def build_top_questions(records: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        question_key = normalize_question_key(item.get("question_candidate") or "")
        if not question_key:
            continue
        key = (
            question_key,
            item["category"],
            item["topic"],
            item.get("response_profile") or "generic",
            item["forum_normalized"] or "",
        )
        grouped[key].append(item)

    results: list[dict[str, Any]] = []
    for (question_key, category, topic, response_profile, forum), items in sorted(
        grouped.items(), key=lambda pair: len(pair[1]), reverse=True
    )[:limit]:
        best = max(items, key=lambda item: len(item.get("answer_candidate") or ""))
        results.append(
            {
                "normalized_question": question_key,
                "example_query": best.get("question_candidate") or "",
                "frequency": len(items),
                "category": category,
                "topic": topic,
                "response_profile": response_profile,
                "forum_normalized": forum,
                "operator_copy_candidate": best.get("answer_candidate") or "",
                "operator_copy_status": "weak_unreviewed",
                "should_escalate": (
                    sum(1 for item in items if item["should_escalate"]) >= len(items) / 2
                ),
            }
        )
    return results


def build_golden_candidates(
    records: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in records:
        query = item.get("question_candidate") or ""
        answer = item.get("answer_candidate") or ""
        if len(query) < 8:
            continue
        if not item["should_escalate"] and not (item["answerable_by_kb"] and len(answer) >= 40):
            continue
        score = 0
        if item["answerable_by_kb"] and len(answer) >= 40:
            score += 5
        if item["should_escalate"]:
            score += 3
        if item["forum_normalized"]:
            score += 2
        if item["difficulty"] == "complex":
            score += 2
        if item["category"] != "другое":
            score += 1
        if len(query) > 500:
            score -= 2
        scored.append((score, item))

    selected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in balanced_scored_records(scored):
        dedupe_key = normalize_question_key(item["question_candidate"])
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        selected.append(
            {
                "id": f"ticket::{item['ticket_hash']}",
                "query": item["question_candidate"],
                "query_variants": [],
                "expected_answer": "" if item["should_escalate"] else item["answer_candidate"],
                "expected_answer_contains": extract_key_phrases(item["answer_candidate"]),
                "forbidden_answer_contains": ["не найдено в источниках", "я думаю", "скорее всего"],
                "forum_normalized": item["forum_normalized"],
                "category": item["category"],
                "topic": item["topic"],
                "intent": item["intent"],
                "response_profile": item.get("response_profile") or "generic",
                "expected_escalated": item["should_escalate"],
                "expected_escalation_reason": item["escalation_reason"],
                "difficulty": item["difficulty"],
                "source_ticket_ids": [item["ticket_id"]],
                "source_refs": ["RAG_Dataset.xlsx"],
                "notes": item["quality_notes"],
                "label_status": "legacy_weak_operator_copy",
                "operator_answer_used_as_fact": True,
                "deprecated_for_product_eval": True,
                "requires_human_review": True,
            }
        )
        if len(selected) >= max_items:
            break
    return selected


def balanced_scored_records(scored: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    groups: dict[
        tuple[str, str, str, bool],
        list[tuple[int, int, dict[str, Any]]],
    ] = defaultdict(list)
    for index, (score, item) in enumerate(scored):
        groups[golden_group_key(item)].append((score, index, item))

    for rows in groups.values():
        rows.sort(key=lambda pair: (-pair[0], pair[1]))

    ordered_keys = sorted(
        groups,
        key=lambda key: (
            max(score for score, _index, _item in groups[key]),
            key[0],
            key[1],
            key[2],
            key[3],
        ),
        reverse=True,
    )
    ordered: list[dict[str, Any]] = []
    while any(groups.values()):
        for key in ordered_keys:
            if not groups[key]:
                continue
            _score, _index, item = groups[key].pop(0)
            ordered.append(item)
    return ordered


def golden_group_key(item: dict[str, Any]) -> tuple[str, str, str, bool]:
    return (
        str(item.get("category") or "unknown"),
        str(item.get("response_profile") or "generic"),
        str(item.get("difficulty") or "unknown"),
        bool(item.get("should_escalate")),
    )


def build_reranker_pairs(
    golden: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    max_pairs: int,
) -> list[dict[str, Any]]:
    answer_pool = [
        item
        for item in records
        if item.get("answer_candidate") and len(item["answer_candidate"]) >= 40
    ]
    pairs: list[dict[str, Any]] = []
    for case in golden:
        if case["expected_escalated"] or not case["expected_answer"]:
            continue
        negatives = []
        for item in answer_pool:
            if item["ticket_id"] in case["source_ticket_ids"]:
                continue
            same_topic = item["topic"] == case["topic"]
            other_forum = (item["forum_normalized"] or "") != (case["forum_normalized"] or "")
            same_category = item["category"] == case["category"]
            if (same_topic and other_forum) or same_category:
                negatives.append(item["answer_candidate"])
            if len(negatives) >= 3:
                break
        if negatives:
            pairs.append(
                {
                    "query": case["query"],
                    "positive_text": case["expected_answer"],
                    "hard_negative_texts": negatives,
                    "forum_normalized": case["forum_normalized"],
                    "category": case["category"],
                    "topic": case["topic"],
                    "response_profile": case.get("response_profile") or "generic",
                    "relevance_positive": 3,
                    "relevance_negatives": [1 for _ in negatives],
                    "source_ticket_ids": case["source_ticket_ids"],
                    "label_status": "legacy_weak_operator_copy",
                    "operator_answer_used_as_fact": True,
                    "deprecated_for_product_eval": True,
                    "requires_human_review": True,
                }
            )
        if len(pairs) >= max_pairs:
            break
    return pairs


def build_product_split_plan(
    records: list[dict[str, Any]],
    forums: list[ForumAlias],
) -> ProductSplitPlan:
    """Assign every ticket representation through one duplicate-component graph."""

    candidates: list[dict[str, Any]] = []
    for item in records:
        query_eligible = bool(
            item.get("question_candidate")
            and item.get("response_profile") not in {None, "", "unresolved"}
        )
        high_confidence_user_turns = [
            turn
            for turn in item.get("dialogue_turns") or []
            if (
                turn.get("role") == "user"
                and turn.get("role_confidence") == "high"
                and str(turn.get("text_masked") or "").strip()
            )
        ]
        if not query_eligible and not high_confidence_user_turns:
            continue

        families: set[str] = set()
        if query_eligible:
            query_family = _ticket_query_family(item)
            if query_family:
                families.add(query_family)
        for turn in high_confidence_user_turns:
            query = str(turn.get("text_masked") or "")
            family = _conversation_turn_family(
                {
                    "query": query,
                    "entity": detect_forum(query, forums),
                }
            )
            if family:
                families.add(family)
        candidates.append(
            {
                "record": item,
                "ticket_hash": str(item.get("ticket_hash") or ""),
                "families": families,
            }
        )

    dated_keys = sorted(
        key
        for candidate in candidates
        if (
            key := _product_available_at_key(candidate["record"])
        ) is not None
    )
    validation_cutoff = _quantile_key(dated_keys, 0.70)
    holdout_cutoff = _quantile_key(dated_keys, 0.85)
    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    family_owner: dict[str, int] = {}
    ticket_owner: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        ticket_hash = candidate["ticket_hash"]
        if ticket_hash:
            owner = ticket_owner.setdefault(ticket_hash, index)
            union(index, owner)
        for family in sorted(candidate["families"]):
            owner = family_owner.setdefault(family, index)
            union(index, owner)

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(candidates)):
        components[find(index)].append(index)

    assignments: dict[str, ProductSplitAssignment] = {}
    for indexes in components.values():
        component = [candidates[index] for index in indexes]
        item_dates = [
            key
            for candidate in component
            if (
                key := _product_available_at_key(candidate["record"])
            ) is not None
        ]
        split, crosses_boundary = _assign_product_split(
            item_dates,
            validation_cutoff=validation_cutoff,
            holdout_cutoff=holdout_cutoff,
        )
        forced_by_role_review = any(
            str(
                candidate["record"].get("role_reconstruction_status")
                or "not_available"
            )
            != "complete"
            for candidate in component
        )
        if forced_by_role_review:
            split = "calibration"

        component_families = sorted(
            {
                family
                for candidate in component
                for family in candidate["families"]
            }
        )
        component_ticket_hashes = sorted(
            candidate["ticket_hash"]
            for candidate in component
            if candidate["ticket_hash"]
        )
        component_id = sha1_short(
            "\n".join(component_families)
            or "\n".join(component_ticket_hashes)
        )
        assignment = ProductSplitAssignment(
            split=split,
            duplicate_component_id=component_id,
            crosses_boundary=crosses_boundary,
            forced_by_role_review=forced_by_role_review,
        )
        for ticket_hash in component_ticket_hashes:
            assignments[ticket_hash] = assignment

    return ProductSplitPlan(
        assignments=assignments,
        validation_cutoff=validation_cutoff,
        holdout_cutoff=holdout_cutoff,
    )


def build_product_eval_splits(
    records: list[dict[str, Any]],
    forums: list[ForumAlias] | None = None,
    *,
    split_plan: ProductSplitPlan | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Build private query review queues from the shared ticket split plan."""

    candidates = [
        item
        for item in records
        if item.get("question_candidate")
        and item.get("response_profile") not in {None, "", "unresolved"}
    ]
    plan = split_plan or build_product_split_plan(records, forums or [])
    splits: dict[str, list[dict[str, Any]]] = {
        "calibration": [],
        "validation": [],
        "holdout": [],
    }
    for item in candidates:
        assignment = plan.assignments[str(item.get("ticket_hash") or "")]
        splits[assignment.split].append(
            _build_product_eval_case(
                item,
                split=assignment.split,
                duplicate_cluster_id=assignment.duplicate_component_id,
            )
        )

    for cases in splits.values():
        cases.sort(key=lambda item: (item["available_at"], item["id"]))

    candidate_component_ids = {
        plan.assignments[str(item.get("ticket_hash") or "")].duplicate_component_id
        for item in candidates
    }
    crossing_component_ids = {
        assignment.duplicate_component_id
        for assignment in (
            plan.assignments[str(item.get("ticket_hash") or "")]
            for item in candidates
        )
        if assignment.crosses_boundary
    }
    role_review_component_ids = {
        assignment.duplicate_component_id
        for assignment in (
            plan.assignments[str(item.get("ticket_hash") or "")]
            for item in candidates
        )
        if assignment.forced_by_role_review
    }
    summary = {
        "schema_version": "1.0.0",
        "total": sum(len(cases) for cases in splits.values()),
        "split_counts": {
            split: len(cases)
            for split, cases in splits.items()
        },
        "unique_duplicate_clusters": len(candidate_component_ids),
        "crossing_families_forced_to_calibration": len(crossing_component_ids),
        "role_review_components_forced_to_calibration": len(
            role_review_component_ids
        ),
        "validation_cutoff": _format_sort_key(plan.validation_cutoff),
        "holdout_cutoff": _format_sort_key(plan.holdout_cutoff),
        "input_tickets_total": len(records),
        "excluded_without_query_or_profile": len(records) - len(candidates),
        "candidate_coverage_ratio": (
            round(len(candidates) / len(records), 4)
            if records
            else 0.0
        ),
        "unit": "merged_ticket_query_candidate",
        "split_timestamp": "latest_of_created_updated_closed",
        "label_status": "weak_unreviewed",
        "deidentification_status": "best_effort_private_only",
        "operator_answers_used_as_facts": False,
        "factual_ground_truth_present": False,
        "sealed_holdout_ready": False,
        "limitations": [
            "Every case requires human review before it can become gold.",
            "Operator replies are excluded from cases and are not factual ground truth.",
            (
                "Cases are query-only: dialogue roles and full multi-turn context "
                "are not reconstructed."
            ),
            (
                "Best-effort masking is not anonymization; every artifact must remain "
                "under data/private."
            ),
            "Time-sensitive facts require an approved as-of release snapshot.",
            (
                "Lexical template families crossing time boundaries stay in calibration; "
                "semantic paraphrase leakage still requires human review."
            ),
        ],
    }
    return splits, summary


def build_product_conversation_splits(
    records: list[dict[str, Any]],
    forums: list[ForumAlias],
    *,
    split_plan: ProductSplitPlan | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Build private conversations from the shared ticket split plan."""

    candidates: list[dict[str, Any]] = []
    for item in records:
        turns = [
            _build_product_conversation_turn(item, turn, forums)
            for turn in item.get("dialogue_turns") or []
            if (
                turn.get("role") == "user"
                and turn.get("role_confidence") == "high"
                and str(turn.get("text_masked") or "").strip()
            )
        ]
        if not turns:
            continue
        candidates.append(
            {
                "record": item,
                "turns": turns,
            }
        )

    plan = split_plan or build_product_split_plan(records, forums)
    splits: dict[str, list[dict[str, Any]]] = {
        "calibration": [],
        "validation": [],
        "holdout": [],
    }
    for candidate in candidates:
        item = candidate["record"]
        assignment = plan.assignments[str(item.get("ticket_hash") or "")]
        splits[assignment.split].append(
            _build_product_conversation_case(
                item,
                candidate["turns"],
                split=assignment.split,
                duplicate_component_id=assignment.duplicate_component_id,
            )
        )

    for conversations in splits.values():
        conversations.sort(key=lambda item: (item["available_at"], item["id"]))

    total_turns = sum(
        len(conversation["turns"])
        for conversations in splits.values()
        for conversation in conversations
    )
    multi_turn_conversations = sum(
        len(conversation["turns"]) >= 2
        for conversations in splits.values()
        for conversation in conversations
    )
    candidate_assignments = [
        plan.assignments[str(candidate["record"].get("ticket_hash") or "")]
        for candidate in candidates
    ]
    candidate_component_ids = {
        assignment.duplicate_component_id
        for assignment in candidate_assignments
    }
    crossing_component_ids = {
        assignment.duplicate_component_id
        for assignment in candidate_assignments
        if assignment.crosses_boundary
    }
    role_review_component_ids = {
        assignment.duplicate_component_id
        for assignment in candidate_assignments
        if assignment.forced_by_role_review
    }
    summary = {
        "schema_version": "2.0.0",
        "total": sum(len(items) for items in splits.values()),
        "turns_total": total_turns,
        "multi_turn_conversations": multi_turn_conversations,
        "split_counts": {
            split: len(items)
            for split, items in splits.items()
        },
        "unique_duplicate_components": len(candidate_component_ids),
        "crossing_components_forced_to_calibration": len(
            crossing_component_ids
        ),
        "role_review_components_forced_to_calibration": len(
            role_review_component_ids
        ),
        "validation_cutoff": _format_sort_key(plan.validation_cutoff),
        "holdout_cutoff": _format_sort_key(plan.holdout_cutoff),
        "input_tickets_total": len(records),
        "excluded_without_high_confidence_user_turn": len(records) - len(candidates),
        "candidate_coverage_ratio": (
            round(len(candidates) / len(records), 4)
            if records
            else 0.0
        ),
        "unit": "ticket_conversation_candidate",
        "label_status": "weak_unreviewed",
        "role_policy": "high_confidence_user_turns_only",
        "operator_answers_used_as_facts": False,
        "historical_assistant_turns_in_eval_payload": False,
        "factual_ground_truth_present": False,
        "sealed_holdout_ready": False,
        "limitations": [
            "The source XLSX has no speaker column; roles are reconstructed heuristically.",
            "Medium-confidence and unknown turns require private human role review.",
            "Historical assistant/operator copy is excluded from evaluation payloads.",
            "Every conversation and turn requires human review before becoming gold.",
            "Partial role reconstructions are forced to calibration.",
            "Best-effort masking is not anonymization; artifacts remain under data/private.",
        ],
    }
    return splits, summary


def build_product_role_review_queue(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for item in records:
        title_review_candidate = str(
            item.get("title_role_review_candidate") or ""
        ).strip()
        if title_review_candidate:
            queue.append(
                {
                    "schema_version": "1.0.0",
                    "ticket_id_hash": str(item.get("ticket_hash") or ""),
                    "turn_index": -1,
                    "role_candidate": "unknown",
                    "role_confidence": "low",
                    "role_reason": "unverified_ticket_title",
                    "text_masked": title_review_candidate,
                    "question_score": score_question_segment(
                        title_review_candidate
                    ),
                    "answer_score": score_answer_segment(
                        title_review_candidate
                    ),
                    "has_pii": bool(
                        item.get("title_role_review_has_pii")
                    ),
                    "pii_types": list(
                        item.get("title_role_review_pii_types") or []
                    ),
                    "reviewer": "",
                    "role_verdict": "",
                    "corrected_role": "",
                    "include_as_user_turn": None,
                    "operator_answer_used_as_fact": False,
                    "requires_human_review": True,
                }
            )
        for turn in item.get("dialogue_turns") or []:
            if (
                turn.get("role_confidence") == "high"
                and turn.get("role") != "unknown"
            ):
                continue
            queue.append(
                {
                    "schema_version": "1.0.0",
                    "ticket_id_hash": str(item.get("ticket_hash") or ""),
                    "turn_index": int(turn.get("turn_index") or 0),
                    "role_candidate": str(turn.get("role") or "unknown"),
                    "role_confidence": str(turn.get("role_confidence") or "low"),
                    "role_reason": str(turn.get("role_reason") or ""),
                    "text_masked": str(turn.get("text_masked") or ""),
                    "question_score": int(turn.get("question_score") or 0),
                    "answer_score": int(turn.get("answer_score") or 0),
                    "has_pii": bool(turn.get("has_pii")),
                    "pii_types": list(turn.get("pii_types") or []),
                    "reviewer": "",
                    "role_verdict": "",
                    "corrected_role": "",
                    "include_as_user_turn": None,
                    "operator_answer_used_as_fact": False,
                    "requires_human_review": True,
                }
            )
    queue.sort(key=lambda item: (item["ticket_id_hash"], item["turn_index"]))
    return queue


def _build_product_conversation_turn(
    item: dict[str, Any],
    turn: dict[str, Any],
    forums: list[ForumAlias],
) -> dict[str, Any]:
    query = str(turn.get("text_masked") or "")
    category = classify_category(query)
    topic = classify_topic(query)
    entity = detect_forum(query, forums)
    profile = infer_response_profile(
        QueryAnalysis(
            category=category,
            is_technical=category == "техподдержка",
        ),
        query,
    ).value
    escalation_reason = classify_escalation(query, {})
    should_escalate = escalation_reason is not None
    should_clarify = needs_clarification(query)
    behavior = (
        "escalate"
        if should_escalate
        else "clarify"
        if should_clarify
        else "answer"
    )
    source_turn_index = int(turn.get("turn_index") or 0)
    ticket_hash = str(item.get("ticket_hash") or "")
    return {
        "id": f"ticket::{ticket_hash}::t{source_turn_index + 1:03d}",
        "source_turn_index": source_turn_index,
        "query": query,
        "channel": "api",
        "category": category,
        "topic": topic,
        "entity": entity,
        "predicted_behavior": behavior,
        "predicted_response_profile": profile,
        "predicted_escalated": should_escalate,
        "predicted_escalation_reason": escalation_reason,
        "answerable_from_snapshot": None,
        "approved_chunk_ids": [],
        "forbidden_response_profiles": list(
            _default_forbidden_response_profiles(profile)
        ),
        "role_confidence": "high",
        "label_status": "weak_unreviewed",
        "requires_human_review": True,
        "operator_answer_included": False,
        "operator_answer_used_as_fact": False,
        "tags": [
            "private_ticket_conversation",
            f"profile:{profile}",
            f"behavior:{behavior}",
        ],
    }


def _build_product_conversation_case(
    item: dict[str, Any],
    turns: list[dict[str, Any]],
    *,
    split: str,
    duplicate_component_id: str,
) -> dict[str, Any]:
    available_at = _format_sort_key(_product_available_at_key(item)) or ""
    return {
        "schema_version": "2.0.0",
        "id": f"ticket::{item['ticket_hash']}",
        "ticket_id_hash": item["ticket_hash"],
        "first_timestamp": str(item.get("created_at") or ""),
        "available_at": available_at,
        "source_channel": str(item.get("channel") or ""),
        "role_reconstruction_status": str(
            item.get("role_reconstruction_status") or "unresolved"
        ),
        "turns": turns,
        "turns_count": len(turns),
        "duplicate_component_id": duplicate_component_id,
        "split": split,
        "label_status": "weak_unreviewed",
        "requires_human_review": True,
        "operator_answer_included": False,
        "operator_answer_used_as_fact": False,
        "sealed_holdout_eligible": False,
    }


def _conversation_turn_family(turn: dict[str, Any]) -> str:
    query = normalize_question_key(str(turn.get("query") or ""))
    entity = normalize_for_match(str(turn.get("entity") or ""))
    if entity:
        query = query.replace(entity, "[event]")
    query = re.sub(r"\b\d+\b", "[num]", query)
    query = re.sub(r"(?:\[num\]\s*){2,}", "[num] ", query)
    return WHITESPACE_RE.sub(" ", query).strip()


def _build_product_eval_case(
    item: dict[str, Any],
    *,
    split: str,
    duplicate_cluster_id: str,
) -> dict[str, Any]:
    query = str(item.get("question_candidate") or "")
    query_category = str(item.get("query_category") or classify_category(query))
    query_topic = str(item.get("query_topic") or classify_topic(query))
    profile = infer_response_profile(
        QueryAnalysis(
            category=query_category,
            is_technical=query_category == "техподдержка",
        ),
        query,
    ).value
    query_escalation_reason = (
        item.get("query_escalation_reason")
        if "query_escalation_reason" in item
        else classify_escalation(query, {})
    )
    query_should_escalate = bool(
        item.get("query_should_escalate")
        if "query_should_escalate" in item
        else query_escalation_reason
    )
    query_needs_clarification = bool(
        item.get("query_needs_clarification")
        if "query_needs_clarification" in item
        else needs_clarification(query)
    )
    available_at = _format_sort_key(_product_available_at_key(item)) or ""
    return {
        "schema_version": "1.0.0",
        "id": f"ticket::{item['ticket_hash']}",
        "ticket_id_hash": item["ticket_hash"],
        "query": query,
        "first_timestamp": str(item.get("created_at") or ""),
        "available_at": available_at,
        "channel": str(item.get("channel") or ""),
        "category": query_category,
        "topic": query_topic,
        "entity": str(item.get("query_forum_normalized") or ""),
        "expected_response_profile": profile,
        "expected_route": (
            "escalate"
            if query_should_escalate
            else "clarify"
            if query_needs_clarification
            else "answer"
        ),
        "needs_clarification": query_needs_clarification,
        "needs_escalation": query_should_escalate,
        "expected_escalation_reason": query_escalation_reason,
        "time_sensitive": _is_time_sensitive_case(profile, query),
        "difficulty": str(item.get("difficulty") or "medium"),
        "role_reconstruction_status": str(
            item.get("role_reconstruction_status") or "not_available"
        ),
        "multiturn_status": (
            "multi_turn"
            if item.get("multi_turn_user_candidate")
            else "single_turn"
            if int(item.get("high_confidence_user_turns_count") or 0) == 1
            else "not_available"
        ),
        "message_segments_count": int(item.get("message_segments_count") or 0),
        "high_confidence_user_turns_count": int(
            item.get("high_confidence_user_turns_count") or 0
        ),
        "answerable_from_snapshot": None,
        "approved_chunk_ids": [],
        "forbidden_response_profiles": list(
            _default_forbidden_response_profiles(profile)
        ),
        "duplicate_cluster_id": duplicate_cluster_id,
        "duplicate_component_id": duplicate_cluster_id,
        "split": split,
        "label_status": "weak_unreviewed",
        "label_provenance": "deterministic_query_only_v2",
        "requires_human_review": True,
        "operator_answer_included": False,
        "operator_answer_used_as_fact": False,
    }


def _ticket_query_family(item: dict[str, Any]) -> str:
    query = normalize_question_key(str(item.get("question_candidate") or ""))
    forum = normalize_for_match(str(item.get("query_forum_normalized") or ""))
    if forum:
        query = query.replace(forum, "[event]")
    query = re.sub(r"\b\d+\b", "[num]", query)
    query = re.sub(r"(?:\[num\]\s*){2,}", "[num] ", query)
    return WHITESPACE_RE.sub(" ", query).strip()


def _product_available_at_key(
    item: dict[str, Any],
) -> tuple[int, int, int, int, int, int] | None:
    timestamps = [
        key
        for field in ("created_at", "updated_at", "closed_at")
        if (key := _created_at_sort_key(str(item.get(field) or ""))) is not None
    ]
    return max(timestamps, default=None)


def _created_at_sort_key(value: str) -> tuple[int, int, int, int, int, int] | None:
    match = re.search(
        r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"
        r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        value,
    )
    if not match:
        return None
    parts = [int(part or 0) for part in match.groups()]
    year, month, day, hour, minute, second = parts
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day, hour, minute, second


def _quantile_key(
    values: list[tuple[int, int, int, int, int, int]],
    quantile: float,
) -> tuple[int, int, int, int, int, int] | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, int(len(values) * quantile)))
    return values[index]


def _assign_product_split(
    item_dates: list[tuple[int, int, int, int, int, int]],
    *,
    validation_cutoff: tuple[int, int, int, int, int, int] | None,
    holdout_cutoff: tuple[int, int, int, int, int, int] | None,
) -> tuple[str, bool]:
    if not item_dates or validation_cutoff is None or holdout_cutoff is None:
        return "calibration", False
    earliest = min(item_dates)
    latest = max(item_dates)
    if earliest >= holdout_cutoff:
        return "holdout", False
    if earliest >= validation_cutoff and latest < holdout_cutoff:
        return "validation", False
    crosses_boundary = (
        earliest < validation_cutoff <= latest
        or earliest < holdout_cutoff <= latest
    )
    return "calibration", crosses_boundary


def _format_sort_key(
    value: tuple[int, int, int, int, int, int] | None,
) -> str | None:
    if value is None:
        return None
    year, month, day, hour, minute, second = value
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"


def _is_time_sensitive_case(profile: str, query: str) -> bool:
    if profile in {
        ResponseProfileName.DATES.value,
        ResponseProfileName.SELECTION_STATUS.value,
    }:
        return True
    normalized = normalize_for_match(query)
    return any(
        marker in normalized
        for marker in (
            "срок",
            "дедлайн",
            "прием заяв",
            "приём заяв",
            "регистрац",
            "статус",
            "результат",
        )
    )


def _default_forbidden_response_profiles(profile: str) -> tuple[str, ...]:
    return CRITICAL_FORBIDDEN_RESPONSE_PROFILES.get(profile, ())


def build_gap_report(
    profile: dict[str, Any],
    taxonomy_rows: list[dict[str, Any]],
    top_questions: list[dict[str, Any]],
) -> str:
    lines = [
        "# KB Gap Report",
        "",
        "## Executive Summary",
        "",
        f"- Tickets total: `{profile['tickets_total']}`",
        f"- Answerable by KB candidates: `{profile['answerable_by_kb_count']}`",
        f"- Escalation candidates: `{profile['should_escalate_count']}`",
        f"- Tickets with detected PII: `{profile['has_pii_count']}`",
        "",
        "## Category Counts",
        "",
        *_counter_lines(profile["category_counts"]),
        "",
        "## Requested Response Profiles",
        "",
        *_counter_lines(profile["response_profile_counts"]),
        "",
        "## Top Forums",
        "",
        *_counter_lines(profile["forum_counts"], limit=30),
        "",
        "## Escalation Reasons",
        "",
        *_counter_lines(profile["escalation_reason_counts"]),
        "",
        "## High-Frequency Intent Groups",
        "",
    ]
    for row in taxonomy_rows[:30]:
        lines.append(
            f"- `{row['frequency']}` {row['category']} / {row['topic']} / "
            f"{row['response_profile']} / {row['forum_normalized'] or 'unknown'}"
        )

    lines.extend(
        [
            "",
            "## Top Questions For Review",
            "",
        ]
    )
    for item in top_questions[:30]:
        lines.append(
            f"- `{item['frequency']}` {item['category']} / {item['topic']} / "
            f"{item['response_profile']} / {item['forum_normalized'] or 'unknown'}: "
            f"{item['example_query'][:180]}"
        )

    lines.extend(
        [
            "",
            "## Recommendations",
            "",
            "- Сначала вручную проверить top intent groups и подтвердить реальные "
            "`expected_answer`.",
            "- Для форумов с похожими вопросами добавить `forum_normalized` и aliases "
            "в KB metadata.",
            "- Для `personal_status` и открытых статусов оставить controlled escalation.",
            (
                "- Legacy operator-copy reranker pairs не использовать для threshold sweep; "
                "сначала сопоставить запросы с approved KB chunks."
            ),
            "- Сырые и нормализованные приватные данные не коммитить.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_summary(
    profile: dict[str, Any],
    golden: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    output_dir: Path,
) -> str:
    golden_difficulty = Counter(item.get("difficulty") or "unknown" for item in golden)
    golden_category = Counter(item.get("category") or "unknown" for item in golden)
    golden_profile = Counter(
        item.get("response_profile") or "generic"
        for item in golden
    )
    golden_escalation = Counter(
        "escalated" if item.get("expected_escalated") else "answerable" for item in golden
    )
    return "\n".join(
        [
            "# Ticket Dataset Analysis Summary",
            "",
            f"- Tickets total: `{profile['tickets_total']}`",
            f"- Golden candidates: `{len(golden)}`",
            f"- Reranker pairs: `{len(pairs)}`",
            f"- Output dir: `{output_dir}`",
            f"- Answerable by KB candidates: `{profile['answerable_by_kb_count']}`",
            f"- Escalation candidates: `{profile['should_escalate_count']}`",
            f"- Tickets with detected PII: `{profile['has_pii_count']}`",
            "",
            "> `golden_set_candidates` and reranker pairs contain legacy, weak "
            "operator copy. They are deprecated for product evaluation and must not "
            "be used as factual ground truth.",
            "",
            "## Golden Candidate Mix",
            "",
            *_counter_lines(dict(golden_difficulty.most_common())),
            "",
            "## Golden Candidate Categories",
            "",
            *_counter_lines(dict(golden_category.most_common())),
            "",
            "## Golden Candidate Response Profiles",
            "",
            *_counter_lines(dict(golden_profile.most_common())),
            "",
            "## Golden Candidate Expected Routing",
            "",
            *_counter_lines(dict(golden_escalation.most_common())),
            "",
            "## Generated Files",
            "",
            "- `dataset_profile.json`",
            "- `tickets_normalized.jsonl`",
            "- `intent_taxonomy.csv`",
            "- `top_questions.md`",
            "- `golden_set_candidates.json`",
            "- `reranker_calibration_pairs.jsonl`",
            "- `product_calibration_cases.jsonl`",
            "- `product_validation_cases.jsonl`",
            "- `product_holdout_cases.jsonl`",
            "- `product_split_summary.json`",
            "- `product_calibration_conversations.json`",
            "- `product_validation_conversations.json`",
            "- `product_holdout_conversations.json`",
            "- `product_conversation_split_summary.json`",
            "- `product_role_review_queue.jsonl`",
            "- `kb_gap_report.md`",
            "- `artifact_manifest.json` (written last; validates the complete generated set)",
            "",
        ]
    )


def top_questions_to_markdown(items: list[dict[str, Any]]) -> str:
    lines = [
        "# Top Questions",
        "",
        "| Rank | Frequency | Category | Topic | Profile | Forum | Escalate | Example Query |",
        "|---:|---:|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(
            "| "
            f"{index} | {item['frequency']} | {item['category']} | {item['topic']} | "
            f"{item['response_profile']} | "
            f"{item['forum_normalized'] or 'unknown'} | {item['should_escalate']} | "
            f"{markdown_cell(item['example_query'][:220])} |"
        )
    return "\n".join(lines) + "\n"


def load_forum_aliases(path: Path) -> list[ForumAlias]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    forums = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized = str(item.get("normalized") or item.get("name") or "").strip()
        aliases = [str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip()]
        if normalized:
            aliases.append(normalized)
            forums.append(ForumAlias(normalized=normalized, aliases=tuple(sorted(set(aliases)))))
    return forums


def mask_pii(text: str) -> tuple[str, list[str]]:
    masked = text or ""
    pii_types: list[str] = []
    seen_pii_types: set[str] = set()
    patterns = (
        ("email", EMAIL_RE, "[EMAIL]"),
        ("url", URL_RE, "[URL]"),
        ("phone", PHONE_RE, "[ТЕЛЕФОН]"),
        ("snils", SNILS_RE, "[СНИЛС]"),
        ("passport", PASSPORT_RE, "[ДОКУМЕНТ]"),
        ("vk_id", VK_ID_RE, "[VK_ID]"),
        ("handle", HANDLE_RE, "[HANDLE]"),
        ("fio_context", FIO_CONTEXT_RE, "[ФИО]"),
        ("long_id", LONG_ID_RE, "[ID]"),
        ("date", DATE_RE, "[ДАТА]"),
    )
    # Replacements can expose an adjacent identifier that was initially hidden
    # by a regex boundary, for example two phone numbers without a separator.
    while True:
        changed = False
        for pii_type, regex, placeholder in patterns:
            masked_after, count = regex.subn(placeholder, masked)
            if count:
                if pii_type not in seen_pii_types:
                    pii_types.append(pii_type)
                    seen_pii_types.add(pii_type)
                changed = changed or masked_after != masked
                masked = masked_after
        if not changed:
            break
    return compact_text(masked), pii_types


def compact_text(text: str) -> str:
    text = str(text or "").replace("\xa0", " ")
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_for_match(text: str) -> str:
    return compact_text(text).casefold().replace("ё", "е")


def normalize_question_key(text: str) -> str:
    normalized = normalize_for_match(text)
    normalized = URL_RE.sub("[url]", normalized)
    normalized = EMAIL_RE.sub("[email]", normalized)
    normalized = PHONE_RE.sub("[phone]", normalized)
    normalized = re.sub(r"[^\wа-яА-ЯёЁ\s\[\]]+", " ", normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized[:240]


def build_intent(category: str, topic: str) -> str:
    return f"{category}.{topic}"


def summarize_answers(items: list[dict[str, Any]]) -> str:
    answers = [item["answer_candidate"] for item in items if item.get("answer_candidate")]
    if not answers:
        return ""
    best = max(answers, key=len)
    return best[:300]


def extract_key_phrases(text: str, limit: int = 3) -> list[str]:
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    phrases = [sentence.strip() for sentence in sentences if 20 <= len(sentence.strip()) <= 180]
    return phrases[:limit]


def build_quality_notes(row: dict[str, str], question: str, answer: str) -> str:
    notes = []
    if not question:
        notes.append("missing_question_candidate")
    if not answer:
        notes.append("missing_answer_candidate")
    if (row.get("typical_atypical") or "").casefold() == "нетиповой":
        notes.append("marked_atypical")
    return ", ".join(notes)


def sha1_short(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def private_id_hash(value: str) -> str:
    namespaced = f"rosmol-private-ticket-v1\0{value}"
    return hashlib.sha256(namespaced.encode("utf-8", errors="ignore")).hexdigest()[:24]


def markdown_cell(value: str) -> str:
    return compact_text(value).replace("|", "\\|").replace("\n", " ")


def _counter_lines(counter: dict[str, int], limit: int | None = None) -> list[str]:
    items = list(counter.items())
    if limit is not None:
        items = items[:limit]
    return [f"- `{count}` {name}" for name, count in items]


def write_json(path: Path, payload: Any) -> None:
    def write(file: TextIO) -> None:
        file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    _atomic_write_text(path, write)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    def write(file: TextIO) -> None:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    _atomic_write_text(path, write)


def write_json_array(path: Path, records: list[dict[str, Any]]) -> None:
    def write(file: TextIO) -> None:
        file.write("[\n")
        for index, record in enumerate(records):
            if index:
                file.write(",\n")
            serialized = json.dumps(record, ensure_ascii=False, indent=2)
            file.write("\n".join(f"  {line}" for line in serialized.splitlines()))
        file.write("\n]\n")

    _atomic_write_text(path, write)


def write_markdown(path: Path, content: str) -> None:
    _atomic_write_text(path, lambda file: file.write(content))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        _atomic_write_text(path, lambda file: file.write(""))
        return

    def write(file: TextIO) -> None:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    _atomic_write_text(path, write, encoding="utf-8-sig", newline="")


def _atomic_write_text(
    path: Path,
    writer: Callable[[TextIO], Any],
    *,
    encoding: str = "utf-8",
    newline: str | None = "\n",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding=encoding, newline=newline) as file:
            writer(file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_artifact_staging_dir(output_dir: Path) -> Path:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.staging-",
            dir=output_dir.parent,
        )
    )


def build_artifact_manifest(
    staging_dir: Path,
    *,
    input_path: Path,
    forums_path: Path,
) -> dict[str, Any]:
    artifacts = []
    for path in sorted(
        item
        for item in staging_dir.rglob("*")
        if item.is_file() and item.name != ARTIFACT_MANIFEST_NAME
    ):
        relative_path = path.relative_to(staging_dir).as_posix()
        artifacts.append(
            {
                "path": relative_path,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if not artifacts:
        raise ValueError("Artifact staging directory is empty")
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "complete": True,
        "source": {
            "input_path": str(input_path.resolve()),
            "input_sha256": file_sha256(input_path),
            "forums_path": str(forums_path.resolve()),
            "forums_sha256": file_sha256(forums_path),
        },
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


def promote_staged_artifacts(staging_dir: Path, output_dir: Path) -> None:
    manifest_source = staging_dir / ARTIFACT_MANIFEST_NAME
    if not manifest_source.is_file():
        raise ValueError("Staged artifacts have no completion manifest")
    output_dir.mkdir(parents=True, exist_ok=True)
    staged_files = sorted(
        path
        for path in staging_dir.rglob("*")
        if path.is_file() and path != manifest_source
    )
    for source in staged_files:
        target = output_dir / source.relative_to(staging_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    os.replace(manifest_source, output_dir / ARTIFACT_MANIFEST_NAME)


def validate_artifact_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / ARTIFACT_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Artifact completion manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("complete") is not True:
        raise ValueError("Artifact completion manifest is not complete")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Artifact completion manifest has no artifacts")
    if manifest.get("artifact_count") != len(artifacts):
        raise ValueError("Artifact completion manifest count mismatch")

    seen_paths: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("Artifact completion manifest item is invalid")
        relative_text = str(item.get("path") or "")
        relative = Path(relative_text)
        if (
            not relative_text
            or relative.is_absolute()
            or ".." in relative.parts
            or relative_text in seen_paths
        ):
            raise ValueError("Artifact completion manifest path is unsafe")
        seen_paths.add(relative_text)
        artifact_path = (output_dir / relative).resolve()
        if not artifact_path.is_relative_to(output_dir.resolve()):
            raise ValueError("Artifact completion manifest path escapes output directory")
        if not artifact_path.is_file():
            raise ValueError(f"Artifact is missing: {relative_text}")
        if artifact_path.stat().st_size != item.get("size_bytes"):
            raise ValueError(f"Artifact size mismatch: {relative_text}")
        if file_sha256(artifact_path) != item.get("sha256"):
            raise ValueError(f"Artifact hash mismatch: {relative_text}")
    return manifest


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze private support tickets for RAG quality.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--forums", type=Path, default=DEFAULT_FORUMS)
    parser.add_argument("--max-golden", type=int, default=800)
    parser.add_argument("--max-pairs", type=int, default=1000)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    summary = analyze_dataset(
        input_path=args.input,
        output_dir=args.out_dir,
        forums_path=args.forums,
        max_golden=args.max_golden,
        max_pairs=args.max_pairs,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
