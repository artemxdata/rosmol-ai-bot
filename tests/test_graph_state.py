from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from src.graph.state import BotState


def test_bot_state_preserves_delivery_and_eval_trace_identifiers() -> None:
    graph = StateGraph(BotState)
    graph.add_node("passthrough", lambda state: {})
    graph.add_edge(START, "passthrough")
    graph.add_edge("passthrough", END)

    result = graph.compile().invoke(
        {
            "request_id": uuid4(),
            "upstream_event_id": "event-1",
            "upstream_event_id_source": "message.id",
            "eval_run_id": "run-1",
            "eval_case_id": "case-1",
        }
    )

    assert result["upstream_event_id"] == "event-1"
    assert result["upstream_event_id_source"] == "message.id"
    assert result["eval_run_id"] == "run-1"
    assert result["eval_case_id"] == "case-1"
