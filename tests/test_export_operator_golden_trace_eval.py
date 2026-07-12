from __future__ import annotations

from scripts.export_operator_golden_trace_eval import (
    expected_trace_user_hash,
    observed_behavior,
)
from src.session.memory import hash_user_id


def test_observed_behavior_detects_routes() -> None:
    assert observed_behavior("Передаю обращение специалисту.", was_escalated=True) == "escalate"
    assert (
        observed_behavior(
            "Я отвечаю на вопросы по мероприятиям. Задай, пожалуйста, вопрос по этим темам.",
            was_escalated=False,
        )
        == "scope_note"
    )
    assert (
        observed_behavior("Уточни, пожалуйста, название форума.", was_escalated=False)
        == "clarify"
    )
    assert observed_behavior("Заявку можно подать в профиле.", was_escalated=False) == "answer"


def test_expected_trace_user_hash_matches_eval_user_prefix() -> None:
    assert expected_trace_user_hash("full-tail", 17) == hash_user_id(
        "api",
        "full-tail-17",
    )
