from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)
DEFAULT_CASES_PATH = Path(
    "eval/cases/product_calibration_synthetic_pilot_20.json"
)
DEFAULT_PRIVATE_CORPUS_PATH = Path(
    "data/private/tickets/product_baseline_20260729_roles_v1/"
    "product_calibration_cases.jsonl"
)


@dataclass(frozen=True, slots=True)
class PrivacyOverlap:
    case_id: str
    kind: str


def normalize_for_overlap(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.casefold().replace("ё", "е")
    return " ".join(TOKEN_RE.findall(normalized))


def token_ngrams(tokens: tuple[str, ...], size: int = 5) -> frozenset[tuple[str, ...]]:
    if size < 1:
        raise ValueError("ngram size must be positive")
    return frozenset(
        tuple(tokens[index : index + size])
        for index in range(len(tokens) - size + 1)
    )


def longest_common_token_run(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> int:
    previous = [0] * (len(right) + 1)
    best = 0
    for left_token in left:
        current = [0] * (len(right) + 1)
        for index, right_token in enumerate(right, start=1):
            if left_token != right_token:
                continue
            current[index] = previous[index - 1] + 1
            best = max(best, current[index])
        previous = current
    return best


def find_private_overlaps(
    synthetic_cases: Iterable[dict[str, Any]],
    private_queries: Iterable[str],
    *,
    min_common_run: int = 8,
    ngram_size: int = 5,
    min_ngram_jaccard: float = 0.8,
) -> list[PrivacyOverlap]:
    prepared_private: list[
        tuple[str, tuple[str, ...], frozenset[tuple[str, ...]]]
    ] = []
    for query in private_queries:
        normalized = normalize_for_overlap(query)
        if not normalized:
            continue
        tokens = tuple(normalized.split())
        prepared_private.append(
            (normalized, tokens, token_ngrams(tokens, ngram_size))
        )

    violations: set[PrivacyOverlap] = set()
    for case in synthetic_cases:
        case_id = str(case.get("id") or "")
        normalized = normalize_for_overlap(str(case.get("query") or ""))
        tokens = tuple(normalized.split())
        ngrams = token_ngrams(tokens, ngram_size)
        for private_normalized, private_tokens, private_ngrams in prepared_private:
            if normalized == private_normalized:
                violations.add(PrivacyOverlap(case_id, "exact_normalized_match"))
                break
            if (
                min_common_run > 0
                and longest_common_token_run(tokens, private_tokens)
                >= min_common_run
            ):
                violations.add(PrivacyOverlap(case_id, "long_common_token_run"))
                break
            if not ngrams or not private_ngrams:
                continue
            union = ngrams | private_ngrams
            jaccard = len(ngrams & private_ngrams) / len(union)
            if jaccard >= min_ngram_jaccard:
                violations.add(PrivacyOverlap(case_id, "high_ngram_similarity"))
                break
    return sorted(violations, key=lambda item: (item.case_id, item.kind))


def load_synthetic_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise ValueError(f"Synthetic cases must be a JSON object array: {path}")
    return payload


def iter_private_queries(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid private JSONL at line {line_number}: {path}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Private JSONL line {line_number} is not an object: {path}"
                )
            query = str(record.get("query") or "").strip()
            if query:
                yield query


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fail if a tracked synthetic pilot query is an exact or near copy "
            "of a private corpus query. Private text is never printed."
        )
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--private-corpus",
        type=Path,
        default=DEFAULT_PRIVATE_CORPUS_PATH,
    )
    args = parser.parse_args()

    cases = load_synthetic_cases(args.cases)
    private_queries = list(iter_private_queries(args.private_corpus))
    violations = find_private_overlaps(cases, private_queries)
    print(
        json.dumps(
            {
                "synthetic_cases": len(cases),
                "private_queries": len(private_queries),
                "violations": [
                    {"case_id": item.case_id, "kind": item.kind}
                    for item in violations
                ],
                "passed": not violations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
