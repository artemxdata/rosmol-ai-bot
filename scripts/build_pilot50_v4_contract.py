from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.pilot50 import PROJECT_ROOT, build_materialized_cases

V3_MANIFEST = PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v3.json"
TYPICAL_PATH = PROJECT_ROOT / "eval" / "cases" / "pilot50_typical_v4.json"
ATYPICAL_PATH = PROJECT_ROOT / "eval" / "cases" / "pilot50_atypical_v4.json"
MANIFEST_PATH = PROJECT_ROOT / "eval" / "cases" / "pilot50_balanced_v4.json"
AS_OF_DATE = "2026-08-14"
V3_TAGS = frozenset({"pilot50:v3", "type:typical", "type:atypical"})

GRANT_APPLICATION_CHUNK = "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm"
GRANT_APPLICATION_DUPLICATE = "yonote_api_jdq60oodtx_s0006_poshagovyy_algoritm"


def _text(*alternatives: str) -> dict[str, Any]:
    return {"kind": "text_any", "alternatives": list(alternatives)}


def _date(value: str, *contexts: str, position: str = "before") -> dict[str, Any]:
    return {
        "kind": "date",
        "value": value,
        "context_any": list(contexts),
        "context_position": position,
    }


def _range(
    start: str,
    end: str,
    *contexts: str,
    position: str = "before",
) -> dict[str, Any]:
    return {
        "kind": "date_range",
        "start": start,
        "end": end,
        "context_any": list(contexts),
        "context_position": position,
    }


def _time(
    value: str,
    *contexts: str,
    position: str = "before",
    timezone: str | None = None,
) -> dict[str, Any]:
    group: dict[str, Any] = {
        "kind": "time",
        "value": value,
        "context_any": list(contexts),
        "context_position": position,
    }
    if timezone is not None:
        group["timezone"] = timezone
    return group


def _number(
    value: int,
    *contexts: str,
    position: str = "before",
) -> dict[str, Any]:
    return {
        "kind": "number",
        "value": value,
        "context_any": list(contexts),
        "context_position": position,
    }


def _default_groups(case: dict[str, Any]) -> list[dict[str, Any]]:
    anchors = case.pop("expected_answer_contains")
    return [_text(str(anchor)) for anchor in anchors]


