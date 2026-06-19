from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.graph.nodes.analyze import (
    _apply_deterministic_forum,
    _coerce_analysis_payload,
    analyze_query,
)
from src.graph.nodes.escalate import escalate
from src.graph.nodes.generate import generate
from src.graph.nodes.rerank import _candidate_chunks_for_question, rerank
from src.graph.nodes.respond import respond
from src.graph.nodes.retrieve import retrieve
from src.graph.question_utils import build_effective_questions
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


class EmptyAnalysisLLM:
    async def generate(self, **kwargs):
        return '{"forum": null, "forum_normalized": null, "category": "техподдержка"}'


class AnalyzerOutageLLM:
    async def generate(self, **kwargs):
        raise RuntimeError("HTTP 503: no healthy upstream")


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


class BroadeningRetriever:
    def __init__(self) -> None:
        self.calls = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        if filters == {"forum_normalized": "Forum A", "category": "forums"}:
            return [
                Chunk(
                    chunk_id="strict_generic",
                    text="Generic forum answer.",
                    metadata={"chunk_id": "strict_generic"},
                    score=0.4,
                )
            ]
        if filters == {"forum_normalized": "Forum A"}:
            return [
                Chunk(
                    chunk_id="forum_specific",
                    text="Specific forum answer.",
                    metadata={"chunk_id": "forum_specific"},
                    score=0.7,
                )
            ]
        return []


class MultiForumRetriever:
    def __init__(self) -> None:
        self.calls = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        forum = filters.get("forum_normalized")
        if filters.get("category") != "форумы" or not forum:
            return []
        if "подать заявку" in query.casefold():
            return [
                Chunk(
                    chunk_id=f"{forum}_registration",
                    text=f"Регистрация на {forum}.",
                    metadata={"chunk_id": f"{forum}_registration", "forum_normalized": forum},
                    score=0.5,
                )
            ]
        if "проезд" in query.casefold():
            return [
                Chunk(
                    chunk_id=f"{forum}_travel",
                    text=f"Проезд на {forum}.",
                    metadata={"chunk_id": f"{forum}_travel", "forum_normalized": forum},
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


class BatchQuestionAwareReranker(QuestionAwareReranker):
    def __init__(self) -> None:
        super().__init__()
        self.group_calls = []

    def rerank_groups(
        self,
        groups: list[tuple[str, list[Chunk], int]],
    ) -> list[list[ScoredChunk]]:
        self.group_calls.append(groups)
        return [self.rerank(query, chunks, top_k) for query, chunks, top_k in groups]


class LowScoreReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.001,
            )
            for chunk in chunks[:top_k]
        ]


class InputOrderReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=1.0 - index * 0.1,
            )
            for index, chunk in enumerate(chunks[:top_k])
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


def test_route_after_verify_preserves_previous_escalation() -> None:
    state = {
        "should_escalate": True,
        "verification": VerificationResult(has_hallucination=False),
    }
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


@pytest.mark.asyncio
async def test_escalate_preserves_partial_answer_for_partial_source_coverage() -> None:
    result = await escalate(
        {
            "generated_response": "Подтверждённая часть ответа. [src:ctx_1]",
            "escalation_reason": "partial_source_coverage",
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "partial_source_coverage"
    assert result["final_response"].startswith("Подтверждённая часть ответа.")
    assert "[src:" not in result["final_response"]
    assert "нет достаточных подтверждённых данных" in result["final_response"]


@pytest.mark.asyncio
async def test_escalate_removes_full_coverage_claim_for_partial_source_coverage() -> None:
    result = await escalate(
        {
            "generated_response": (
                "Подтверждённая часть ответа.\n\n"
                "Источники полностью покрывают твои вопросы. [src:ctx_1]"
            ),
            "escalation_reason": "partial_source_coverage",
        }
    )

    assert "Подтверждённая часть ответа." in result["final_response"]
    assert "Источники полностью покрывают" not in result["final_response"]
    assert "нет достаточных подтверждённых данных" in result["final_response"]


@pytest.mark.asyncio
async def test_escalate_asks_for_forum_when_context_is_ambiguous() -> None:
    result = await escalate({"escalation_reason": "ambiguous_forum_context"})

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"
    assert "Уточните" in result["final_response"]
    assert "название форума" in result["final_response"]


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


def test_coerce_analysis_payload_normalizes_grant_project_aliases() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Гранты для физических лиц",
            "forum_normalized": "Гранты для физических лиц",
            "category": "реализация проекта",
            "questions": [
                {
                    "text": "Как вернуть средства?",
                    "category": "отчётность и средства",
                    "forum_normalized": "Гранты для физических лиц",
                }
            ],
        }
    )

    assert payload["category"] == "гранты"
    assert payload["forum"] is None
    assert payload["forum_normalized"] is None
    assert payload["questions"][0]["category"] == "гранты"
    assert payload["questions"][0]["forum_normalized"] is None


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


