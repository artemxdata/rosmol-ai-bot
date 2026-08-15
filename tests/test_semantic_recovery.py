from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.edges import (
    route_after_generate,
    route_after_rerank,
    route_after_semantic_recovery,
    route_after_verify,
)
from src.graph.nodes.rerank import rerank
from src.graph.nodes.retrieve import retrieve
from src.graph.nodes.semantic_recovery import semantic_recovery
from src.graph.question_utils import QueryProvenTopicPlan
from src.models import (
    Chunk,
    Complexity,
    QueryAnalysis,
    Question,
    ScoredChunk,
    VerificationResult,
)


class CapturingRecoveryLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def generate(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.response


class FailingRecoveryLLM:
    async def generate(self, **_kwargs: object) -> str:
        raise RuntimeError("model unavailable")


class RecoveryAwareRetriever:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def retrieve_by_metadata(
        self,
        filters: dict[str, object],
        top_k: int,
    ) -> list[Chunk]:
        return []

    async def retrieve(
        self,
        query: str,
        filters: dict[str, object],
        top_k: int,
    ) -> list[Chunk]:
        self.queries.append(query)
        return self._result(query)

    async def retrieve_keyword_candidates(
        self,
        query: str,
        filters: dict[str, object],
        *,
        top_k: int,
        **_kwargs: object,
    ) -> list[Chunk]:
        self.queries.append(query)
        return self._result(query)

    @staticmethod
    def _result(query: str) -> list[Chunk]:
        if "финансирует проезд" not in query.casefold():
            return []
        return [
            Chunk(
                chunk_id="published_travel_fact",
                text="Организаторы оплачивают проезд участника до места проведения форума.",
                metadata={
                    "source_type": "yonote",
                    "source": "yonote_api",
                    "version": "yonote-api-v1",
                    "status": "published",
                    "category": "форумы",
                    "forum_normalized": "Машук",
                    "topic": "oplata_proezda",
                },
                score=0.95,
            )
        ]


class HighConfidenceReranker:
    def rerank(
        self,
        query: str,
        chunks: list[Chunk],
        top_k: int,
    ) -> list[ScoredChunk]:
        return [
            ScoredChunk(
                **chunk.model_dump(),
                reranker_score=0.95,
            )
            for chunk in chunks[:top_k]
        ]


def _analysis() -> QueryAnalysis:
    return QueryAnalysis(
        category="форумы",
        forum_normalized="Машук",
        complexity=Complexity.COMPLEX,
        questions=[
            Question(
                text="Как участвовать?",
                topic="legacy_topic",
                category="форумы",
                forum_normalized="Машук",
            )
        ],
    )


def test_low_confidence_routes_to_semantic_recovery_only_once() -> None:
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "max_confidence": 0.2,
        "should_escalate": True,
        "escalation_reason": "low_confidence",
    }

    assert route_after_rerank(state) == "recover"
    assert route_after_rerank({**state, "semantic_recovery_attempted": True}) == "escalate"
    assert route_after_rerank({**state, "escalation_reason": "rerank_failed"}) == "escalate"


def test_successful_generation_does_not_enter_recovery() -> None:
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "max_confidence": 0.9,
        "should_escalate": False,
    }

    assert route_after_rerank(state) == "generate"
    assert route_after_generate(state) == "guard"


def test_source_coverage_failure_can_recover_but_llm_failure_cannot() -> None:
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "should_escalate": True,
        "escalation_reason": "insufficient_sources",
    }

    assert route_after_generate(state) == "recover"
    assert route_after_generate({**state, "escalation_reason": "llm_generation_failed"}) == (
        "escalate"
    )


@pytest.mark.parametrize(
    "reason",
    ["insufficient_sources", "partial_source_coverage"],
)
def test_late_source_coverage_failure_can_recover_once(reason: str) -> None:
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "should_escalate": True,
        "escalation_reason": reason,
    }

    assert route_after_verify(state) == "recover"
    assert route_after_verify({**state, "semantic_recovery_attempted": True}) == (
        "escalate"
    )


def test_verifier_contract_failure_does_not_retry_retrieval() -> None:
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "should_escalate": True,
        "escalation_reason": "missing_source_citations",
    }

    assert route_after_verify(state) == "escalate"


def test_verifier_hallucination_fails_closed_even_with_recoverable_reason() -> None:
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "should_escalate": True,
        "escalation_reason": "insufficient_sources",
        "verification": VerificationResult(has_hallucination=True),
    }

    assert route_after_verify(state) == "escalate"


def test_semantic_recovery_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.graph.edges.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            semantic_recovery_enabled=False,
        ),
    )
    state = {
        "analysis": _analysis(),
        "llm_client": object(),
        "max_confidence": 0.1,
        "should_escalate": True,
        "escalation_reason": "low_confidence",
    }

    assert route_after_rerank(state) == "escalate"


