from __future__ import annotations

from scripts.audit_synthetic_case_privacy import (
    PrivacyOverlap,
    find_private_overlaps,
    longest_common_token_run,
    normalize_for_overlap,
    token_ngrams,
)


def test_normalize_for_overlap_is_unicode_and_punctuation_stable() -> None:
    assert normalize_for_overlap("  Ёжик — ФОРУМ!  ") == "ежик форум"


def test_longest_common_token_run_is_contiguous() -> None:
    assert longest_common_token_run(
        ("один", "два", "три", "четыре"),
        ("ноль", "два", "три", "пять"),
    ) == 2


def test_token_ngrams_requires_positive_size() -> None:
    try:
        token_ngrams(("один",), 0)
    except ValueError as exc:
        assert str(exc) == "ngram size must be positive"
    else:
        raise AssertionError("zero ngram size must be rejected")


def test_find_private_overlaps_reports_ids_without_private_text() -> None:
    cases = [
        {
            "id": "synthetic_exact",
            "query": "Когда проходит форум?",
        },
        {
            "id": "synthetic_distinct",
            "query": "Что умеет бот?",
        },
    ]

    overlaps = find_private_overlaps(
        cases,
        [
            "Когда проходит форум!",
            "Совершенно другой приватный вопрос",
        ],
    )

    assert overlaps == [
        PrivacyOverlap(
            case_id="synthetic_exact",
            kind="exact_normalized_match",
        )
    ]
    assert "приватный" not in repr(overlaps)


def test_find_private_overlaps_detects_long_run_and_ngram_similarity() -> None:
    cases = [
        {
            "id": "synthetic_long_run",
            "query": "раз два три четыре пять шесть семь восемь отдельно",
        },
        {
            "id": "synthetic_ngram",
            "query": "альфа бета гамма дельта эпсилон дзета",
        },
    ]

    overlaps = find_private_overlaps(
        cases,
        [
            "начало раз два три четыре пять шесть семь восемь конец",
            "альфа бета гамма дельта эпсилон дзета финал",
        ],
        min_ngram_jaccard=0.6,
    )

    assert overlaps == [
        PrivacyOverlap(
            case_id="synthetic_long_run",
            kind="long_common_token_run",
        ),
        PrivacyOverlap(
            case_id="synthetic_ngram",
            kind="high_ngram_similarity",
        ),
    ]
