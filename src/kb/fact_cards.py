from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.kb.aspect_catalog import topic_candidates_for_aspect
from src.kb.fact_extractor import (
    KnowledgeAspect,
    extract_source_fact_excerpts,
    infer_source_aspects,
    plan_query_aspects,
    semantic_fact_tokens,
    source_answer_signal_score,
    source_fact_units,
    source_scope_constraints_match,
)
from src.kb.forum_registry import forums_are_equivalent
from src.models import ScoredChunk

_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\u2600-\u27bf"
    "\ufe0f"
    "\u200d"
    "]+"
)
_MONTH_PATTERN = (
    r"январ[ья]|феврал[ья]|март[а]?|апрел[ья]|ма[йя]|июн[ья]|"
    r"июл[ья]|август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья]"
)
_WORD_RANGE_RE = re.compile(
    rf"(?:с\s+)?(?P<start>\d{{1,2}})\s+(?P<start_month>{_MONTH_PATTERN})"
    rf"(?:\s+(?P<start_year>20\d{{2}}))?\s+(?:года?\s+)?по\s+"
    rf"(?P<end>\d{{1,2}})\s+(?P<end_month>{_MONTH_PATTERN})"
    rf"(?:\s+(?P<end_year>20\d{{2}}))?",
    re.IGNORECASE,
)
_SAME_MONTH_RANGE_RE = re.compile(
    rf"(?P<start>\d{{1,2}})\s*[–—-]\s*(?P<end>\d{{1,2}})\s+"
    rf"(?P<month>{_MONTH_PATTERN})(?:\s+(?P<year>20\d{{2}}))?",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./]"
    r"(?P<year>20\d{2})(?!\d)"
)
_WORD_DATE_RE = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_PATTERN})\s+"
    r"(?P<year>20\d{2})",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
_ORDINAL_WORDS = {
    1: "Первая",
    2: "Вторая",
    3: "Третья",
    4: "Четвёртая",
    5: "Пятая",
    6: "Шестая",
}
_QUERY_STOPWORDS = frozenset(
    {
        "какая",
        "какие",
        "какой",
        "когда",
        "может",
        "можно",
        "нужно",
        "форум",
        "форуме",
        "через",
    }
)


@dataclass(frozen=True)
class FactCard:
    chunk_id: str
    aspects: frozenset[KnowledgeAspect]
    excerpts: tuple[str, ...]


@dataclass(frozen=True)
class FactCardDraft:
    response: str
    cited_sources: tuple[str, ...]
    requested_aspects: frozenset[KnowledgeAspect]
    cards: tuple[FactCard, ...]


@dataclass(frozen=True)
class _FactSlot:
    aspect: KnowledgeAspect
    qualifier: str = ""