def test_fallback_questions_keep_reporting_deadline_in_grant_context() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="гранты"),
        "Проект по гранту сорвался, нужно вернуть деньги и понять сроки отчётности.",
    )

    assert [question.text for question in questions] == [
        "Как вернуть грантовые средства?",
        "Как оформить отчётность по гранту?",
    ]


def test_fallback_questions_do_not_match_hotel_marker_inside_wanted_word() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="форумы", forum_normalized="Арктика. Лёд тронулся"),
        "Арктика. Лёд тронулся Хотели бы поучаствовать в акции",
    )

    assert [question.text for question in questions] == [
        "Как подать заявку или зарегистрироваться?"
    ]


def test_apply_deterministic_forum_uses_registry_alias() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": None,
            "forum_normalized": None,
            "category": None,
            "questions": [{"text": "Какие документы нужны?"}],
        }
    )

    _apply_deterministic_forum(
        payload,
        "Хочу на Российский Север: какие документы нужны?",
    )

    assert payload["forum_normalized"] == "Российский Север"
    assert payload["category"] == "форумы"
    assert payload["questions"][0]["forum_normalized"] == "Российский Север"
    assert payload["questions"][0]["category"] == "форумы"


def test_apply_deterministic_forum_treats_grants_as_category_not_forum() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Гранты для физических лиц",
            "forum_normalized": "Гранты для физических лиц",
            "category": "реализация проекта",
            "questions": [
                {
                    "text": "Как вернуть средства?",
                    "forum_normalized": "Гранты для физических лиц",
                }
            ],
        }
    )

    _apply_deterministic_forum(
        payload,
        "Гранты для физических лиц: проект сорвался, как вернуть грантовые средства?",
    )

    assert payload["category"] == "гранты"
    assert payload["forum"] is None
    assert payload["forum_normalized"] is None
    assert payload["questions"][0]["category"] == "гранты"
    assert payload["questions"][0]["forum_normalized"] is None


def test_apply_deterministic_forum_overrides_non_registry_llm_forum() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Личный кабинет",
            "forum_normalized": "Личный кабинет",
            "category": "техподдержка",
            "questions": [],
        }
    )

    _apply_deterministic_forum(
        payload,
        "Амур Вышлите пожалуйста положение",
    )

    assert payload["forum"] == "Амур"
    assert payload["forum_normalized"] == "Амур"
    assert payload["category"] == "форумы"


def test_apply_deterministic_forum_overrides_question_forum() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Арктика. Лёд тронулся",
            "forum_normalized": "Арктика. Лёд тронулся",
            "category": "форумы",
            "questions": [
                {
                    "text": "Как принять участие?",
                    "category": "форумы",
                    "forum_normalized": "Арктика",
                }
            ],
        }
    )

    _apply_deterministic_forum(
        payload,
        "Арктика. Лёд тронулся Хотели бы поучаствовать в акции",
    )

    assert payload["questions"][0]["forum_normalized"] == "Арктика. Лёд тронулся"


