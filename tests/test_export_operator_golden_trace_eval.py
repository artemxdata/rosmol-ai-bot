from __future__ import annotations

from scripts.export_operator_golden_trace_eval import observed_behavior


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
