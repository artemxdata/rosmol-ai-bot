from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.clarify import clarify
from src.graph.nodes.escalate import escalate
from src.graph.nodes.generate import generate
from src.graph.nodes.rerank import rerank
from src.graph.nodes.respond import respond
from src.graph.nodes.retrieve import retrieve
from src.graph.nodes.verify import verify
from src.graph.state import BotState


def build_graph():
    graph = StateGraph(BotState)
    graph.add_node("analyze", analyze_query)
    graph.add_node("retrieve", retrieve)
    graph.add_node("rerank", rerank)
    graph.add_node("generate", generate)
    graph.add_node("verify", verify)
    graph.add_node("respond", respond)
    graph.add_node("clarify", clarify)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "analyze")
    graph.add_conditional_edges(
        "analyze",
        route_after_analyze,
        {"clarify": "clarify", "retrieve": "retrieve", "escalate": "escalate"},
    )
    graph.add_edge("retrieve", "rerank")
    graph.add_conditional_edges(
        "rerank",
        route_after_rerank,
        {"generate": "generate", "escalate": "escalate"},
    )
    graph.add_edge("generate", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"respond": "respond", "escalate": "escalate"},
    )
    graph.add_edge("respond", END)
    graph.add_edge("clarify", END)
    graph.add_edge("escalate", END)
    return graph.compile()