def test_apply_deterministic_forum_preserves_multiple_detected_forums() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": None,
            "forum_normalized": None,
            "category": None,
            "questions": [{"text": "Чем отличаются по регистрации и проезду?"}],
        }
    )

    _apply_deterministic_forum(
        payload,
        "Чем отличаются Машук и Территория смыслов по регистрации и проезду?",
    )

    assert payload["forum_normalized"] is None
    assert payload["category"] == "форумы"
    assert payload["extracted_params"]["detected_forums"] == [
        "Территория смыслов",
        "Машук",
    ]
    assert payload["questions"][0]["forum_normalized"] is None


@pytest.mark.asyncio
async def test_analyze_detects_forum_from_original_message_after_pii_masking() -> None:
    result = await analyze_query(
        {
            "message": "Амур Вышлите пожалуйста положение",
            "message_masked": "[ИМЯ] Вышлите пожалуйста положение",
            "llm_client": EmptyAnalysisLLM(),
        }
    )

    analysis = result["analysis"]
    assert analysis.forum_normalized == "Амур"
    assert analysis.category == "форумы"


@pytest.mark.asyncio
async def test_analyze_falls_back_to_deterministic_grant_routing_on_llm_outage() -> None:
    result = await analyze_query(
        {
            "message": "где подать проект на грант",
            "message_masked": "где подать проект на грант",
            "routing_hint": {"complexity": "simple"},
            "llm_client": AnalyzerOutageLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_fallback"] is True
    assert analysis.category == "гранты"
    assert analysis.complexity == Complexity.SIMPLE
    assert analysis.questions[0].text == "где подать проект на грант"
    assert analysis.questions[0].category == "гранты"
    assert "should_escalate" not in result


@pytest.mark.asyncio
async def test_analyze_still_escalates_unclear_query_on_llm_outage() -> None:
    result = await analyze_query(
        {
            "message": "непонятная формулировка без домена",
            "message_masked": "непонятная формулировка без домена",
            "routing_hint": {"complexity": "complex"},
            "llm_client": AnalyzerOutageLLM(),
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "analyzer_failed"
    assert "HTTP 503" in result["error"]


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
            "Как подать заявку или зарегистрироваться?",
            {"category": "гранты"},
            10,
        ),
        (
            "Как подать заявку или зарегистрироваться?",
            {},
            10,
        ),
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
async def test_retrieve_adds_one_broader_candidate_layer_after_strict_hit() -> None:
    retriever = BroadeningRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                forum_normalized="Forum A",
                category="forums",
                questions=[Question(text="Where is the schedule?")],
            ),
            "retriever": retriever,
        }
    )

    assert [chunk.chunk_id for chunk in result["retrieved_chunks"]] == [
        "strict_generic",
        "forum_specific",
    ]
    assert retriever.calls == [
        ("Where is the schedule?", {"forum_normalized": "Forum A", "category": "forums"}, 10),
        ("Where is the schedule?", {"forum_normalized": "Forum A"}, 10),
    ]


@pytest.mark.asyncio
async def test_retrieve_expands_multi_forum_fallback_questions() -> None:
    retriever = MultiForumRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                category="форумы",
                extracted_params={"detected_forums": ["Машук", "Территория смыслов"]},
            ),
            "message_masked": (
                "Чем отличаются Машук и Территория смыслов по регистрации, "
                "проживанию и оплате проезда?"
            ),
            "retriever": retriever,
        }
    )

    chunk_ids = {chunk.chunk_id for chunk in result["retrieved_chunks"]}
    assert {
        "Машук_registration",
        "Территория смыслов_registration",
        "Машук_travel",
        "Территория смыслов_travel",
    } <= chunk_ids
    strict_calls = [
        (query, filters)
        for query, filters, _top_k in retriever.calls
        if filters.get("category") == "форумы"
    ]
    assert (
        "Машук: Как подать заявку или зарегистрироваться?",
        {"forum_normalized": "Машук", "category": "форумы"},
    ) in strict_calls
    assert (
        "Территория смыслов: Кто оплачивает проезд?",
        {"forum_normalized": "Территория смыслов", "category": "форумы"},
    ) in strict_calls


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


