from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class RegressionCheckError(ValueError):
    """Raised when regression metrics cannot prove that the gate passed."""


def check_regression(
    metrics: Any,
    *,
    threshold: float = 0.85,
    min_cases: int = 1,
    allow_generated_smoke: bool = False,
) -> dict[str, float | int | str | bool]:
    if not 0 <= threshold <= 1:
        raise RegressionCheckError("threshold must be between 0 and 1")
    if min_cases < 1:
        raise RegressionCheckError("min_cases must be positive")
    if not isinstance(metrics, dict):
        raise RegressionCheckError("metrics file must contain a JSON object")
    if metrics.get("generated_smoke_cases") and not allow_generated_smoke:
        raise RegressionCheckError(
            "generated seed smoke cases are structural checks, not an independent regression set"
        )

    cases_scored = metrics.get("cases_scored")
    if isinstance(cases_scored, bool) or not isinstance(cases_scored, int):
        raise RegressionCheckError("metrics must contain integer cases_scored")
    if cases_scored < min_cases:
        raise RegressionCheckError(
            f"insufficient scored cases: {cases_scored} < required {min_cases}"
        )

    recall = metrics.get("recall_at_5")
    if isinstance(recall, bool) or not isinstance(recall, (int, float)):
        raise RegressionCheckError("metrics must contain numeric recall_at_5")
    recall_value = float(recall)
    if not 0 <= recall_value <= 1:
        raise RegressionCheckError("recall_at_5 must be between 0 and 1")
    if recall_value < threshold:
        raise RegressionCheckError(
            f"Recall@5 regression: {recall_value:.3f} < {threshold:.3f}"
        )

    return {
        "status": "pass",
        "backend": str(metrics.get("backend") or "unknown"),
        "cases_scored": cases_scored,
        "recall_at_5": recall_value,
        "threshold": threshold,
        "generated_smoke_cases": bool(metrics.get("generated_smoke_cases")),
    }


def load_metrics(path: Path) -> Any:
    if not path.is_file():
        raise RegressionCheckError(f"metrics file is absent: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegressionCheckError(f"cannot read metrics file {path}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="eval/metrics.json")
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--min-cases", type=int, default=1)
    parser.add_argument("--allow-generated-smoke", action="store_true")
    args = parser.parse_args()

    path = Path(args.metrics)
    try:
        result = check_regression(
            load_metrics(path),
            threshold=args.threshold,
            min_cases=args.min_cases,
            allow_generated_smoke=args.allow_generated_smoke,
        )
    except RegressionCheckError as exc:
        raise SystemExit(f"Regression check failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