def _semantic_groups(ordinal: int, case: dict[str, Any]) -> list[dict[str, Any]]:
    base_groups = _default_groups(case)
    # Some override expressions reuse positions from their own v3 case. The
    # mapping is built eagerly, so keep non-output padding for unrelated rows.
    groups = [
        *base_groups,
        *([_text("internal non-output padding")] * max(0, 5 - len(base_groups))),
    ]
    overrides: dict[int, list[dict[str, Any]]] = {
        1: [
            _text(
                "прислать ID этого аккаунта по адресу support@myrosmol.ru",
                "отправить ID аккаунта на support@myrosmol.ru",
            )
        ],
        4: [
            _text("статус «На рассмотрении»", "статус на рассмотрении"),
            _text("статус «Одобрена»", "заявка одобрена"),
            _text("статус «Отклонена»", "заявка отклонена"),
        ],
        6: [
            _text(
                "письмо на указанный email",
                "письмо на электронную почту",
                "письмо на указанную почту",
            )
        ],
        7: [
            _text(
                "зарегистрироваться в качестве волонтёра",
                "подать заявку как волонтёр",
                "подать волонтёрскую заявку",
            )
        ],
        8: [
            _date("2026-06-30", "подать заявку", "приём заявок"),
            _time("23:59", "подать заявку", "приём заявок", timezone="мск"),
        ],
        10: [
            _text(
                "затраты на проезд могут быть компенсированы",
                "обратиться в региональный орган",
                "орган по делам молодёжи в вашем регионе",
            )
        ],
        11: [_date("2026-09-12", "регистрация", "подать заявку")],
        12: [
            _number(18, "возраст", "участники"),
            _number(35, "возраст", "участники"),
        ],
        13: [
            _text("смена «Единство»", "смена Единство"),
            _text("смена «Правда»", "смена Правда"),
            _text("смена «Родина»", "смена Родина"),
        ],
        14: [_number(30, "проверка отчёта")],
        15: [
            _text("проект проверяет куратор", "проверяет прикреплённый куратор"),
            _number(30, "проверка проекта", "первичная проверка"),
        ],
        16: [
            _text("номинация — это тематика проекта"),
            _number(18, "стандартных номинаций", position="after"),
        ],
        17: [
            _text(
                "регистрация проходит по ссылке",
                "перейти по ссылке для регистрации",
                "зарегистрироваться на платформе",
            )
        ],
        18: [
            _text(
                "фильтры универсальные",
                "поставить фильтр",
                "отсортировать по регионам",
            )
        ],
        19: [
            _text("статус «На рассмотрении»", "статус на рассмотрении"),
            _text("статус «Одобрена»", "заявка одобрена"),
        ],
        20: [
            _text(
                "верифицировать профиль ФГАИС через ЕСИА",
                "привязать аккаунт Госуслуг",
            ),
            _text("подать заявку на ФГАИС", "подать заявку в ФГАИС"),
        ],
        21: [
            _text(
                "цель конкурса — вовлечение молодёжи",
                "грантовый конкурс для молодёжи",
            )
        ],
        24: [
            _text(
                "молодёжное событие в Ленинградской области",
                "форум в Ленинградской области",
            )
        ],
        25: [
            _number(14, "результаты отбора", "списки участников"),
            _text("до даты начала смены", "до начала смены"),
        ],
        26: [
            _text("регистрация проходит по ссылке", "перейти по ссылке для регистрации"),
            _text("регион проведения", "фильтр по региону проведения"),
            _text("регион участников", "фильтр по региону участников"),
        ],
        27: [
            _text("создать новый аккаунт", "создать аккаунт через Госуслуги"),
            _text("войти через Госуслуги", "аккаунт с помощью Госуслуг"),
            _text("прислать ID этого аккаунта", "отправить ID аккаунта"),
            _text(
                "по адресу support@myrosmol.ru",
                "на почту support@myrosmol.ru",
            ),
            _text(
                "организаторы одобрили участие",
                "статус «Одобрена»",
            ),
        ],
        28: [
            _date("2026-06-30", "подать заявку", "приём заявок"),
            _text("3-разовое питание", "трёхразовое питание"),
            _text("за счёт организаторов форума"),
            _text("затраты на проезд могут быть компенсированы"),
        ],
        29: [
            _date("2026-09-12", "регистрация", "подать заявку"),
            groups[1],
            _number(18, "физические лица", "гражданин"),
            _number(35, "физические лица", "гражданин"),
            groups[3],
            groups[4],
            _number(18, "юридические лица", "представители"),
            _number(55, "юридические лица", "представители"),
        ],
        30: [
            groups[0],
            _range(
                "2026-07-20",
                "2026-08-06",
                "форум",
                "мероприятие",
            ),
            _text("смена «Единство»", "смена Единство"),
            _text("смена «Правда»", "смена Правда"),
            _text("смена «Родина»", "смена Родина"),
        ],
        31: [
            _text("номинация — это тематика проекта"),
            _number(18, "стандартных номинаций", position="after"),
            *groups[2:],
        ],
        32: [
            _number(30, "проверка соглашения", "проверка проекта"),
            _number(30, "проверка отчёта"),
        ],
        33: [
            _text("письмо на указанный email", "письмо на электронную почту"),
            _text("подтверждение аккаунта", "подтвердить аккаунт"),
            groups[2],
            groups[3],
            _text("подать заявку", "подача заявки"),
        ],
        34: [
            _number(14, "результаты отбора", "списки участников"),
            groups[1],
        ],
        35: [
            _range("2026-08-08", "2026-08-15", "первая смена"),
            _range("2026-08-15", "2026-08-22", "вторая смена"),
            _text("разъезд участников", "день разъезда"),
            _text("отъезд участников", "день отъезда"),
        ],
        36: [
            _range("2026-07-20", "2026-08-06", "форум", "мероприятие"),
            _range("2026-07-26", "2026-07-30", "смена Правда", "Правда"),
        ],
        37: [
            _text("регистрация во ФГАИС", "заявка во ФГАИС"),
            _date("2026-07-06", "регистрация", "заявка"),
            _time("23:59", "регистрация", "заявка", timezone="мск"),
        ],
        38: [
            _text("регистрация во ФГАИС", "заявка во ФГАИС"),
            _date("2026-07-06", "регистрация", "заявка"),
            _time("23:59", "регистрация", "заявка", timezone="мск"),
        ],
        39: [
            _text("регистрация во ФГАИС", "заявка во ФГАИС"),
            _date("2026-07-06", "регистрация", "заявка"),
            _time("23:59", "регистрация", "заявка", timezone="мск"),
        ],
        41: [
            _text("регистрация проходит по ссылке", "перейти по ссылке для регистрации"),
            _text("с помощью Госуслуг", "через Госуслуги"),
            _text("кнопку «Зарегистрироваться»", "нажать «Зарегистрироваться»"),
        ],
        42: [
            _text("информация о форумах", "перечень форумов"),
            groups[1],
        ],
        43: [
            _text("регистрация на ФГАИС", "зарегистрироваться на ФГАИС"),
            _text("ссылка для граждан России", "ссылка для граждан РФ"),
        ],
        44: [
            groups[0],
            groups[1],
            _text(
                "творческое задание и видеовизитка",
                "выполнить видеовизитку",
            ),
            groups[3],
        ],
        45: [
            _text("регистрация во ФГАИС", "заявка во ФГАИС"),
            _date("2026-07-06", "регистрация", "заявка"),
            _time("23:59", "регистрация", "заявка", timezone="мск"),
        ],
        47: [
            _text("код билета", "билетный код"),
            _text("код в диалоге", "пришлёт код в диалоге"),
            _text("письмо на указанную почту", "направит на почту"),
        ],
        48: [
            _date("2026-06-30", "подать заявку", "приём заявок"),
            _time("23:59", "подать заявку", "приём заявок", timezone="мск"),
            _text("платформа «Росмолодёжь.Форумы»", "платформе Росмолодёжь.Форумы"),
        ],
        49: [_range("2026-07-26", "2026-07-30", "смена Правда", "Правда")],
        50: [_range("2026-08-08", "2026-08-15", "первая смена")],
    }
    return overrides.get(ordinal, base_groups)


