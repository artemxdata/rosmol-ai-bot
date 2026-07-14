from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.check_regression import (
    RegressionCheckError,
    check_regression,
    load_metrics,
)


def _metrics(**overrides: object) -> dict[str, object]:
    metrics: dict[str, object] = {
        "backend": "lexical",
        "cases_scored": 11,
        "recall_at_5": 0.91,
        "generated_smoke_cases": False,
    }
    metrics.update(overrides)
    return metrics


def test_check_regression_accepts_reproducible_scored_metrics() -> None:
    result = check_regression(_metrics(), threshold=0.85, min_cases=10)

    assert result["status"] == "pass"
    assert result["cases_scored"] == 11
    assert result["recall_at_5"] == 0.91


@pytest.mark.parametrize(
    ("metrics", "message"),
    [
        ({"cases_scored": 0, "recall_at_5": None}, "insufficient scored cases"),
        ({"cases_scored": 11}, "numeric recall_at_5"),
        (_metrics(recall_at_5=0.5), "Recall@5 regression"),
        (_metrics(generated_smoke_cases=True), "structural checks"),
    ],
)
def test_check_regression_fails_closed(metrics: object, message: str) -> None:
    with pytest.raises(RegressionCheckError, match=message):
        check_regression(metrics, threshold=0.85, min_cases=10)


def test_check_regression_allows_generated_smoke_only_when_explicit() -> None:
    result = check_regression(
        _metrics(generated_smoke_cases=True),
        threshold=0.85,
        min_cases=10,
        allow_generated_smoke=True,
    )

    assert result["generated_smoke_cases"] is True


def test_load_metrics_rejects_missing_or_invalid_file(tmp_path: Path) -> None:
    with pytest.raises(RegressionCheckError, match="metrics file is absent"):
        load_metrics(tmp_path / "missing.json")

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    with pytest.raises(RegressionCheckError, match="cannot read metrics file"):
        load_metrics(invalid)


def test_load_metrics_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(_metrics()), encoding="utf-8")

    assert load_metrics(path) == _metrics()
