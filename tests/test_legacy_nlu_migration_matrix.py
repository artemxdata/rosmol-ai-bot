from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from src.response_contract import ResponseProfileName

MATRIX_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "legacy_nlu_migration_matrix_v1.json"
)
DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "legacy_nlu_response_contract.md"
)

ALLOWED_DISPOSITIONS = {
    "exact_static",
    "yonote_template",
    "current_safety_policy",
    "deprecated",
}
ALLOWED_RESPONSE_PROFILES = {
    profile.value for profile in ResponseProfileName
}
EXPECTED_CONTOUR_NAMES = {
    "NLU/гранты",
    "NLU/другое",
    "NLU/территория",
    "NLU/иволга",
    "NLU/Машук",
    "NLU/Бирюса",
    "NLU/Истоки",
    "NLU/Утро",
    "NLU/Таврида",
    "NLU/Амур",
    "NLU/Агропродвижение",
    "NLU/На волне",
    "Область будущего",
    "Каспий",
    "Полюс",
    "ОстроVа",
    "Ладога",
    "Шерегеш",
    "Студенческий спецназ",
    "Волга",
    "NLU/Истоки школа",
    "NLU/Ростов",
    "NLU/Арктика",
    "NLU/Добрино",
    "NLU/ШУМ",
    "NLU/ГосСтарт",
    "NLU/День молодёжи",
    "NLU/Путешествие",
    "Российский Север",
    "Ямолод",
    "Экосистема",
    "Территория БезОпасности",
    "КМОЦ Полюс",
    "КМОЦ Спортэкс",
    "КМОЦ Дон",
    "КМОЦ Вместе",
    "КМОЦ ГосСтарт",
    "КМОЦ Экосистема",
    "КМОЦ Добрино",
    "КМОЦ Острова",
    "КМОЦ Маяк",
    "КМОЦ Профилактика и безопасность",
}


def _load_matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_matrix_schema_and_baseline_counts() -> None:
    matrix = _load_matrix()

    assert matrix["matrix_version"] == "1.0.0"
    assert matrix["content_fields_included"] is False
    assert matrix["allowed_dispositions"] == [
        "exact_static",
        "yonote_template",
        "current_safety_policy",
        "deprecated",
    ]
    assert set(matrix["allowed_dispositions"]) == ALLOWED_DISPOSITIONS
    assert set(matrix["allowed_response_profiles"]) == ALLOWED_RESPONSE_PROFILES
    assert matrix["baseline"]["nlu_contour_count"] == 42
    assert matrix["baseline"]["reachable_answer_node_count"] == 762
    assert matrix["baseline"]["root_overlap_node_count"] == 0
    assert matrix["baseline"]["first_level_branch_overlap_node_count"] == 0
    assert len(matrix["contours"]) == 42
    assert (
        set(matrix["baseline"]["response_profile_node_counts"])
        == ALLOWED_RESPONSE_PROFILES
    )
    assert sum(matrix["baseline"]["response_profile_node_counts"].values()) == 762


def test_matrix_lists_exactly_42_unique_legacy_contours() -> None:
    contours = _load_matrix()["contours"]
    names = [contour["legacy_name"] for contour in contours]
    indices = [contour["legacy_slot_index"] for contour in contours]

    assert len(names) == len(set(names)) == 42
    assert set(names) == EXPECTED_CONTOUR_NAMES
    assert len(indices) == len(set(indices)) == 42
    assert all(name == name.strip() for name in names)


def test_every_answer_node_has_one_allowed_disposition_partition() -> None:
    matrix = _load_matrix()
    aggregate = Counter({disposition: 0 for disposition in ALLOWED_DISPOSITIONS})

    for contour in matrix["contours"]:
        counts = contour["disposition_counts"]
        assignment = contour["assignment"]
        assert set(counts) == ALLOWED_DISPOSITIONS
        assert all(isinstance(value, int) and value >= 0 for value in counts.values())
        assert sum(counts.values()) == contour["covered_node_count"]
        aggregate.update(counts)

        assert assignment["partition_key"] == "first_level_branch_slot_index"
        assert assignment["default_disposition"] in ALLOWED_DISPOSITIONS
        exception_indices = [
            exception["branch_slot_index"]
            for exception in assignment["exceptions"]
        ]
        assert len(exception_indices) == len(set(exception_indices))
        assert all(
            exception["disposition"] in ALLOWED_DISPOSITIONS
            and exception["disposition"] != assignment["default_disposition"]
            for exception in assignment["exceptions"]
        )

    assert sum(contour["covered_node_count"] for contour in matrix["contours"]) == 762
    assert dict(aggregate) == matrix["baseline"]["disposition_node_counts"]
    assert sum(aggregate.values()) == 762


