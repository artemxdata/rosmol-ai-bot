from __future__ import annotations

import csv
import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest

import scripts.build_ticket_product_review as review_builder
from scripts.build_ticket_product_review import build_review_exports

SUMMARY_FIELDS = (
    "rank",
    "intent",
    "aspect",
    "entity_class",
    "cases",
    "share",
    "unique_clusters",
    "largest_cluster",
    "channels_count",
    "answer_count",
    "clarify_count",
    "escalate_count",
    "time_sensitive_count",
    "review_quota",
    "relabel_risk",
)

MANIFEST_FIELDS = (
    "case_id_hash",
    "duplicate_cluster_id",
    "source_schema_version",
    "source_case_fingerprint",
    "stratum_rank",
    "intent",
    "aspect",
    "entity_class",
    "channel",
    "time_bucket",
    "expected_route",
    "expected_escalation_reason",
    "time_sensitive",
    "difficulty",
    "role_reconstruction_status",
    "multiturn_status",
    "reviewer",
    "reviewed_at",
    "role_verdict",
    "label_verdict",
    "corrected_intent",
    "corrected_aspect",
    "corrected_entity_class",
    "answerable_from_snapshot",
    "approved_chunk_ids",
    "approved_kb_seed_sha256",
    "forbidden_profiles",
    "include_in_calibration",
)

STATS_FIELDS = {
    "input_cases",
    "strata_total",
    "top_strata",
    "selected_cases",
    "unique_clusters",
    "summary_rows",
    "manifest_rows",
}


@pytest.fixture(autouse=True)
def _use_test_private_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_builder, "PRIVATE_DATA_ROOT", tmp_path)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _case(
    label: str,
    *,
    category: str = "форумы",
    topic: str = "прочее",
    aspect: str = "generic",
    entity: str = "",
    cluster: str | None = None,
    route: str = "answer",
    time_sensitive: bool = False,
    split: str = "calibration",
    operator_answer_included: bool = False,
    operator_answer_used_as_fact: bool = False,
) -> dict[str, object]:
    ticket_id_hash = _digest(f"ticket:{label}")
    return {
        "schema_version": "1.0.0",
        "id": f"ticket::{ticket_id_hash}",
        "ticket_id_hash": ticket_id_hash,
        "query": f"СЕКРЕТНЫЙ_ВОПРОС_{label}: Иван, +7 999 000-00-00",
        "expected_answer": f"СЕКРЕТНЫЙ_ОТВЕТ_{label}",
        "answer_candidate": f"ОПЕРАТОРСКИЙ_ТЕКСТ_{label}",
        "messages_masked": f"ПРИВАТНЫЙ_ДИАЛОГ_{label}",
        "raw_email": f"{label}@example.test",
        "first_timestamp": "2026-01-10T10:00:00",
        "available_at": "2026-01-10T11:00:00",
        "channel": "api",
        "category": category,
        "topic": topic,
        "entity": entity,
        "expected_response_profile": aspect,
        "expected_route": route,
        "expected_escalation_reason": (
            "missing_confirmed_data" if route == "escalate" else None
        ),
        "needs_clarification": route == "clarify",
        "needs_escalation": route == "escalate",
        "time_sensitive": time_sensitive,
        "difficulty": "medium",
        "answerable_from_snapshot": None,
        "approved_chunk_ids": [],
        "forbidden_response_profiles": [],
        "duplicate_cluster_id": _digest(f"cluster:{cluster or label}"),
        "split": split,
        "label_status": "weak_unreviewed",
        "label_provenance": "deterministic_query_only_v2",
        "requires_human_review": True,
        "operator_answer_included": operator_answer_included,
        "operator_answer_used_as_fact": operator_answer_used_as_fact,
    }


def _write_jsonl(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
            for case in cases
        ),
        encoding="utf-8",
    )


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader.fieldnames or []), list(reader)


def _build(
    tmp_path: Path,
    cases: list[dict[str, object]],
    *,
    prefix: str = "review",
    top_n: int = 20,
    min_per_stratum: int = 10,
    total: int = 300,
) -> tuple[dict[str, int], Path, Path]:
    input_path = tmp_path / f"{prefix}.jsonl"
    summary_path = tmp_path / f"{prefix}_summary.csv"
    manifest_path = tmp_path / f"{prefix}_manifest.csv"
    _write_jsonl(input_path, cases)
    stats = build_review_exports(
        input_path,
        summary_path,
        manifest_path,
        top_n=top_n,
        min_per_stratum=min_per_stratum,
        total=total,
    )
    return stats, summary_path, manifest_path