def test_rerank_candidate_prefilter_prioritizes_participation_action_wording() -> None:
    chunks = [
        Chunk(
            chunk_id="dates",
            text="Актуальные даты проведения форума будут объявлены позже.",
            metadata={"intent_name": "Даты начала мероприятия"},
            score=1.0,
        ),
        Chunk(
            chunk_id="application",
            text="Заявки принимаются до 17 июня включительно. Присоединяйся к акции.",
            metadata={"intent_name": "Подача заявки на проект"},
            score=0.1,
        ),
    ]

    candidates = _candidate_chunks_for_question("Хотели бы поучаствовать в акции", chunks, 2)

    assert [chunk.chunk_id for chunk in candidates] == ["application", "dates"]


def test_rerank_candidate_prefilter_prioritizes_intent_marker() -> None:
    chunks = [
        Chunk(
            chunk_id="logistics",
            text="Проживание и питание на время форума оплачивает организатор.",
            metadata={"intent_name": "Оплата проезда, проживания и чартер"},
            score=1.0,
        ),
        Chunk(
            chunk_id="food",
            text="На площадке будут точки питания и кулеры с питьевой водой.",
            metadata={"intent_name": "Условия питания"},
            score=0.1,
        ),
    ]

    candidates = _candidate_chunks_for_question("Есть ли питание?", chunks, limit=2)

    assert [chunk.chunk_id for chunk in candidates] == ["food", "logistics"]


def test_rerank_candidate_prefilter_prioritizes_travel_to_venue_wording() -> None:
    chunks = [
        Chunk(
            chunk_id="docs",
            text="Паспорт и справка нужны при заезде.",
            metadata={"intent_name": "Документы"},
            score=1.0,
        ),
        Chunk(
            chunk_id="travel",
            text="Логистика: дорога до Москвы самостоятельно, далее чартер до Салехарда.",
            metadata={"intent_name": "Оплата проезда, проживания и чартер"},
            score=0.1,
        ),
    ]

    candidates = _candidate_chunks_for_question("Как ехать до площадки?", chunks, 2)

    assert [chunk.chunk_id for chunk in candidates] == ["travel", "docs"]


def test_rerank_candidate_prefilter_prioritizes_regional_invitation_letter() -> None:
    chunks = [
        Chunk(
            chunk_id="travel",
            text="Проезд и проживание оплачиваются организаторами.",
            metadata={"intent_name": "Оплата проезда"},
            score=1.0,
        ),
        Chunk(
            chunk_id="invitation",
            text="Письмо-вызов можно запросить через орган молодёжной политики региона.",
            metadata={"intent_name": "Письмо-вызов"},
            score=0.2,
        ),
    ]

    candidates = _candidate_chunks_for_question("Хотел бы запросить письмо на регион", chunks, 2)

    assert [chunk.chunk_id for chunk in candidates] == ["invitation", "travel"]


def test_rerank_candidate_prefilter_prioritizes_grant_return_intent() -> None:
    chunks = [
        Chunk(
            chunk_id="generic_grant",
            text="На форуме можно подать заявку на грантовый конкурс.",
            metadata={"intent_name": "Росмолодёжь.Гранты"},
            score=1.0,
        ),
        Chunk(
            chunk_id="grant_return",
            text="Вернуть грантовые средства можно через почту reportgrant2024@fadm.gov.ru.",
            metadata={"intent_name": "Вернуть денежные средства"},
            score=0.1,
        ),
    ]

    candidates = _candidate_chunks_for_question("Как вернуть грантовые средства?", chunks, 2)

    assert [chunk.chunk_id for chunk in candidates] == ["grant_return", "generic_grant"]