def test_every_answer_node_has_one_response_profile_partition() -> None:
    matrix = _load_matrix()
    aggregate = Counter({profile: 0 for profile in ALLOWED_RESPONSE_PROFILES})

    for contour in matrix["contours"]:
        profile_counts = contour["response_profile_counts"]
        assignment = contour["response_profile_assignment"]

        assert set(profile_counts) == ALLOWED_RESPONSE_PROFILES
        assert all(
            isinstance(value, int) and value >= 0
            for value in profile_counts.values()
        )
        assert sum(profile_counts.values()) == contour["covered_node_count"]

        assert assignment["partition_key"] == "first_level_branch_slot_index"
        default_profile = assignment["default_profile"]
        assert default_profile in ALLOWED_RESPONSE_PROFILES

        exceptions = assignment["exceptions"]
        exception_indices = [
            exception["branch_slot_index"] for exception in exceptions
        ]
        assert len(exception_indices) == len(set(exception_indices))
        assert all(
            isinstance(exception["branch_slot_index"], int)
            and exception["response_profile"] in ALLOWED_RESPONSE_PROFILES
            and exception["response_profile"] != default_profile
            and isinstance(exception["covered_node_count"], int)
            and exception["covered_node_count"] > 0
            for exception in exceptions
        )

        exception_node_count = sum(
            exception["covered_node_count"] for exception in exceptions
        )
        assert exception_node_count <= contour["covered_node_count"]
        derived_counts = Counter(
            {profile: 0 for profile in ALLOWED_RESPONSE_PROFILES}
        )
        derived_counts[default_profile] = (
            contour["covered_node_count"] - exception_node_count
        )
        for exception in exceptions:
            derived_counts[exception["response_profile"]] += exception[
                "covered_node_count"
            ]

        assert dict(derived_counts) == profile_counts
        aggregate.update(profile_counts)

    assert dict(aggregate) == matrix["baseline"]["response_profile_node_counts"]
    assert sum(aggregate.values()) == 762


def test_event_contours_are_yonote_first_and_general_service_is_mixed() -> None:
    contours = _load_matrix()["contours"]
    events = [contour for contour in contours if contour["contour_kind"] == "event"]
    general = next(
        contour for contour in contours if contour["contour_kind"] == "general_service"
    )

    assert events
    assert all(
        contour["assignment"]["default_disposition"] == "yonote_template"
        for contour in events
    )
    assert sum(contour["disposition_counts"]["yonote_template"] for contour in events) > sum(
        contour["disposition_counts"]["deprecated"] for contour in events
    )
    assert {
        disposition
        for disposition, count in general["disposition_counts"].items()
        if count
    } == ALLOWED_DISPOSITIONS


def test_matrix_and_document_contain_no_raw_legacy_payload() -> None:
    matrix = _load_matrix()
    artifacts = [
        json.dumps(matrix, ensure_ascii=False),
        DOC_PATH.read_text(encoding="utf-8"),
    ]
    forbidden_markers = (
        "api_key",
        "authorization",
        "bearer",
        "password",
        "token",
        "secret",
        "dsn",
        "credential",
        "bot_message\":\"",
    )
    forbidden_keys = {
        "text",
        "text_raw",
        "text_clean",
        "template",
        "url",
        "links",
        "phones",
        "dates_mentioned",
        "credentials",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert not (set(value) & forbidden_keys)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(matrix)
    for artifact in artifacts:
        lowered = artifact.casefold()
        assert not re.search(r"https?://|www\.", lowered)
        assert not re.search(
            r"\b(?:19|20)\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?\b",
            artifact,
        )
        assert all(marker not in lowered for marker in forbidden_markers)