def compose_fact_cards(
    query: str,
    chunks: list[ScoredChunk],
    *,
    category: str | None = None,
    forum_normalized: str | None = None,
    response_limit: int = 450,
    max_sources: int = 5,
) -> FactCardDraft | None:
    """Build a complete deterministic answer from published fact cards.

    A draft is returned only when every planned aspect resolves inside the
    current entity/category scope. Missing or ambiguous coverage stays on the
    existing grounded LLM/escalation path.
    """

    requested = plan_query_aspects(query)
    if not requested or response_limit <= 0 or max_sources <= 0:
        return None

    scoped = [
        chunk
        for chunk in chunks
        if _is_published_yonote_chunk(chunk)
        and _chunk_matches_scope(
            chunk,
            query=query,
            category=category,
            forum_normalized=forum_normalized,
        )
        and source_scope_constraints_match(query, chunk.metadata)
    ]
    if not scoped:
        return None

    source_aspects = {
        chunk.chunk_id: infer_source_aspects(chunk.metadata, chunk.text)
        for chunk in scoped
    }
    query_tokens = _tokens(query)
    selected: list[tuple[ScoredChunk, set[KnowledgeAspect]]] = []
    selected_by_id: dict[str, set[KnowledgeAspect]] = {}
    slots = _request_slots(requested, query)

    for slot in slots:
        aspect = slot.aspect
        preferred_topics = topic_candidates_for_aspect(
            aspect,
            query,
            category=category,
            forum_normalized=forum_normalized,
        )
        preferred_rank = {
            topic: ordinal for ordinal, topic in enumerate(preferred_topics)
        }
        candidates = [
            chunk
            for chunk in scoped
            if aspect in source_aspects.get(chunk.chunk_id, frozenset())
            and _slot_matches_chunk(slot, chunk)
        ]
        if not candidates:
            return None
        ranked_candidates = sorted(
            (
                _candidate_rank(
                    chunk,
                    query=query,
                    aspect=aspect,
                    query_tokens=query_tokens,
                    preferred_rank=preferred_rank,
                    requested_coverage=len(
                        source_aspects.get(chunk.chunk_id, frozenset()) & requested
                    ),
                ),
                chunk,
            )
            for chunk in candidates
        )
        best_rank, chosen = ranked_candidates[0]
        if len(ranked_candidates) > 1:
            second_rank, _second = ranked_candidates[1]
            if best_rank[:-1] == second_rank[:-1]:
                return None
        covered = {aspect}
        if chosen.chunk_id in selected_by_id:
            selected_by_id[chosen.chunk_id].update(covered)
        else:
            selected_by_id[chosen.chunk_id] = covered
            selected.append((chosen, selected_by_id[chosen.chunk_id]))
        if len(selected) > max_sources:
            return None

    if not selected:
        return None
    source_order = {chunk.chunk_id: ordinal for ordinal, chunk in enumerate(chunks)}
    selected.sort(key=lambda item: source_order.get(item[0].chunk_id, len(source_order)))

    marker_budget = sum(len(f" [src:{chunk.chunk_id}]") for chunk, _ in selected)
    text_budget = response_limit - marker_budget - max(0, len(selected) - 1) * 2
    if text_budget <= 0:
        return None
    per_source_budget = max(120, text_budget // len(selected))

    cards: list[FactCard] = []
    url_seen = False
    for chunk, covered in selected:
        excerpts = _render_fact_card(
            chunk,
            query=query,
            aspects=frozenset(covered),
            max_chars=per_source_budget,
        )
        if not excerpts:
            return None
        sanitized: list[str] = []
        for excerpt in excerpts:
            text = excerpt
            urls = _URL_RE.findall(text)
            if urls and url_seen:
                text = _URL_RE.sub("", text)
                text = re.sub(r"\s+([.,;:])", r"\1", text).strip()
            if text:
                sanitized.append(text)
            url_seen = url_seen or bool(urls)
        if not sanitized:
            return None
        cards.append(
            FactCard(
                chunk_id=chunk.chunk_id,
                aspects=frozenset(covered),
                excerpts=tuple(sanitized),
            )
        )

    claims = [
        f"{' '.join(card.excerpts)} [src:{card.chunk_id}]"
        for card in cards
    ]
    response = "\n\n".join(claims).strip()
    if not response or len(response) > response_limit + marker_budget:
        return None
    if len(_URL_RE.findall(response)) > 1:
        return None
    return FactCardDraft(
        response=response,
        cited_sources=tuple(card.chunk_id for card in cards),
        requested_aspects=requested,
        cards=tuple(cards),
    )


def _request_slots(
    requested: frozenset[KnowledgeAspect],
    query: str,
) -> tuple[_FactSlot, ...]:
    slots: list[_FactSlot] = []
    aspect_order = {aspect: ordinal for ordinal, aspect in enumerate(KnowledgeAspect)}
    for aspect in sorted(requested, key=aspect_order.__getitem__):
        if aspect != KnowledgeAspect.DATES:
            slots.append(_FactSlot(aspect))
            continue

        shift_ordinals = _query_shift_ordinals(query)
        if len(shift_ordinals) > 1:
            slots.extend(
                _FactSlot(aspect, f"shift_ordinal:{ordinal}")
                for ordinal in shift_ordinals
            )
            continue
        if _asks_overall_and_shift_dates(query):
            slots.extend(
                (
                    _FactSlot(aspect, "overall_event"),
                    _FactSlot(aspect, "specific_shift"),
                )
            )
            continue
        if shift_ordinals:
            slots.append(_FactSlot(aspect, f"shift_ordinal:{shift_ordinals[0]}"))
            continue
        slots.append(
            _FactSlot(
                aspect,
                "specific_shift" if _asks_specific_shift_dates(query) else "",
            )
        )
    return tuple(slots)


def _slot_matches_chunk(slot: _FactSlot, chunk: ScoredChunk) -> bool:
    if not slot.qualifier:
        return True
    metadata = chunk.metadata or {}
    signature = " ".join(
        (
            str(metadata.get("topic") or "").replace("_", " "),
            str(metadata.get("intent_name") or ""),
            _heading_text(metadata),
        )
    ).casefold()
    if slot.qualifier.startswith("shift_ordinal:"):
        ordinal = int(slot.qualifier.rsplit(":", 1)[1])
        return re.search(rf"\b{ordinal}\s*(?:-?я\s+)?смен", signature) is not None
    source_aspects = infer_source_aspects(metadata, chunk.text)
    if slot.qualifier == "overall_event":
        return KnowledgeAspect.OVERVIEW in source_aspects
    if slot.qualifier == "specific_shift":
        return (
            KnowledgeAspect.DATES in source_aspects
            and KnowledgeAspect.OVERVIEW not in source_aspects
            and KnowledgeAspect.REGISTRATION not in source_aspects
        )
    return True


def _render_fact_card(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
    max_chars: int,
) -> list[str]:
    renderers = (
        _render_account_access,
        _render_confirmation,
        _render_grant_application_review,
        _render_statuses,
        _render_named_shifts,
        _render_grant_nominations,
        _render_grant_review,
        _render_eligibility,
        _render_ticket,
        _render_dates,
        _render_volunteering,
        _render_navigation,
        _render_technical,
        _render_registration,
        _render_results_or_program,
        _render_overview,
        _render_stay,
    )
    for renderer in renderers:
        claims = renderer(chunk, query=query, aspects=aspects)
        fitted = _fit_claims(claims, max_chars=max_chars)
        if fitted:
            return fitted

    excerpts = extract_source_fact_excerpts(
        chunk.text,
        query,
        chunk.metadata,
        max_chars=max_chars,
        max_units=5,
        requested_aspects=aspects,
    )
    return _fit_claims(excerpts, max_chars=max_chars)


def _render_account_access(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.ACCOUNT_ACCESS not in aspects:
        return []
    normalized = _normalized(chunk.text)
    email_match = _EMAIL_RE.search(chunk.text)
    if not (
        email_match
        and "потерял доступ" in normalized
        and "перенесут" in normalized
        and "аккаунт" in normalized
    ):
        return []
    email = email_match.group(0).rstrip(".,;:")
    return [
        "Нужно создать новый аккаунт.",
        (
            "Чтобы создать аккаунт, нужно войти через Госуслуги и прислать ID "
            f"этого аккаунта по адресу {email}."
        ),
        "Специалисты перенесут данные из старого кабинета в новый аккаунт.",
    ]


def _render_statuses(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    if KnowledgeAspect.STATUS not in aspects:
        return []
    rows: list[tuple[str, str]] = []
    for line in _source_lines(chunk):
        match = re.match(r"^\s*\d+[.)]\s*([^.!?]{2,72})\.\s*(.+)$", line)
        if match is None:
            continue
        rows.append((match.group(1).strip(" «»"), match.group(2).strip()))
    if not rows:
        return []

    quoted = {_normalized(value) for value in _quoted_values(query)}
    selected = [
        row
        for row in rows
        if any(
            wanted == _normalized(row[0]) or wanted in _normalized(row[0])
            for wanted in quoted
        )
    ]
    if not selected:
        selected = [*rows[:4], *rows[-1:]]
        selected = list(dict.fromkeys(selected))
        concise: list[str] = []
        for label, definition in selected:
            normalized_definition = _normalized(definition)
            if label.casefold().replace("ё", "е") == "на рассмотрении":
                definition = "Заявка находится у организаторов."
            elif label.casefold().replace("ё", "е") == "одобрена":
                definition = "Организаторы одобрили участие."
            elif label.casefold().replace("ё", "е") == "резерв":
                definition = "Отбор пройден; места в основном составе пока нет."
            elif "отклонена администратором" in normalized_definition:
                definition = "Заявка отклонена администратором."
            concise.append(f"Статус «{label}» — {definition}")
        return concise
    return [
        f"Статус «{label}» — {_clean_claim(definition)}"
        for label, definition in selected
    ]


def _render_confirmation(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.CONFIRMATION not in aspects:
        return []
    normalized = _normalized(chunk.text)
    if "подтвердить участие" not in normalized or "мои заявки" not in normalized:
        return []
    duration = re.search(r"в течение\s+(\d+)\s+календарн\w*\s+дн", normalized)
    claims = [
        "В разделе ФГАИС «Мои заявки» нажми кнопку «Подтвердить участие»."
    ]
    if duration:
        claims.append(
            f"Подтверди участие в течение {duration.group(1)} календарных дней."
        )
    return claims


def _render_grant_application_review(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    if KnowledgeAspect.STATUS not in aspects:
        return []
    normalized_query = _normalized(query)
    normalized = _normalized(chunk.text)
    topic = str((chunk.metadata or {}).get("topic") or "")
    if topic != "4_tehnicheskaya_proverka" or not all(
        marker in normalized_query for marker in ("заяв", "провер")
    ):
        return []
    if not all(
        marker in normalized
        for marker in ("сроки технической проверки", "указываются в объявлении")
    ):
        return []
    return [
        "Сроки технической проверки заявки указываются в объявлении о конкурсе."
    ]


def _render_named_shifts(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.SHIFTS not in aspects:
        return []
    names = _quoted_values(chunk.text)
    if len(names) < 2:
        return []
    normalized_names = [name.split("(", 1)[0].strip() for name in names]
    year = _scope_year(chunk.metadata, chunk.text)
    prefix = f"В {year} году: " if year else "Тематические смены: "
    rendered = ", ".join(f"смена «{name}»" for name in normalized_names)
    return [f"{prefix}{rendered}."]


def _render_grant_review(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    normalized_query = _normalized(query)
    normalized = _normalized(chunk.text)
    duration = re.search(r"(?:до\s+)?(\d+)\s+(рабочих\s+)?дн", normalized)
    if KnowledgeAspect.GRANT_AGREEMENT in aspects:
        if (
            "для заключения соглашения" in normalized
            and "три вкладки" in normalized
            and "сумма расходов" in normalized
            and "сумме гранта" in normalized
            and "сроки реализации проекта" in normalized
        ):
            return [
                (
                    "Для заключения соглашения открой раздел "
                    "«Грантовые соглашения»: там доступны три вкладки."
                ),
                (
                    "Во вкладке «Проект» проверь, что сумма расходов строго "
                    "соответствует сумме гранта по приказу."
                ),
                "Там же укажи сроки реализации проекта.",
            ]
        if duration is None or "проект" not in normalized or "куратор" not in normalized:
            return []
        return [
            "Проект проверяет куратор, прикреплённый после выигрыша.",
            f"Процесс первичной проверки проекта занимает до {duration.group(1)} дней.",
            (
                f"Первичная проверка проекта: {duration.group(1)}-дневный "
                "максимальный срок."
            ),
        ]
    if KnowledgeAspect.GRANT_REPORT in aspects:
        if "отчет" not in normalized:
            return []
        asks_tab_opening = "вкладк" in normalized_query and any(
            marker in normalized_query for marker in ("откро", "доступ")
        )
        if asks_tab_opening and all(
            marker in normalized
            for marker in ("вкладка", "откроется", "первый рабочий день")
        ):
            return [
                (
                    "Вкладка «Отчёт» откроется в первый рабочий день после "
                    "окончания срока реализации проекта."
                ),
                (
                    "Срок реализации указан в Приложении 1 к соглашению в "
                    "разделе «Грантовые соглашения»."
                ),
            ]

        asks_rework = any(
            marker in normalized_query
            for marker in ("доработ", "исправ", "устран")
        )
        if asks_rework and re.search(
            r"для грантополучател\w* с 2026 года.{0,32}30 рабочих дней",
            normalized,
        ):
            return [
                (
                    "Для грантополучателей с 2026 года на исправление отчёта "
                    "после комментариев даётся 30 рабочих дней."
                )
            ]

        asks_check = "провер" in normalized_query or (
            "статус отчет на проверке" in normalized_query
        )
        check_duration = re.search(
            r"(?:провер\w*|на проверке).{0,80}(?:в среднем\s+)?до\s+"
            r"(\d+)\s+рабочих\s+дн",
            normalized,
        )
        if asks_check and check_duration is not None:
            days = check_duration.group(1)
            claims = [
                (
                    "Проверка отчёта занимает до "
                    f"{days} рабочих дней."
                ),
                f"Проверка отчёта: {days} рабочих дней — максимальный срок.",
            ]
            if "срок может увеличиваться" in normalized:
                claims.append("Иногда этот срок может увеличиваться.")
            return claims

        asks_submission = any(
            marker in normalized_query
            for marker in ("сдать", "сдач", "срок", "отчетност", "отчётност")
        )
        current_submission = re.search(
            r"для победител\w* с 2026 года.{0,16}?(\d+)\s+рабочих\s+дн",
            normalized,
        )
        if asks_submission and current_submission is not None:
            return [
                (
                    "Для победителей с 2026 года срок сдачи отчётности — "
                    f"{current_submission.group(1)} рабочих дней с даты "
                    "открытия вкладки «Отчёт»."
                )
            ]

        if duration is None:
            return []
        unit = "рабочих дней" if duration.group(2) else "дней"
        return [f"Проверка отчёта занимает до {duration.group(1)} {unit}."]
    return []


def _render_grant_nominations(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.GRANT_NOMINATIONS not in aspects:
        return []
    normalized = _normalized(chunk.text)
    count = re.search(r"\b(\d+)\s+стандартн\w*\s+номинац", normalized)
    if "номинация - это тематика проекта" not in normalized or count is None:
        return []
    return [
        "Номинация — это тематика проекта.",
        f"Предусмотрено {count.group(1)} стандартных номинаций.",
    ]


def _render_eligibility(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.ELIGIBILITY not in aspects:
        return []
    lines = _source_lines(chunk)
    citizen = next(
        (
            line
            for line in lines
            if "гражданин" in _normalized(line)
            and re.search(r"от\s+\d+\s+до\s+\d+\s+лет", _normalized(line))
        ),
        None,
    )
    legal = [line for line in lines if "юридическое лицо" in _normalized(line)]
    claims: list[str] = []
    if citizen:
        citizen_range = re.search(
            r"от\s+(\d+)\s+до\s+(\d+)\s+лет",
            _normalized(citizen),
        )
        if citizen_range:
            claims.extend(
                (
                    (
                        "Участвовать может гражданин Российской Федерации "
                        f"от {citizen_range.group(1)} до "
                        f"{citizen_range.group(2)} лет."
                    ),
                    (
                        "Гражданин: минимальный возраст — "
                        f"{citizen_range.group(1)} лет."
                    ),
                    (
                        "Гражданин: максимальный возраст — "
                        f"{citizen_range.group(2)} лет."
                    ),
                )
            )
        else:
            claims.append(f"Участники: {_clean_claim(citizen).rstrip(',.')}.")
    if legal:
        legal_text = " ".join(_normalized(line) for line in legal)
        age = re.search(r"от\s+(\d+)\s+до\s+(\d+)\s+лет", legal_text)
        foreign = "иностранного государства" in legal_text
        scope = "из России"
        if foreign:
            scope += " или иностранного государства"
        raw_age = f": представитель от {age.group(1)} до {age.group(2)} лет" if age else ""
        claims.append(f"Участвовать может юридическое лицо {scope}{raw_age}.")
        if age:
            claims.extend(
                (
                    (
                        "Представители: минимальный возраст — "
                        f"{age.group(1)} лет."
                    ),
                    (
                        "Представители: максимальный возраст — "
                        f"{age.group(2)} лет."
                    ),
                )
            )
    return claims


def _render_ticket(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.TICKET not in aspects:
        return []
    normalized = _normalized(chunk.text)
    if not all(marker in normalized for marker in ("код билета", "диалоге", "почту")):
        return []
    return [
        "Бот пришлёт код билета.",
        "Билетный код в диалоге и письмо на указанную почту придут после регистрации.",
    ]


def _render_dates(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    if KnowledgeAspect.DATES not in aspects:
        return []
    normalized = _normalized(chunk.text)
    registration_fact = re.search(
        r"\b(?:подач\w*\s+заяв\w*|подать\s+заяв\w*|регистрац\w*\s+во\s+фгаис)",
        normalized,
    )
    if KnowledgeAspect.REGISTRATION in aspects or registration_fact:
        deadline = _deadline_value(chunk.text)
        if deadline:
            prefix = (
                "Регистрация во ФГАИС: заявку можно подать до"
                if "регистрация во фгаис" in normalized
                else "Подать заявку можно до"
            )
            claims = [f"{prefix} {deadline}."]
            platform = next(
                (
                    line
                    for line in _source_lines(chunk)
                    if "платформ" in _normalized(line)
                    and "заяв" in _normalized(line)
                ),
                None,
            )
            if platform:
                platform_name = re.search(
                    r"[«\"]([^»\"]*форум[^»\"]*)[»\"]",
                    platform,
                    re.IGNORECASE,
                )
                if platform_name:
                    claims.append(
                        f"Платформа «{platform_name.group(1)}» используется для подачи заявки."
                    )
                else:
                    claims.append(_clean_claim(platform))
            return claims

    date_range = _date_range_value(chunk)
    if not date_range:
        return []
    label = _date_subject(query, chunk)
    claims: list[str] = []
    if KnowledgeAspect.OVERVIEW in aspects:
        overview = _overview_claim(chunk)
        if overview:
            claims.append(overview)
    claims.append(f"{label} проходит {date_range}.")
    endpoints = _date_endpoint_claim(chunk, label)
    if endpoints:
        claims.append(endpoints)
    if "разъезд" in _normalized(query) or "отъезд" in _normalized(query):
        departure = next(
            (
                line
                for line in _source_lines(chunk)
                if "разъезд" in _normalized(line) or "отъезд" in _normalized(line)
            ),
            None,
        )
        if departure:
            claims.append(_clean_claim(departure))
    return claims


def _render_volunteering(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.VOLUNTEERING not in aspects:
        return []
    normalized = _normalized(chunk.text)
    if (
        "зарегистрироваться в качестве волонтер" in normalized
        and re.search(r"\bфильтр\w*\s+поиска\b", normalized)
        and re.search(r"\bподач\w*\s+заяв\w*", normalized)
    ):
        url = _URL_RE.search(chunk.text)
        link = f" по ссылке {url.group(0).rstrip('.,;:')}" if url else ""
        return [
            f"Можно зарегистрироваться в качестве волонтёра{link}.",
            "Настройка фильтров поиска: местоположение, даты и тематика.",
            "Подача заявки: выбери мероприятие и нажми кнопку подачи заявки.",
            "Следующим шагом в некоторых мероприятиях нужно заполнить анкету.",
        ]
    lines = _source_lines(chunk)
    return [
        _clean_claim(line)
        for line in lines
        if "волонтер" in _normalized(line) or "подач заяв" in _normalized(line)
    ][:4]


def _render_navigation(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    if KnowledgeAspect.NAVIGATION not in aspects:
        return []
    normalized = _normalized(chunk.text)
    if "регион проведения" in normalized and "регион участников" in normalized:
        common = [
            "В личном кабинете доступно разделение мероприятий по уровням.",
            "Фильтры универсальные для всех уровней мероприятий.",
            "Из любого подраздела можно поставить фильтр на другой уровень.",
        ]
        if "регион" not in _normalized(query):
            return common
        return [
            common[1],
            (
                "Фильтр по региону проведения показывает место мероприятия; "
                "подать заявку можно из любой точки России."
            ),
            (
                "Фильтр по региону участников показывает мероприятия для тех, "
                "кто живёт в указанном регионе."
            ),
        ]
    if "всероссийские и окружные форумы" in normalized:
        url = _URL_RE.search(chunk.text)
        suffix = f" на странице {url.group(0).rstrip('.,;:')}" if url else ""
        return [
            (
                "Собрана информация о форумах: Всероссийские и окружные форумы "
                f"Форумной Дирекции{suffix}."
            )
        ]
    excerpts = extract_source_fact_excerpts(
        chunk.text,
        "фильтры поиска мероприятий по регионам",
        chunk.metadata,
        max_chars=360,
        max_units=4,
        requested_aspects=frozenset({KnowledgeAspect.NAVIGATION}),
    )
    return [_clean_claim(value) for value in excerpts]


def _render_technical(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.TECHNICAL not in aspects:
        return []
    return [
        _clean_claim(line)
        for line in _source_lines(chunk)
        if "неактив" in _normalized(line) or "ошиб" in _normalized(line)
    ][:2]


def _render_registration(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.REGISTRATION not in aspects:
        return []
    lines = _source_lines(chunk)
    normalized = _normalized(chunk.text)
    if "регистрация для граждан рф на фгаис" in normalized:
        citizen = next(
            line
            for line in lines
            if "регистрация для граждан рф" in _normalized(line)
        )
        url = _URL_RE.search(citizen)
        suffix = f": {url.group(0).rstrip('.,;:')}" if url else ""
        return [f"Для граждан РФ регистрация на ФГАИС; ссылка для граждан РФ{suffix}."]

    if "зарегистрироваться на сайте таврида" in normalized:
        return [_clean_claim(lines[0])] if lines else []

    if "письмо на указанный email" in normalized:
        return [
            "Для регистрации на портале Добро заполни анкету.",
            "После этого придёт письмо на указанный email.",
            "Подтверждение аккаунта выполняется по этому письму.",
            "После подтверждения аккаунт будет создан.",
        ]

    if all(
        marker in normalized
        for marker in ("верифицировать профиль", "оформить идею проекта", "подать заявку")
    ):
        return [
            (
                "Верифицировать профиль ФГАИС через ЕСИА, то есть привязать "
                "аккаунт Госуслуг."
            ),
            "Оформить идею проекта в шаблоне конкурса.",
            "Подать заявку на ФГАИС «Молодёжь России».",
        ]

    action_markers = (
        "регистрация проходит",
        "верифицировать профиль",
        "верификацию учетной записи",
        "заполнить все обязательные",
        "оформить идею проекта",
        "подать заявку",
        "кнопку «зарегистрироваться»",
        "нажатия на кнопку «зарегистрироваться»",
        "регистрация во фгаис",
        "госуслуг",
    )
    actions = [
        _clean_claim(line)
        for line in lines
        if any(marker in _normalized(line) for marker in action_markers)
    ]
    if actions:
        return actions[:4]
    return []


def _render_results_or_program(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    if not aspects & {KnowledgeAspect.RESULTS, KnowledgeAspect.PROGRAM}:
        return []
    lines = _source_lines(chunk)
    if not lines:
        return []
    if KnowledgeAspect.RESULTS in aspects:
        normalized_query = _normalized(query)
        result_lines = [
            line
            for line in lines
            if any(
                marker in _normalized(line)
                for marker in (
                    "результат",
                    "итоги отбор",
                    "список победител",
                    "приказ о победител",
                    "публикации приказа",
                )
            )
        ]
        if any(marker in normalized_query for marker in ("приказ", "опублик")):
            publication = next(
                (
                    line
                    for line in result_lines
                    if any(
                        marker in _normalized(line)
                        for marker in ("приказ", "опублик", "размещается")
                    )
                ),
                None,
            )
            if publication:
                return [_clean_claim(publication)]
        normalized = _normalized(lines[0])
        duration = re.search(r"за\s+(\d+)\s+календарн\w*\s+дн", normalized)
        if duration and "до даты начала смен" in normalized:
            return [
                (
                    f"Результаты отбора: {duration.group(1)} календарных дней "
                    "до даты начала смены — предельный срок публикации."
                )
            ]
        if result_lines:
            return [_clean_claim(result_lines[0])]
    return [_clean_claim(lines[0])]


def _render_overview(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    del query
    if KnowledgeAspect.OVERVIEW not in aspects:
        return []
    return [_clean_claim(line) for line in _source_lines(chunk)[:2]]


def _render_stay(
    chunk: ScoredChunk,
    *,
    query: str,
    aspects: frozenset[KnowledgeAspect],
) -> list[str]:
    if not aspects & {KnowledgeAspect.ACCOMMODATION, KnowledgeAspect.FOOD}:
        return []
    lines = _source_lines(chunk)
    wanted = [
        _clean_claim(line)
        for line in lines
        if "питан" in _normalized(line)
        or "прожив" in _normalized(line)
        or "организатор" in _normalized(line)
    ]
    if len(aspects & {KnowledgeAspect.ACCOMMODATION, KnowledgeAspect.FOOD}) == 2:
        wanted = [
            re.sub(
                r"за\s+счет\s+организаторов\b",
                "за счёт организаторов форума",
                value,
                flags=re.IGNORECASE,
            )
            for value in wanted
        ]
    del query
    return wanted[:3]


def _source_lines(chunk: ScoredChunk) -> list[str]:
    return [unit.text for unit in source_fact_units(chunk.text, chunk.metadata)]


def _fit_claims(values: list[str], *, max_chars: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    used = 0
    for value in values:
        claim = _clean_claim(value)
        normalized = _normalized(claim)
        if not claim or normalized in seen:
            continue
        separator = 1 if result else 0
        if used + separator + len(claim) > max_chars:
            continue
        seen.add(normalized)
        result.append(claim)
        used += separator + len(claim)
    return result


def _clean_claim(value: str) -> str:
    claim = _EMOJI_RE.sub("", str(value or "")).replace("\u00a0", " ")
    claim = claim.replace("➡", "; затем ")
    claim = claim.replace("Дирекция.Всероссийские", "Дирекция. Всероссийские")
    claim = re.sub(r"\bпо адресу\s+ТП\s+", "по адресу ", claim, flags=re.IGNORECASE)
    claim = re.sub(r"\bпри помощи Госуслуг\b", "с помощью Госуслуг", claim)
    claim = re.sub(r"\bне позднее,\s*чем\b", "не позднее чем", claim)
    claim = claim.replace("творческое и видеовизитка", "творческое задание и видеовизитка")
    claim = " ".join(claim.split()).strip(" •\t")
    if claim.endswith(":"):
        claim = claim[:-1].rstrip() + "."
    elif claim and claim[-1] not in ".!?":
        claim += "."
    return claim


def _overview_claim(chunk: ScoredChunk) -> str | None:
    lines = _source_lines(chunk)
    return _clean_claim(lines[0]) if lines else None


def _deadline_value(source_text: str) -> str | None:
    numeric = _NUMERIC_DATE_RE.search(source_text)
    word = _WORD_DATE_RE.search(source_text)
    if numeric:
        value = (
            f"{int(numeric.group('day')):02d}."
            f"{int(numeric.group('month')):02d}.{numeric.group('year')}"
        )
    elif word:
        value = (
            f"{int(word.group('day'))} {word.group('month')} "
            f"{word.group('year')} года"
        )
    else:
        return None
    time_match = _TIME_RE.search(source_text)
    if time_match:
        timezone = " мск" if "мск" in _normalized(source_text) else ""
        value += f" в {time_match.group(0)}{timezone}"
    return value


def _date_range_value(chunk: ScoredChunk) -> str | None:
    source_text = chunk.text.replace("\u00a0", " ")
    word = _WORD_RANGE_RE.search(source_text)
    if word:
        year = (
            word.group("end_year")
            or word.group("start_year")
            or _scope_year(chunk.metadata, source_text)
        )
        start_year = word.group("start_year") or year
        end_year = word.group("end_year") or year
        start = f"{int(word.group('start'))} {word.group('start_month')}"
        end = f"{int(word.group('end'))} {word.group('end_month')}"
        if start_year:
            start += f" {start_year}"
        if end_year:
            end += f" {end_year}"
        return f"с {start} по {end}"

    same_month = _SAME_MONTH_RANGE_RE.search(source_text)
    if same_month:
        year = same_month.group("year") or _scope_year(chunk.metadata, source_text)
        value = (
            f"с {int(same_month.group('start'))} по "
            f"{int(same_month.group('end'))} {same_month.group('month')}"
        )
        return f"{value} {year} года" if year else value
    return None


def _date_endpoint_claim(chunk: ScoredChunk, label: str) -> str | None:
    source_text = chunk.text.replace("\u00a0", " ")
    same_month = _SAME_MONTH_RANGE_RE.search(source_text)
    if same_month is None:
        return None
    year = same_month.group("year") or _scope_year(chunk.metadata, source_text)
    year_suffix = f" {year} года" if year else ""
    month = same_month.group("month")
    return (
        f"{label}: начало {int(same_month.group('start'))} {month}{year_suffix}, "
        f"завершение {int(same_month.group('end'))} {month}{year_suffix}."
    )


def _date_subject(query: str, chunk: ScoredChunk) -> str:
    if KnowledgeAspect.OVERVIEW in infer_source_aspects(chunk.metadata, chunk.text):
        return "Форум"
    ordinal = _source_shift_ordinal(chunk)
    if ordinal:
        return f"{_ORDINAL_WORDS.get(ordinal, str(ordinal))} смена"
    match = re.search(
        r"смен\w*\s*[«„\"]([^»“\"]{2,64})[»“\"]",
        query,
        flags=re.IGNORECASE,
    )
    if match:
        return f"Смена «{match.group(1).strip()}»"
    return "Форум"


def _source_shift_ordinal(chunk: ScoredChunk) -> int | None:
    signature = " ".join(
        (
            str((chunk.metadata or {}).get("topic") or "").replace("_", " "),
            str((chunk.metadata or {}).get("intent_name") or ""),
            _heading_text(chunk.metadata or {}),
        )
    )
    match = re.search(r"\b([1-6])\s*(?:-?я\s+)?смен", signature, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _scope_year(metadata: dict[str, Any], source_text: str = "") -> str | None:
    signature = f"{_heading_text(metadata)} {source_text[:160]}"
    match = re.search(r"\b(20\d{2})\b", signature)
    return match.group(1) if match else None


def _heading_text(metadata: dict[str, Any]) -> str:
    heading = metadata.get("source_heading_path") or []
    if isinstance(heading, (list, tuple)):
        return " ".join(str(value) for value in heading)
    return str(heading or "")


def _query_shift_ordinals(query: str) -> tuple[int, ...]:
    normalized = _normalized(query)
    values: list[int] = []
    words = (("перв", 1), ("втор", 2), ("трет", 3), ("четверт", 4))
    for stem, ordinal in words:
        if re.search(
            rf"(?:\b{stem}\w*.{{0,36}}\bсмен\w*|"
            rf"\bсмен\w*.{{0,36}}\b{stem}\w*)",
            normalized,
        ):
            values.append(ordinal)
    values.extend(
        int(value)
        for value in re.findall(r"\b([1-6])\s*(?:-?я\s+)?смен", normalized)
    )
    return tuple(dict.fromkeys(values))


def _asks_overall_and_shift_dates(query: str) -> bool:
    normalized = _normalized(query)
    return bool(
        "смен" in normalized
        and any(marker in normalized for marker in ("общий период", "период форума"))
    )


def _asks_specific_shift_dates(query: str) -> bool:
    normalized = _normalized(query)
    return "смен" in normalized and bool(
        _query_shift_ordinals(query)
        or re.search(r"смен\w*\s*[«„\"]", query, flags=re.IGNORECASE)
    )


def _quoted_values(text: str) -> list[str]:
    return [
        " ".join(value.split())
        for value in re.findall(r"[«„\"]([^»“\"]{2,96})[»“\"]", text)
    ]


def _normalized(value: Any) -> str:
    normalized = str(value or "").casefold().replace("ё", "е").replace("\u00a0", " ")
    return " ".join(normalized.split())


def _candidate_rank(
    chunk: ScoredChunk,
    *,
    query: str,
    aspect: KnowledgeAspect,
    query_tokens: set[str],
    preferred_rank: dict[str, int],
    requested_coverage: int,
) -> tuple[int, int, int, int, int, float, str]:
    metadata = chunk.metadata or {}
    topic = str(metadata.get("topic") or "").strip()
    topic_rank = preferred_rank.get(topic, len(preferred_rank) + 1)
    haystack = " ".join(
        (
            topic.replace("_", " "),
            str(metadata.get("intent_name") or ""),
            str(metadata.get("forum_normalized") or ""),
            _heading_text(metadata),
            chunk.text[:600],
        )
    )
    lexical_overlap = len(query_tokens & _tokens(haystack))
    answer_signal = source_answer_signal_score(
        query,
        aspect,
        metadata,
        chunk.text,
    )
    # A precise published heading is stronger evidence than a facet discovered
    # inside a broad FAQ body. Body discovery is the recall fallback when no
    # dedicated card exists; it must not displace an exact source card.
    heading_aspects = infer_source_aspects(metadata, "")
    return (
        int(aspect not in heading_aspects),
        -answer_signal,
        topic_rank,
        -requested_coverage,
        -lexical_overlap,
        -float(chunk.reranker_score or chunk.score or 0.0),
        chunk.chunk_id,
    )


def _chunk_matches_scope(
    chunk: ScoredChunk,
    *,
    query: str,
    category: str | None,
    forum_normalized: str | None,
) -> bool:
    metadata = chunk.metadata or {}
    expected_category = str(category or "").strip()
    source_category = str(metadata.get("category") or "").strip()
    if expected_category and source_category != expected_category:
        return False

    expected_forum = str(forum_normalized or "").strip()
    source_forum = str(metadata.get("forum_normalized") or "").strip()
    if expected_forum:
        if forums_are_equivalent(source_forum, expected_forum):
            return True
        return _matches_explicit_grant_season(
            query,
            expected_category,
            source_forum,
        )
    if expected_category == "форумы":
        return not source_forum
    return True


def _matches_explicit_grant_season(
    query: str,
    category: str,
    source_forum: str,
) -> bool:
    normalized_query = str(query or "").casefold().replace("ё", "е")
    normalized_forum = str(source_forum or "").casefold().replace("ё", "е")
    return bool(
        category == "гранты"
        and "сезон" in normalized_query
        and "сезон" in normalized_forum
        and "грант" in normalized_forum
    )


def _is_published_yonote_chunk(chunk: ScoredChunk) -> bool:
    metadata = chunk.metadata or {}
    return (
        str(metadata.get("source_type") or "").strip().casefold() == "yonote"
        and str(metadata.get("source") or "").strip().casefold() == "yonote_api"
        and str(metadata.get("version") or "").strip() == "yonote-api-v1"
        and str(metadata.get("status") or "").strip().casefold() == "published"
    )


def _tokens(text: Any) -> set[str]:
    return set(semantic_fact_tokens(text)) - _QUERY_STOPWORDS
