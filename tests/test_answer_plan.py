from __future__ import annotations

import pytest

import src.graph.answer_plan as answer_plan_module
from src.graph.answer_plan import answer_plan_for_state, plan_answer
from src.graph.graph import build_graph
from src.models import QueryAnalysis


def test_plan_node_freezes_one_complete_contract_for_the_rag_graph() -> None:
    query = (
        "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
        "и что значит статус «Одобрена»?"
    )
    state = {
        "message": query,
        "message_masked": query,
        "analysis": QueryAnalysis(category="платформа_фгаис"),
    }

    result = plan_answer(state)
    plan = result["answer_plan"]

    assert result["answer_plan_message"] == query
    assert plan.incomplete is False
    assert len(plan.clauses) == 3
    assert tuple(question.topic for question in plan.questions) == (
        "registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
        "poisk_i_navigaciya_po_meropriyatiyam",
        "statusy_zayavok",
    )


def test_downstream_nodes_reuse_the_frozen_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "Что такое гранты для физических лиц?"
    state = {
        "message": query,
        "message_masked": query,
        "analysis": QueryAnalysis(category="гранты"),
    }
    state.update(plan_answer(state))
    frozen = state["answer_plan"]

    def fail_rebuild(*_args, **_kwargs):
        raise AssertionError("the frozen plan must be reused")

    monkeypatch.setattr(
        answer_plan_module,
        "build_query_proven_topic_plan",
        fail_rebuild,
    )

    assert answer_plan_for_state(state) is frozen


def test_grounded_graph_has_an_explicit_plan_stage() -> None:
    graph = build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert "plan" in graph.nodes
    assert ("analyze", "plan") in edges
    assert ("plan", "retrieve") in edges
    assert ("analyze", "retrieve") not in edges