def _apply_temporal_contract(ordinal: int, case: dict[str, Any]) -> None:
    contracts = {
        8: (
            "Можно ли было подать заявку на форум «Ладога» по состоянию на "
            "14 августа 2026 года и до какой даты принимали заявки?",
            "closed",
        ),
        37: (
            case["query"] + " По состоянию на 14 августа 2026 года приём уже закрыт?",
            "closed",
        ),
        38: (
            case["query"] + " По состоянию на 14 августа 2026 года приём уже закрыт?",
            "closed",
        ),
        39: (
            case["query"] + " По состоянию на 14 августа 2026 года приём уже закрыт?",
            "closed",
        ),
        45: (
            case["query"] + " По состоянию на 14 августа 2026 года приём уже закрыт?",
            "closed",
        ),
        48: (
            "До какого срока принимали заявки на «Ладогу» и был ли приём уже "
            "закрыт по состоянию на 14 августа 2026 года?",
            "closed",
        ),
        49: (
            "Когда проходила смена «Правда» форума «Территория смыслов» и "
            "завершилась ли она к 14 августа 2026 года?",
            "completed",
        ),
        50: (
            "Когда проходит первая смена «Машука» и продолжалась ли она по "
            "состоянию на 14 августа 2026 года?",
            "in_progress",
        ),
    }
    contract = contracts.get(ordinal)
    if contract is None:
        return
    case["query"], case["expected_temporal_polarity"] = contract
    case["temporal_as_of_date"] = AS_OF_DATE


def _v4_case(ordinal: int, source: dict[str, Any]) -> dict[str, Any]:
    case = copy.deepcopy(source)
    case.pop("pilot50_group", None)
    case["user_id"] = f"synthetic-pilot50-v4-source-{ordinal:02d}"
    case["tags"] = [tag for tag in case.get("tags", []) if tag not in V3_TAGS]
    if ordinal == 16:
        case["id"] = "pilot50_v4_grant_nomination_definition"
        case["query"] = (
            "Что такое номинация грантового конкурса и сколько стандартных "
            "номинаций предусмотрено?"
        )
    case["expected_answer_fact_groups"] = _semantic_groups(ordinal, case)
    if GRANT_APPLICATION_CHUNK in case.get("expected_chunk_ids", []):
        case["equivalent_chunk_ids"] = {
            GRANT_APPLICATION_CHUNK: [GRANT_APPLICATION_DUPLICATE]
        }
    _apply_temporal_contract(ordinal, case)
    return case


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_contract() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    v3_cases, _receipt = build_materialized_cases(V3_MANIFEST)
    cases = [_v4_case(index, case) for index, case in enumerate(v3_cases, start=1)]
    typical, atypical = cases[:25], cases[25:]
    sources = []
    for path, group, rows in (
        (TYPICAL_PATH, "typical", typical),
        (ATYPICAL_PATH, "atypical", atypical),
    ):
        sources.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": _sha256(_canonical_bytes(rows)),
                "type": group,
                "selection_rule": "all_cases",
                "case_ids": [case["id"] for case in rows],
            }
        )
    manifest = {
        "schema_version": "1.0.0",
        "dataset_id": "pilot50_balanced_v4",
        "classification": "calibration_only",
        "human_product_verdict": False,
        "strata_contract": {
            "typical": (
                "single-intent frequent in-scope questions answerable from the "
                "published knowledge contract"
            ),
            "atypical": (
                "noisy, slang, profane, precise-aspect or multi-aspect in-scope "
                "questions that remain answerable without an operator"
            ),
        },
        "expected_contract": {
            "cases_total": 50,
            "type_counts": {"typical": 25, "atypical": 25},
            "expected_behavior": "answer",
            "expected_escalated": False,
        },
        "sources": sources,
        "disclaimer": (
            "Tracked regression calibration only; not an independent holdout or a "
            "human product-conversion verdict."
        ),
    }
    return typical, atypical, manifest


def _write(path: Path, value: Any) -> None:
    path.write_bytes(_canonical_bytes(value))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the versioned Pilot50 v4 calibration contract."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the three v4 contract files; otherwise only print digests.",
    )
    args = parser.parse_args()
    typical, atypical, manifest = build_contract()
    if args.write:
        _write(TYPICAL_PATH, typical)
        _write(ATYPICAL_PATH, atypical)
        _write(MANIFEST_PATH, manifest)
    summary = {
        "manifest_raw_sha256": _sha256(_canonical_bytes(manifest)),
        "manifest_canonical_sha256": _sha256(_canonical_bytes(manifest)),
        "typical_source_sha256": _sha256(_canonical_bytes(typical)),
        "atypical_source_sha256": _sha256(_canonical_bytes(atypical)),
        "cases_total": len(typical) + len(atypical),
    }
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