def test_build_review_exports_defaults_are_product_review_contract() -> None:
    parameters = inspect.signature(build_review_exports).parameters

    assert parameters["top_n"].default == 20
    assert parameters["min_per_stratum"].default == 10
    assert parameters["total"].default == 300
    assert parameters["overwrite"].default is False


def test_build_review_exports_writes_top20_metadata_only(tmp_path: Path) -> None:
    cases: list[dict[str, object]] = []
    for rank in range(21):
        frequency = 21 - rank
        for occurrence in range(frequency):
            cases.append(
                _case(
                    f"stratum-{rank:02d}-{occurrence:02d}",
                    topic=f"topic_{rank:02d}",
                    entity="Машук" if rank == 0 else "",
                )
            )

    stats, summary_path, manifest_path = _build(
        tmp_path,
        cases,
        min_per_stratum=1,
        total=20,
    )
    summary_fields, summary = _read_csv(summary_path)
    manifest_fields, manifest = _read_csv(manifest_path)

    assert set(stats) == STATS_FIELDS
    assert stats == {
        "input_cases": 231,
        "strata_total": 21,
        "top_strata": 20,
        "selected_cases": 20,
        "unique_clusters": 231,
        "summary_rows": 20,
        "manifest_rows": 20,
    }
    assert summary_fields == list(SUMMARY_FIELDS)
    assert manifest_fields == list(MANIFEST_FIELDS)
    assert len(summary) == 20
    assert len(manifest) == 20
    assert summary[0]["intent"] == "форумы.topic_00"
    assert summary[0]["aspect"] == "generic"
    assert summary[0]["entity_class"] == "forum:named"
    assert {row["intent"] for row in summary} == {
        f"форумы.topic_{rank:02d}" for rank in range(20)
    }
    assert "форумы.topic_20" not in {row["intent"] for row in summary}
    assert len({row["duplicate_cluster_id"] for row in manifest}) == len(manifest)
    assert all(
        re.fullmatch(r"[0-9a-f]{12,64}", row["case_id_hash"])
        for row in manifest
    )
    assert {
        "query",
        "expected_answer",
        "answer_candidate",
        "messages_masked",
        "raw_email",
        "entity",
    }.isdisjoint(summary_fields + manifest_fields)

    exported = summary_path.read_text(encoding="utf-8-sig")
    exported += manifest_path.read_text(encoding="utf-8-sig")
    for forbidden in (
        "СЕКРЕТНЫЙ_ВОПРОС_",
        "СЕКРЕТНЫЙ_ОТВЕТ_",
        "ОПЕРАТОРСКИЙ_ТЕКСТ_",
        "ПРИВАТНЫЙ_ДИАЛОГ_",
        "Иван",
        "+7 999 000-00-00",
        "@example.test",
        "Машук",
    ):
        assert forbidden not in exported


def test_build_review_exports_selects_one_case_per_duplicate_cluster(
    tmp_path: Path,
) -> None:
    cases = [
        _case("same-cluster-a", cluster="shared"),
        _case("same-cluster-b", cluster="shared"),
        _case("other-cluster"),
    ]

    stats, summary_path, manifest_path = _build(
        tmp_path,
        cases,
        min_per_stratum=1,
        total=2,
    )
    _, summary = _read_csv(summary_path)
    _, manifest = _read_csv(manifest_path)

    assert len(summary) == 1
    assert summary[0]["cases"] == "3"
    assert summary[0]["unique_clusters"] == "2"
    assert summary[0]["largest_cluster"] == "2"
    assert summary[0]["review_quota"] == "2"
    assert len(manifest) == 2
    assert len({row["duplicate_cluster_id"] for row in manifest}) == 2
    assert stats["selected_cases"] == 2
    assert stats["unique_clusters"] == 2


def test_build_review_exports_deduplicates_cluster_across_strata(
    tmp_path: Path,
) -> None:
    cases = [
        _case(
            "forum-shared",
            category="форумы",
            cluster="shared",
            route="escalate",
        ),
        _case("forum-unique", category="форумы"),
        _case(
            "grant-shared",
            category="гранты",
            aspect="grants",
            cluster="shared",
            route="escalate",
        ),
        _case("grant-unique", category="гранты", aspect="grants"),
    ]

    stats, _, manifest_path = _build(
        tmp_path,
        cases,
        top_n=2,
        min_per_stratum=1,
        total=3,
    )
    _, manifest = _read_csv(manifest_path)

    assert stats["selected_cases"] == 3
    assert len({row["duplicate_cluster_id"] for row in manifest}) == 3