@pytest.mark.asyncio
async def test_rerank_single_question_prefilter_prioritizes_selection_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(ml_unload_after_use=False, reranker_threshold_low=0.4),
    )
    chunks = [
        Chunk(
            chunk_id="decline",
            text="Если решишь отказаться от участия, сообщи организаторам.",
            metadata={"intent_name": "Отказ от участия"},
            score=0.9,
        ),
        Chunk(
            chunk_id="results",
            text="Результаты конкурсного отбора придут на электронную почту.",
            metadata={"intent_name": "Результаты РМ"},
            score=0.7,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Когда будут известны результаты конкурсного отбора?",
            "analysis": QueryAnalysis(category="форумы"),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "results"


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
async def test_rerank_uses_retrieval_confidence_floor_for_exact_forum_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            ml_unload_after_use=False,
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    chunks = [
        Chunk(
            chunk_id="docs",
            text="Положение форума доступно в личном кабинете.",
            metadata={"forum_normalized": "Машук", "intent_name": "Документы мероприятия"},
            score=1.0,
        )
    ]

    result = await rerank(
        {
            "message_masked": "[ИМЯ] Вышлите положение",
            "analysis": QueryAnalysis(forum_normalized="Машук", category="техподдержка"),
            "retrieved_chunks": chunks,
            "reranker": LowScoreReranker(),
        }
    )

    assert result.get("should_escalate") is not True
    assert result["max_confidence"] == 0.7


@pytest.mark.asyncio
async def test_rerank_uses_retrieval_confidence_floor_for_exact_category_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            ml_unload_after_use=False,
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    chunks = [
        Chunk(
            chunk_id="grant_return",
            text="Для возврата грантовых средств напишите на почту отчётности.",
            metadata={"category": "гранты", "intent_name": "Вернуть денежные средства"},
            score=0.7,
        )
    ]

    result = await rerank(
        {
            "message_masked": "Как вернуть грантовые средства?",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": LowScoreReranker(),
        }
    )

    assert result.get("should_escalate") is not True
    assert result["max_confidence"] == 0.4


@pytest.mark.asyncio
async def test_rerank_does_not_use_retrieval_floor_without_exact_forum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(ml_unload_after_use=False, reranker_threshold_low=0.4),
    )
    chunks = [
        Chunk(
            chunk_id="docs",
            text="Положение форума доступно в личном кабинете.",
            metadata={"forum_normalized": "Утро", "intent_name": "Документы мероприятия"},
            score=1.0,
        )
    ]

    result = await rerank(
        {
            "message_masked": "[ИМЯ] Вышлите положение",
            "analysis": QueryAnalysis(forum_normalized="Машук", category="техподдержка"),
            "retrieved_chunks": chunks,
            "reranker": LowScoreReranker(),
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "low_confidence"


@pytest.mark.asyncio
async def test_rerank_uses_batched_group_api_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(ml_unload_after_use=False, reranker_threshold_low=0.4),
    )
    reranker = BatchQuestionAwareReranker()
    chunks = [
        Chunk(chunk_id="docs", text="Паспорт.", metadata={"intent_name": "Документы"}),
        Chunk(chunk_id="age", text="От 14 до 35 лет.", metadata={"intent_name": "Возраст"}),
    ]

    await rerank(
        {
            "message_masked": "Документы и возраст?",
            "analysis": QueryAnalysis(
                questions=[
                    Question(text="Какие документы нужны?"),
                    Question(text="Какие возрастные ограничения?"),
                ]
            ),
            "retrieved_chunks": chunks,
            "reranker": reranker,
        }
    )

    assert len(reranker.group_calls) == 1
    assert [group[0] for group in reranker.group_calls[0]] == [
        "Какие документы нужны?",
        "Какие возрастные ограничения?",
        "Документы и возраст?",
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
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
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
async def test_generate_returns_source_chunk_for_simple_intent_match_at_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="letter",
        text="Письмо-вызов можно запросить через орган молодёжной политики региона.",
        metadata={"intent_name": "Письмо-вызов"},
        score=0.7,
        reranker_score=0.48,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                questions=[Question(text="Как получить письмо-вызов?")],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.48,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "Письмо-вызов можно запросить через орган молодёжной политики региона. "
        "[src:letter]"
    )
    assert result["generator_model"] == "source_chunk"


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
