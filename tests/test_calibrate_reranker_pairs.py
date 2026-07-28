from __future__ import annotations

from pathlib import Path

import pytest

from scripts.calibrate_reranker_pairs import (
    analyze_scored_pairs,
    calibrate_pairs,
    calibration_quality_warnings,
    positive_rank,
    rank_histogram,
    read_jsonl_lines,
    recommend_threshold,
    score_margin,
    score_pair,
    threshold_row,
)


class FakeScorer:
    def score(self, query: str, texts: list[str]) -> list[float]:
        return [0.8, 0.2, 0.1]


def test_positive_rank_and_margin() -> None:
    assert positive_rank(0.7, [0.2, 0.8, 0.1]) == 2
    assert score_margin(0.7, [0.2, 0.8, 0.1]) == -0.1


@pytest.mark.asyncio
async def test_score_pair_uses_scorer_order() -> None:
    result = await score_pair(
        {
            "query": "Как подать заявку?",
            "positive_text": "Правильный ответ",
            "hard_negative_texts": ["Неверный ответ", "Другой ответ"],
            "category": "форумы",
        },
        FakeScorer(),
    )

    assert result["positive_score"] == 0.8
    assert result["negative_scores"] == [0.2, 0.1]
    assert result["positive_rank"] == 1
    assert result["margin"] == 0.6


def test_threshold_row_counts_retention_and_rejection() -> None:
    row = threshold_row(
        0.3,
        positive_scores=[0.8, 0.2],
        negative_scores=[0.1, 0.4],
    )

    assert row == {
        "threshold": 0.3,
        "positive_retention_rate": 0.5,
        "negative_rejection_rate": 0.5,
        "precision_if_answered": 0.5,
    }


def test_recommend_threshold_prefers_negative_rejection_with_target_retention() -> None:
    rows = [
        {
            "threshold": 0.1,
            "positive_retention_rate": 1.0,
            "negative_rejection_rate": 0.2,
            "precision_if_answered": 0.5,
        },
        {
            "threshold": 0.3,
            "positive_retention_rate": 0.9,
            "negative_rejection_rate": 0.8,
            "precision_if_answered": 0.9,
        },
        {
            "threshold": 0.5,
            "positive_retention_rate": 0.6,
            "negative_rejection_rate": 1.0,
            "precision_if_answered": 1.0,
        },
    ]

    assert recommend_threshold(rows, 0.9) == 0.3


def test_analyze_scored_pairs_builds_summary() -> None:
    report = analyze_scored_pairs(
        [
            {
                "query": "q1",
                "positive_score": 0.8,
                "negative_scores": [0.2, 0.1],
                "positive_rank": 1,
                "margin": 0.6,
            },
            {
                "query": "q2",
                "positive_score": 0.05,
                "negative_scores": [0.1],
                "positive_rank": 2,
                "margin": -0.05,
            },
        ],
        target_positive_retention=0.5,
    )

    assert report["pairs_total"] == 2
    assert report["positive_at_1_rate"] == 0.5
    assert report["positive_scores"]["min"] == 0.05
    assert report["negative_scores"]["max"] == 0.2
    assert report["positive_rank_histogram"] == {"1": 1, "2": 1}
    assert report["negative_beats_positive_rate"] == 0.5
    assert report["margin_summary"]["min"] == -0.05
    assert report["recommended_threshold"] is not None
    assert "many_hard_negatives_beat_positive" in report["quality_warnings"]


def test_rank_histogram_sorts_numeric_ranks() -> None:
    assert rank_histogram([10, 2, 1, 2]) == {"1": 1, "2": 2, "10": 1}


def test_calibration_quality_warnings_flag_unsafe_threshold() -> None:
    warnings = calibration_quality_warnings(
        {
            "positive_at_1_rate": 0.2,
            "negative_beats_positive_rate": 0.8,
            "recommended_threshold": 0.3,
            "threshold_candidates": [
                {
                    "threshold": 0.3,
                    "precision_if_answered": 0.25,
                }
            ],
        }
    )

    assert warnings == [
        "low_positive_at_1_rate_review_pairs_or_reranker",
        "many_hard_negatives_beat_positive",
        "recommended_threshold_has_low_precision",
    ]


def test_read_jsonl_lines_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        read_jsonl_lines(['["not-object"]'])


@pytest.mark.asyncio
async def test_calibrate_pairs_can_skip_output_files(tmp_path) -> None:
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text(
        (
            '{"query":"q","positive_text":"p",'
            '"hard_negative_texts":["n1","n2"],"category":"forums"}\n'
        ),
        encoding="utf-8",
    )

    report = await calibrate_pairs(pairs, Path("-"), scorer=FakeScorer())

    assert report["pairs_total"] == 1
    assert not (tmp_path / "-").exists()


@pytest.mark.asyncio
async def test_calibrate_pairs_rejects_deprecated_operator_copy(tmp_path) -> None:
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text(
        (
            '{"query":"q","positive_text":"operator copy",'
            '"hard_negative_texts":["n"],"deprecated_for_product_eval":true}\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="deprecated operator copy"):
        await calibrate_pairs(pairs, Path("-"), scorer=FakeScorer())
