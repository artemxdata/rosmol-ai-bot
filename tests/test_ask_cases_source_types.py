from __future__ import annotations

from eval.ask_cases import build_seed_ask_cases, summarize_cases


def _record(chunk_id: str, source_type: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "status": "published",
        "category": "forums",
        "source_type": source_type,
        "intent_examples": [f"question {chunk_id}"],
        "text_clean": f"Answer {chunk_id}",
    }


def test_summarize_cases_counts_source_types() -> None:
    cases = build_seed_ask_cases(
        [
            _record("ticket_1", "ticket_answer_bank"),
            _record("xlsx_1", "xlsx"),
        ]
    )

    summary = summarize_cases(cases)

    assert summary["source_type_counts"] == {"ticket_answer_bank": 1, "xlsx": 1}
