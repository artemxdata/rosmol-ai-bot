from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.graph.nodes.analyze import _coerce_analysis_payload
from src.graph.nodes.generate import generate
from src.graph.nodes.rerank import _candidate_chunks_for_question, rerank
from src.graph.nodes.respond import respond
from src.graph.nodes.retrieve import retrieve
from src.models import Chunk, Complexity, QueryAnalysis, Question, ScoredChunk, VerificationResult


class FailingLLM:
    async def generate(self, **kwargs):
        raise AssertionError("LLM must not be called in this test")


class CapturingLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs = []

    async def generate(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return "LLM answer [src:ctx_1]"


class CapturingRetriever:
    def __init__(self) -> None:
        self.calls = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        return []


class ForumFallbackRetriever:
    def __init__(self) -> None:
        self.calls = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        if filters == {"forum_normalized": "Российский Север"}:
            return [
                Chunk(
                    chunk_id="north_docs",
                    text="Документы для Российского Севера.",
                    metadata={"chunk_id": "north_docs"},
                    score=0.5,
                )
            ]
        return []


class QuestionAwareReranker:
    def __init__(self) -> None:
        self.calls = []

    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        self.calls.append((query, top_k))
        query_lower = query.casefold()
        scores = {
            "docs": 0.9 if "документ" in query_lower else 0.2,
            "age": 0.85 if "возраст" in query_lower else 0.1,
            "travel": 0.8 if "трансфер" in query_lower else 0.7,
        }
        ranked = sorted(chunks, key=lambda chunk: scores[chunk.chunk_id], reverse=True)
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=scores[chunk.chunk_id],
            )
            for chunk in ranked[:top_k]
        ]


def test_route_after_analyze_clarifies() -> None:
    state = {"analysis": QueryAnalysis(needs_clarification=True)}
    assert route_after_analyze(state) == "clarify"


def test_route_after_rerank_escalates_on_low_score(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.graph.edges.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4),
    )
    assert route_after_rerank({"max_confidence": 0.1}) == "escalate"


def test_route_after_verify_escalates_on_hallucination() -> None:
    state = {"verification": VerificationResult(has_hallucination=True)}
    assert route_after_verify(state) == "escalate"


@pytest.mark.asyncio
async def test_respond_does_not_append_specialist_note_for_valid_lowish_confidence() -> None:
    result = await respond(
        {
            "generated_response": "Ответ по источнику [src:ctx_1]",
            "max_confidence": 0.56,
        }
    )

    assert result["final_response"] == "Ответ по источнику"


def test_coerce_analysis_payload_accepts_topic_objects() -> None:
    payload = _coerce_analysis_payload({"topics": [{"title": "Подать заявку"}, "гранты"]})

    assert payload["topics"] == ["Подать заявку", "гранты"]


def test_coerce_analysis_payload_normalizes_taxonomy_aliases() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Территория смыслов",
            "category": "мероприятия и форумы",
            "questions": [
                {
                    "text": "Как зарегистрироваться?",
                    "category": "регистрация",
                    "forum": "Машук",
                },
                {
                    "text": "Где найти id?",
                    "category": "технические проблемы",
                },
            ],
        }
    )

    assert payload["category"] == "форумы"
    assert payload["forum_normalized"] == "Территория смыслов"
    assert payload["questions"][0]["category"] == "платформа_фгаис"
    assert payload["questions"][0]["forum_normalized"] == "Машук"
    assert payload["questions"][1]["category"] == "техподдержка"


def test_coerce_analysis_payload_drops_boolean_optional_strings() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": True,
            "forum_normalized": True,
            "category": False,
            "topics": [True, "регистрация"],
            "questions": [
                {
                    "text": "Кто оплачивает дорогу?",
                    "topic": True,
                    "forum": True,
                    "forum_normalized": False,
                    "category": True,
                }
            ],
        }
    )

    assert payload["forum"] is None
    assert payload["forum_normalized"] is None
    assert payload["category"] is None
    assert payload["topics"] == ["регистрация"]
    assert payload["questions"][0]["topic"] is None
    assert payload["questions"][0]["forum"] is None
    assert payload["questions"][0]["forum_normalized"] is None
    assert payload["questions"][0]["category"] is None


@pytest.mark.asyncio
async def test_retrieve_uses_masked_message_when_analysis_has_no_questions() -> None:
    retriever = CapturingRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(category="гранты"),
            "message_masked": "Гранты для физических лиц Подать заявку на участие",
            "retriever": retriever,
        }
    )

    assert result["retrieved_chunks"] == []
    assert retriever.calls == [
        (
            "Гранты для физических лиц Подать заявку на участие",
            {"category": "гранты"},
            10,
        )
    ]


@pytest.mark.asyncio
async def test_retrieve_retries_forum_without_category_when_strict_filter_is_empty() -> None:
    retriever = ForumFallbackRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                forum_normalized="Российский Север",
                category="участие",
                questions=[Question(text="Какие документы нужны?")],
            ),
            "retriever": retriever,
        }
    )

    assert [chunk.chunk_id for chunk in result["retrieved_chunks"]] == ["north_docs"]
    assert retriever.calls == [
        (
            "Какие документы нужны?",
            {"forum_normalized": "Российский Север", "category": "участие"},
            10,
        ),
        (
            "Какие документы нужны?",
            {"forum_normalized": "Российский Север"},
            10,
        ),
    ]