def test_minimum_selection_uses_matching_when_greedy_would_starve(
    tmp_path: Path,
) -> None:
    cases = [
        _case("s1-a", topic="s1", cluster="a"),
        _case("s1-b", topic="s1", cluster="b"),
        _case("s2-a", topic="s2", cluster="a"),
        _case("s2-c", topic="s2", cluster="c"),
        _case("s3-a", topic="s3", cluster="a"),
        _case("s3-c", topic="s3", cluster="c"),
    ]

    stats, _, manifest_path = _build(
        tmp_path,
        cases,
        top_n=3,
        min_per_stratum=1,
        total=3,
    )
    _, manifest = _read_csv(manifest_path)

    assert stats["selected_cases"] == 3
    assert len({row["duplicate_cluster_id"] for row in manifest}) == 3
    assert {row["intent"].rsplit(".", maxsplit=1)[-1] for row in manifest} == {
        "s1",
        "s2",
        "s3",
    }


def test_weighted_top_up_is_deterministic_and_prioritizes_frequency_and_risk(
    tmp_path: Path,
) -> None:
    cases = [
        *[
            _case(
                f"risk-{index}",
                category="другое",
                topic="прочее",
                aspect="generic",
                route="escalate",
                time_sensitive=True,
            )
            for index in range(4)
        ],
        *[
            _case(
                f"frequency-{index}",
                category="форумы",
                topic="регистрация_и_заявка",
                aspect="application",
                entity="Машук",
            )
            for index in range(8)
        ],
        *[
            _case(
                f"baseline-{index}",
                category="навигация",
                topic="письмо_и_уведомления",
                aspect="documents",
            )
            for index in range(3)
        ],
    ]

    _, summary_a_path, manifest_a_path = _build(
        tmp_path,
        cases,
        prefix="forward",
        top_n=3,
        min_per_stratum=1,
        total=7,
    )
    _, summary_b_path, manifest_b_path = _build(
        tmp_path,
        list(reversed(cases)),
        prefix="reversed",
        top_n=3,
        min_per_stratum=1,
        total=7,
    )

    assert summary_a_path.read_bytes() == summary_b_path.read_bytes()
    assert manifest_a_path.read_bytes() == manifest_b_path.read_bytes()

    _, summary = _read_csv(summary_a_path)
    quotas = {row["intent"]: int(row["review_quota"]) for row in summary}
    assert sum(quotas.values()) == 7
    assert quotas["форумы.регистрация_и_заявка"] > quotas[
        "навигация.письмо_и_уведомления"
    ]
    assert quotas["другое.прочее"] > quotas[
        "навигация.письмо_и_уведомления"
    ]


def test_build_review_exports_rejects_non_calibration_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="calibration"):
        _build(
            tmp_path,
            [
                _case("calibration"),
                _case("holdout", split="holdout"),
            ],
        )


@pytest.mark.parametrize(
    "unsafe_field",
    [
        "operator_answer_included",
        "operator_answer_used_as_fact",
    ],
)
def test_build_review_exports_rejects_operator_answer_leakage(
    tmp_path: Path,
    unsafe_field: str,
) -> None:
    case = _case("unsafe")
    case[unsafe_field] = True

    with pytest.raises(ValueError, match="operator-answer"):
        _build(tmp_path, [case])


def test_build_review_exports_preserves_existing_review_files(
    tmp_path: Path,
) -> None:
    _, summary_path, manifest_path = _build(
        tmp_path,
        [_case("existing")],
        total=1,
        min_per_stratum=1,
    )
    summary_before = summary_path.read_bytes()
    manifest_before = manifest_path.read_bytes()
    input_path = tmp_path / "review.jsonl"

    with pytest.raises(ValueError, match="explicit overwrite"):
        build_review_exports(
            input_path,
            summary_path,
            manifest_path,
            total=1,
            min_per_stratum=1,
        )

    assert summary_path.read_bytes() == summary_before
    assert manifest_path.read_bytes() == manifest_before


def test_build_review_exports_rejects_paths_outside_project_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(review_builder, "PRIVATE_DATA_ROOT", private_root)
    input_path = tmp_path / "public_cases.jsonl"
    _write_jsonl(input_path, [_case("public")])

    with pytest.raises(ValueError, match="must stay under"):
        build_review_exports(
            input_path,
            tmp_path / "summary.csv",
            tmp_path / "manifest.csv",
            total=1,
            min_per_stratum=1,
        )