@pytest.mark.asyncio
async def test_semantic_recovery_builds_grounded_search_questions() -> None:
    llm = CapturingRecoveryLLM(
        '{"questions":['
        '{"text":"Какие условия участия в форуме Машук?"},'
        '{"text":"Кто оплачивает проезд участника на форум Машук?"}'
        "]}"
    )
    result = await semantic_recovery(
        {
            "message_masked": (
                "Хочу на Машук: подхожу ли я по условиям и оплатят ли мне дорогу?"
            ),
            "contextual_message": (
                "Машук: Хочу участвовать: подхожу ли я по условиям и оплатят ли дорогу?"
            ),
            "analysis": _analysis(),
            "llm_client": llm,
            "should_escalate": True,
            "escalation_reason": "insufficient_sources",
        }
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["response_format"] == "json"
    assert llm.calls[0]["temperature"] == 0.0
    assert result["semantic_recovery_attempted"] is True
    assert result["semantic_recovery_reason"] == "insufficient_sources"
    assert result["should_escalate"] is False
    assert result["escalation_reason"] == ""
    assert result["answer_plan"] == QueryProvenTopicPlan()
    assert result["reranked_chunks"] == []
    assert result["partial_source_missing_coverage"] == []
    recovered = result["analysis"]
    assert recovered.complexity == Complexity.COMPLEX
    assert [question.text for question in recovered.questions] == [
        "Какие условия участия в форуме Машук?",
        "Кто оплачивает проезд участника на форум Машук?",
        "Как участвовать?",
    ]
    assert all(question.topic is None for question in recovered.questions)
    assert all(question.category == "форумы" for question in recovered.questions)
    assert all(question.forum_normalized == "Машук" for question in recovered.questions)
    assert route_after_semantic_recovery(result) == "retrieve"


@pytest.mark.asyncio
async def test_semantic_recovery_fails_closed_on_invalid_output() -> None:
    result = await semantic_recovery(
        {
            "message_masked": "Сложный вопрос про форум",
            "analysis": _analysis(),
            "llm_client": CapturingRecoveryLLM('{"answer":"выдуманный ответ"}'),
            "escalation_reason": "low_confidence",
        }
    )

    assert result["semantic_recovery_attempted"] is True
    assert result["semantic_recovery_question_count"] == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "low_confidence"
    assert route_after_semantic_recovery(result) == "escalate"


@pytest.mark.asyncio
async def test_semantic_recovery_fails_closed_on_model_outage() -> None:
    result = await semantic_recovery(
        {
            "message_masked": "Сложный вопрос про форум",
            "analysis": _analysis(),
            "llm_client": FailingRecoveryLLM(),
            "escalation_reason": "low_confidence",
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "low_confidence"


@pytest.mark.asyncio
async def test_semantic_recovery_retries_real_retrieve_and_rerank_once() -> None:
    retriever = RecoveryAwareRetriever()
    llm = CapturingRecoveryLLM(
        '{"questions":[{"text":"Кто финансирует проезд участника на Машук?"}]}'
    )
    state = {
        "message_masked": "Как добраться на Машук?",
        "contextual_message": "Машук: как добраться?",
        "analysis": QueryAnalysis(
            category="форумы",
            forum_normalized="Машук",
            complexity=Complexity.COMPLEX,
            questions=[
                Question(
                    text="Как добраться на Машук?",
                    topic="transfer_do_mesta_provedeniya_meropriyatiya",
                    category="форумы",
                    forum_normalized="Машук",
                )
            ],
        ),
        "llm_client": llm,
        "retriever": retriever,
        "reranker": HighConfidenceReranker(),
    }

    first_retrieval = await retrieve(state)
    state.update(first_retrieval)
    first_rerank = await rerank(state)
    state.update(first_rerank)
    assert state["reranked_chunks"] == []
    assert state["escalation_reason"] == "no_relevant_chunks"
    assert route_after_rerank(state) == "recover"

    recovered = await semantic_recovery(state)
    state.update(recovered)
    assert route_after_semantic_recovery(state) == "retrieve"

    second_retrieval = await retrieve(state)
    state.update(second_retrieval)
    second_rerank = await rerank(state)
    state.update(second_rerank)

    assert [chunk.chunk_id for chunk in state["retrieved_chunks"]] == [
        "published_travel_fact"
    ]
    assert [chunk.chunk_id for chunk in state["reranked_chunks"]] == [
        "published_travel_fact"
    ]
    assert state["max_confidence"] >= 0.4
    assert route_after_rerank(state) == "generate"
    assert len(llm.calls) == 1