@pytest.mark.asyncio
async def test_rerank_preserves_sources_for_each_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(ml_unload_after_use=False, reranker_threshold_low=0.4),
    )
    chunks = [
        Chunk(
            chunk_id="docs",
            text="Паспорт и справка.",
            metadata={"intent_name": "Документы"},
            score=0.2,
        ),
        Chunk(
            chunk_id="age",
            text="От 14 до 35 лет.",
            metadata={"intent_name": "Возрастные ограничения"},
            score=0.2,
        ),
        Chunk(
            chunk_id="travel",
            text="Есть трансфер.",
            metadata={"intent_name": "Трансфер"},
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Документы, трансфер и возраст?",
            "analysis": QueryAnalysis(
                questions=[
                    Question(text="Какие документы нужны?"),
                    Question(text="Какие ограничения по возрасту?"),
                ]
            ),
            "retrieved_chunks": chunks,
            "reranker": QuestionAwareReranker(),
        }
    )

    assert [chunk.chunk_id for chunk in result["reranked_chunks"][:2]] == ["docs", "age"]
    assert result["max_confidence"] == 0.9


def test_rerank_candidate_prefilter_prioritizes_domain_marker() -> None:
    chunks = [
        Chunk(
            chunk_id="transfer",
            text="Бесплатный трансфер по Салехарду будет организован для всех участников.",
            metadata={"intent_name": "Трансфер по городу"},
            score=1.0,
        ),
        Chunk(
            chunk_id="docs",
            text="Что взять обязательно из документов: паспорт и справку от врача.",
            metadata={"intent_name": "Документы, вещи и дресс-код"},
            score=0.2,
        ),
    ]

    candidates = _candidate_chunks_for_question("Какие документы нужны?", chunks, limit=2)

    assert [chunk.chunk_id for chunk in candidates] == ["docs", "transfer"]


@pytest.mark.asyncio
async def test_rerank_uses_fallback_questions_when_analyzer_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(ml_unload_after_use=False, reranker_threshold_low=0.4),
    )
    chunks = [
        Chunk(
            chunk_id="docs",
            text="Паспорт и справка.",
            metadata={"intent_name": "Документы"},
            score=0.2,
        ),
        Chunk(
            chunk_id="age",
            text="От 14 до 35 лет.",
            metadata={"intent_name": "Возрастные ограничения"},
            score=0.2,
        ),
        Chunk(
            chunk_id="travel",
            text="Есть трансфер.",
            metadata={"intent_name": "Трансфер"},
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Документы, трансфер и ограничения по возрасту?",
            "analysis": QueryAnalysis(questions=[]),
            "retrieved_chunks": chunks,
            "reranker": QuestionAwareReranker(),
        }
    )

    assert [chunk.chunk_id for chunk in result["reranked_chunks"][:3]] == [
        "docs",
        "travel",
        "age",
    ]


@pytest.mark.asyncio
async def test_generate_escalates_without_source_chunks() -> None:
    result = await generate(
        {
            "analysis": QueryAnalysis(
                questions=[Question(text="Кто оплачивает проезд?", category="форумы")]
            ),
            "reranked_chunks": [],
            "llm_client": FailingLLM(),
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_sources_for_generation"
    assert result["cited_sources"] == []


@pytest.mark.asyncio
async def test_generate_returns_source_chunk_for_simple_high_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="ctx_1",
        text="Исходный ответ из базы.",
        metadata={"chunk_id": "ctx_1"},
        score=0.9,
        reranker_score=0.91,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                questions=[Question(text="Кто оплачивает проезд?", category="форумы")]
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.91,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == "Исходный ответ из базы. [src:ctx_1]"
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ctx_1"]


@pytest.mark.asyncio
async def test_generate_uses_llm_for_complex_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_high=0.7),
    )
    llm = CapturingLLM()
    chunk = ScoredChunk(
        chunk_id="ctx_1",
        text="Исходный ответ из базы.",
        metadata={"chunk_id": "ctx_1"},
        score=0.9,
        reranker_score=0.91,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                questions=[Question(text="Сложный вопрос", category="форумы")],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.91,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["generated_response"] == "LLM answer [src:ctx_1]"


@pytest.mark.asyncio
async def test_generate_uses_masked_message_when_analyzer_returns_no_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_high=0.7),
    )
    llm = CapturingLLM()
    chunk = ScoredChunk(
        chunk_id="ctx_1",
        text="Исходный ответ из базы.",
        metadata={"chunk_id": "ctx_1"},
        score=0.9,
        reranker_score=0.2,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized="Российский Север",
                questions=[],
            ),
            "message_masked": "Какие документы нужны на Российский Север?",
            "reranked_chunks": [chunk],
            "max_confidence": 0.2,
            "llm_client": llm,
        }
    )

    assert result["generated_response"] == "LLM answer [src:ctx_1]"
    assert "Какие документы нужны?" in llm.kwargs[0]["user"]
