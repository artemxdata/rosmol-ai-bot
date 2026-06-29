from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.context import apply_session_context, build_contextual_message
from src.graph.edges import route_after_analyze, route_after_rerank, route_after_verify
from src.graph.nodes.analyze import (
    _apply_deterministic_forum,
    _coerce_analysis_payload,
    analyze_query,
)
from src.graph.nodes.clarify import OFFTOPIC_SCOPE_NOTE, clarify
from src.graph.nodes.escalate import PARTIAL_COVERAGE_NOTE, escalate
from src.graph.nodes.generate import build_deterministic_source_response, generate
from src.graph.nodes.rerank import _candidate_chunks_for_question, rerank
from src.graph.nodes.respond import respond
from src.graph.nodes.retrieve import retrieve
from src.graph.nodes.verify import verify
from src.graph.query_normalization import expand_query_aliases
from src.graph.question_utils import build_effective_questions
from src.models import (
    Channel,
    Chunk,
    Complexity,
    QueryAnalysis,
    Question,
    ScoredChunk,
    Session,
    VerificationResult,
)


class FailingLLM:
    async def generate(self, **kwargs):
        raise AssertionError("LLM must not be called in this test")


class CapturingLLM:
    def __init__(self, response: str = "LLM answer [src:ctx_1]") -> None:
        self.response = response
        self.calls = 0
        self.kwargs = []

    async def generate(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return self.response


class EmptyAnalysisLLM:
    async def generate(self, **kwargs):
        return '{"forum": null, "forum_normalized": null, "category": "техподдержка"}'


class CapturingAnalysisLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return self.payload


class AnalyzerOutageLLM:
    async def generate(self, **kwargs):
        raise RuntimeError("HTTP 503: no healthy upstream")


class CapturingRetriever:
    def __init__(self) -> None:
        self.calls = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        return []


class KeywordRecallRetriever(CapturingRetriever):
    def __init__(self) -> None:
        super().__init__()
        self.keyword_calls = []

    async def retrieve_keyword_candidates(
        self,
        query: str,
        filters: dict,
        *,
        top_k: int,
        scan_limit: int,
        min_score: float,
        source_type: str,
    ):
        self.keyword_calls.append((query, filters, top_k, scan_limit, min_score, source_type))
        if source_type == "xlsx" and filters == {"category": "grants"}:
            return [
                Chunk(
                    chunk_id="xlsx_exact",
                    text="Exact official source.",
                    metadata={
                        "chunk_id": "xlsx_exact",
                        "source_type": "xlsx",
                    },
                    score=1.0,
                )
            ]
        if source_type == "ticket_answer_bank" and filters == {}:
            return [
                Chunk(
                    chunk_id="ticket_answer_bank_exact",
                    text="Exact private answer-bank source.",
                    metadata={
                        "chunk_id": "ticket_answer_bank_exact",
                        "source_type": "ticket_answer_bank",
                    },
                    score=1.0,
                )
            ]
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


class ExactForumDenseRetriever:
    def __init__(self) -> None:
        self.calls = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        if filters == {"forum_normalized": "Forum A", "category": "forums"}:
            return [
                Chunk(
                    chunk_id=f"forum_exact_{index}",
                    text=f"Exact forum source {index}.",
                    metadata={
                        "chunk_id": f"forum_exact_{index}",
                        "forum_normalized": "Forum A",
                        "category": "forums",
                    },
                    score=0.9,
                )
                for index in range(3)
            ]
        return []


class MultiAspectDenseForumRetriever(ExactForumDenseRetriever):
    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        if filters == {"forum_normalized": "Forum A", "category": "forums"}:
            return [
                Chunk(
                    chunk_id=f"forum_exact_{index}",
                    text=f"Exact forum source {index}.",
                    metadata={
                        "chunk_id": f"forum_exact_{index}",
                        "forum_normalized": "Forum A",
                        "category": "forums",
                    },
                    score=0.9,
                )
                for index in range(3)
            ]
        if filters == {"forum_normalized": "Forum A"}:
            return [
                Chunk(
                    chunk_id="forum_broad_multi_aspect",
                    text="Broad forum source for another requested aspect.",
                    metadata={
                        "chunk_id": "forum_broad_multi_aspect",
                        "forum_normalized": "Forum A",
                    },
                    score=0.8,
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


class DroppingExactReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        broad_chunks = [chunk for chunk in chunks if chunk.chunk_id != "fgais_registration"]
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.9 - index * 0.1,
            )
            for index, chunk in enumerate(broad_chunks[:top_k])
        ]


class FailingReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        raise AssertionError("cross-encoder reranker must not be called")


class InputOrderGroupReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
        return [
            ScoredChunk(
                **chunk.model_dump(exclude={"score"}),
                score=chunk.score,
                reranker_score=0.9 - index * 0.1,
            )
            for index, chunk in enumerate(chunks[:top_k])
        ]

    def rerank_groups(
        self,
        groups: list[tuple[str, list[Chunk], int]],
    ) -> list[list[ScoredChunk]]:
        return [self.rerank(query, chunks, top_k) for query, chunks, top_k in groups]


def test_route_after_analyze_clarifies() -> None:
    state = {"analysis": QueryAnalysis(needs_clarification=True)}
    assert route_after_analyze(state) == "clarify"


def test_route_after_analyze_clarifies_safe_offtopic_without_escalation() -> None:
    state = {"analysis": QueryAnalysis(category="offtopic", is_offtopic=True)}
    assert route_after_analyze(state) == "clarify"


@pytest.mark.asyncio
async def test_clarify_returns_scope_note_for_safe_offtopic() -> None:
    result = await clarify({"analysis": QueryAnalysis(category="offtopic", is_offtopic=True)})

    assert result["final_response"] == OFFTOPIC_SCOPE_NOTE
    assert result["should_escalate"] is False
    assert result["escalation_reason"] is None


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
async def test_verify_allows_source_supported_confirmation_instruction() -> None:
    chunk = ScoredChunk(
        chunk_id="confirmation",
        text="Обязательно нужно подтвердить своё участие через личный кабинет.",
        metadata={"chunk_id": "confirmation", "source_type": "xlsx"},
        score=0.8,
        reranker_score=0.8,
    )

    result = await verify(
        {
            "generated_response": (
                "Ты можешь подтвердить участие в личном кабинете. [src:confirmation]"
            ),
            "reranked_chunks": [chunk],
            "cited_sources": ["confirmation"],
            "generator_model": "source_chunk",
            "max_confidence": 0.8,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result


@pytest.mark.asyncio
async def test_verify_allows_source_supported_email_instruction() -> None:
    chunk = ScoredChunk(
        chunk_id="docs",
        text="Полный список рекомендаций будет отправлен участникам на электронную почту.",
        metadata={"chunk_id": "docs", "source_type": "xlsx"},
        score=0.8,
        reranker_score=0.8,
    )

    result = await verify(
        {
            "generated_response": (
                "Список рекомендаций отправят на электронную почту. [src:docs]"
            ),
            "reranked_chunks": [chunk],
            "cited_sources": ["docs"],
            "generator_model": "source_chunk",
            "max_confidence": 0.8,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result


@pytest.mark.asyncio
async def test_verify_allows_source_supported_plain_email_wording() -> None:
    chunk = ScoredChunk(
        chunk_id="results",
        text="Тебе придёт письмо с результатами отбора на почту, указанную при регистрации.",
        metadata={"chunk_id": "results", "source_type": "xlsx"},
        score=0.8,
        reranker_score=0.8,
    )

    result = await verify(
        {
            "generated_response": (
                "Результаты отбора придут на электронную почту. [src:results]"
            ),
            "reranked_chunks": [chunk],
            "cited_sources": ["results"],
            "generator_model": "source_chunk",
            "max_confidence": 0.8,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result


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
async def test_respond_preserves_paragraphs_between_multiple_source_chunks() -> None:
    result = await respond(
        {
            "generated_response": (
                "Проезд оплачивается направляющей стороной. [src:travel]\n\n"
                "Формат проживания: палатки на площадке. [src:housing]"
            ),
        }
    )

    assert result["final_response"] == (
        "Проезд оплачивается направляющей стороной.\n\n"
        "Формат проживания: палатки на площадке."
    )


@pytest.mark.asyncio
async def test_respond_normalizes_user_address_to_ty() -> None:
    result = await respond(
        {
            "generated_response": (
                "Вы сможете посмотреть статус в вашем личном кабинете. "
                "Если у вас нет доступа, перейдите в профиль и нажмите кнопку "
                "обновления. [src:profile]"
            ),
        }
    )

    assert result["final_response"] == (
        "Ты сможешь посмотреть статус в твоём личном кабинете. "
        "Если у тебя нет доступа, перейди в профиль и нажми кнопку обновления."
    )


@pytest.mark.asyncio
async def test_respond_normalizes_ty_verbs_and_sentence_spacing() -> None:
    result = await respond(
        {
            "generated_response": (
                "Вы приезжаете на площадку за свой счёт."
                "Этот формат:питание самостоятельно. [src:food]"
            ),
        }
    )

    assert result["final_response"] == (
        "Ты приезжаешь на площадку за свой счёт. "
        "Этот формат: питание самостоятельно."
    )


@pytest.mark.asyncio
async def test_respond_preserves_links_email_and_time_spacing() -> None:
    result = await respond(
        {
            "generated_response": (
                "Напишите на reportgrant2024@fadm.gov.ru с 09:00 до 18:00."
                "Профиль: https://myrosmol.ru/profile?section=accounts."
                "События: events.myrosmol.ru/forumy/. [src:contacts]"
            ),
        }
    )

    assert result["final_response"] == (
        "Напиши на reportgrant2024@fadm.gov.ru с 09:00 до 18:00. "
        "Профиль: https://myrosmol.ru/profile?section=accounts. "
        "События: events.myrosmol.ru/forumy/."
    )


@pytest.mark.asyncio
async def test_respond_repairs_llm_spacing_inside_structured_tokens() -> None:
    result = await respond(
        {
            "generated_response": (
                "Свяжись с нами: reportgrant2024@fadm. gov. ru, "
                "пн-пт 09: 00-18: 00. Кабинет myrosmol. ru/profile? section=accounts. "
                "[src:contacts]"
            ),
        }
    )

    assert result["final_response"] == (
        "Свяжись с нами: reportgrant2024@fadm.gov.ru, "
        "пн-пт 09:00-18:00. Кабинет myrosmol.ru/profile?section=accounts."
    )


@pytest.mark.asyncio
async def test_respond_normalizes_common_polite_instruction_verbs() -> None:
    result = await respond(
        {
            "generated_response": (
                "Давайте мы тебе поможем. Выйдите из аккаунта. Войдите через Госуслуги. "
                "Отмените привязку и повторите попытку. Убедитесь, что вы используете ваш аккаунт. "
                "Если вы хотите привязать Госуслуги, нажмите кнопку. [src:profile]"
            ),
        }
    )

    assert result["final_response"] == (
        "Давай мы тебе поможем. Выйди из аккаунта. Войди через Госуслуги. "
        "Отмени привязку и повтори попытку. Убедись, что ты используешь твой аккаунт. "
        "Если ты хочешь привязать Госуслуги, нажми кнопку."
    )


@pytest.mark.asyncio
async def test_escalate_returns_only_safe_note_for_partial_source_coverage() -> None:
    result = await escalate(
        {
            "generated_response": "Подтверждённая часть ответа. [src:ctx_1]",
            "escalation_reason": "partial_source_coverage",
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "partial_source_coverage"
    assert result["final_response"] == PARTIAL_COVERAGE_NOTE
    assert "Подтверждённая часть ответа" not in result["final_response"]
    assert "[src:" not in result["final_response"]
    assert "нет достаточных подтверждённых данных" in result["final_response"]


@pytest.mark.asyncio
async def test_escalate_drops_generated_claims_for_partial_source_coverage() -> None:
    result = await escalate(
        {
            "generated_response": (
                "Подтверждённая часть ответа.\n\n"
                "Источники полностью покрывают твои вопросы. [src:ctx_1]"
            ),
            "escalation_reason": "partial_source_coverage",
        }
    )

    assert result["final_response"] == PARTIAL_COVERAGE_NOTE
    assert "Подтверждённая часть ответа." not in result["final_response"]
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


def test_coerce_analysis_payload_drops_rosmol_grant_pseudo_forum() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Гранты Росмолодёжи",
            "forum_normalized": "Гранты Росмолодёжи",
            "category": "гранты",
            "questions": [
                {
                    "text": "Где подать проект на грант?",
                    "forum_normalized": "Гранты Росмолодёжи",
                }
            ],
        }
    )

    assert payload["forum"] is None
    assert payload["forum_normalized"] is None
    assert payload["questions"][0]["forum_normalized"] is None


def test_coerce_analysis_payload_drops_plain_grants_pseudo_forum() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "Гранты",
            "forum_normalized": "Гранты",
            "category": "гранты",
            "questions": [
                {
                    "text": "Где подать проект на грант?",
                    "forum_normalized": "Гранты",
                    "category": "гранты",
                }
            ],
        }
    )

    assert payload["forum"] is None
    assert payload["forum_normalized"] is None
    assert payload["questions"][0]["forum_normalized"] is None


def test_coerce_analysis_payload_drops_platform_domain_pseudo_forum() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "admin.myrosmol.ru",
            "forum_normalized": "admin.myrosmol.ru",
            "category": "форумы",
            "questions": [
                {
                    "text": "Не получается открыть заявку в личном кабинете",
                    "forum_normalized": "admin.myrosmol.ru",
                    "category": "форумы",
                }
            ],
        }
    )

    assert payload["category"] == "платформа_фгаис"
    assert payload["forum"] is None
    assert payload["forum_normalized"] is None
    assert payload["questions"][0]["category"] == "платформа_фгаис"
    assert payload["questions"][0]["forum_normalized"] is None


def test_coerce_analysis_payload_normalizes_latin_i_ivolga_alias() -> None:
    payload = _coerce_analysis_payload(
        {
            "forum": "iВолга",
            "forum_normalized": "iВолга",
            "category": "форумы",
            "questions": [
                {
                    "text": "Как подать заявку?",
                    "forum_normalized": "iВолга",
                }
            ],
        }
    )

    assert payload["forum"] == "Иволга"
    assert payload["forum_normalized"] == "Иволга"
    assert payload["questions"][0]["forum_normalized"] == "Иволга"


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


def test_fallback_questions_map_grant_expenses_to_reporting_not_travel() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="гранты"),
        "Вопрос по расходам",
    )

    assert [question.text for question in questions] == [
        "Как оформить отчётность по гранту?"
    ]


def test_fallback_questions_map_control_point_to_grant_reporting() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="гранты"),
        "Контрольная точка",
    )

    assert [question.text for question in questions] == [
        "Как оформить отчётность по гранту?"
    ]


def test_fallback_questions_cover_common_navigation_and_recommendation_intents() -> None:
    assert [
        question.text
        for question in build_effective_questions(
            QueryAnalysis(category="общее"),
            "Что такое Росмолодёжь?",
        )
    ] == ["Что такое Росмолодёжь?"]
    assert [
        question.text
        for question in build_effective_questions(
            QueryAnalysis(category="общее"),
            "До свидания, хорошего дня",
        )
    ] == ["Прощание"]
    assert [
        question.text
        for question in build_effective_questions(
            QueryAnalysis(category="рекомендации"),
            "Посоветуйте, какие мероприятия мне подойдут",
        )
    ] == ["Какие мероприятия могут подойти?"]


def test_fallback_questions_prefer_arrival_departure_over_generic_dates() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="форумы", forum_normalized="Российский Север"),
        "Какое время заезда и выезда?",
    )

    assert [question.text for question in questions] == ["Когда заезд и выезд?"]


def test_fallback_questions_map_rejected_application() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="гранты"),
        "Прошу указать причину отклонения заявки на грантовый конкурс.",
    )

    assert [question.text for question in questions] == [
        "Почему отклонили заявку?",
    ]


def test_fallback_questions_map_failed_grant_implementation_to_return_flow() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="гранты"),
        "Ввиду обстоятельств не удаётся реализовать грант.",
    )

    assert [question.text for question in questions] == ["Как вернуть грантовые средства?"]


def test_fallback_questions_map_short_id_issue_to_profile_id_flow() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="техподдержка"),
        "id не вижу",
    )

    assert [question.text for question in questions] == ["Где найти ID профиля?"]


def test_fallback_questions_map_cannot_go_followup_to_decline_not_travel() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="форумы", forum_normalized="Амур"),
        "А что делать, если я уже подтвердил участие, но теперь не могу поехать?",
    )

    assert [question.text for question in questions] == [
        "Как отказаться от участия или отозвать заявку?"
    ]
    assert questions[0].forum_normalized == "Амур"


def test_query_aliases_expand_cannot_go_to_decline_terms() -> None:
    expanded = expand_query_aliases("Подтвердил участие, но не могу поехать")

    assert "отказ от участия" in expanded
    assert "отозвать заявку" in expanded


def test_session_context_restores_forum_for_followup_from_last_five_turns() -> None:
    session = Session(
        user_id="u1",
        channel=Channel.API,
        user_id_hash="hash",
        last_messages=[
            {
                "user": "Подскажи по форуму Амур: как подать заявку?",
                "bot": "Регистрация на форум «Амур» закрыта.",
            }
        ],
    )
    message = "А что делать, если я уже подтвердил участие, но теперь не могу поехать?"
    analysis = apply_session_context(QueryAnalysis(), message, session)

    assert analysis.forum_normalized == "Амур"
    assert analysis.category == "форумы"
    assert build_contextual_message(message, session, analysis).startswith("Амур: ")


def test_session_context_restores_grant_return_for_followup() -> None:
    session = Session(
        user_id="u1",
        channel=Channel.API,
        user_id_hash="hash",
        last_messages=[
            {
                "user": "Как вернуть грантовые средства?",
                "bot": "Для возврата грантовых средств напиши на reportgrant2024@fadm.gov.ru.",
            }
        ],
    )
    message = "А куда именно писать?"
    analysis = apply_session_context(QueryAnalysis(), message, session)

    assert analysis.category == "гранты"
    assert analysis.questions[0].topic == "vernut_denezhnye_sredstva"
    assert build_contextual_message(message, session, analysis).startswith(
        "Как вернуть грантовые средства?"
    )


def test_contextual_message_keeps_previous_topic_for_elliptical_forum_followup() -> None:
    session = Session(
        user_id="u1",
        channel=Channel.API,
        user_id_hash="hash",
        last_messages=[
            {
                "user": "Больше, чем путешествие: расскажи про питание и трансфер.",
                "bot": "Есть трансфер и условия питания зависят от категории.",
            }
        ],
    )
    message = "А если я еду с семьёй, условия такие же?"
    analysis = apply_session_context(QueryAnalysis(), message, session)
    contextual = build_contextual_message(message, session, analysis)

    assert contextual.startswith("Больше, чем путешествие: ")
    assert "расскажи про питание и трансфер" in contextual


def test_effective_questions_ignore_personal_birthdate_as_event_date() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="платформа_фгаис"),
        "Моя дата рождения [ДАТА], где найти ID профиля?",
    )

    assert [question.text for question in questions] == ["Где найти ID профиля?"]


def test_fallback_questions_do_not_match_hotel_marker_inside_wanted_word() -> None:
    questions = build_effective_questions(
        QueryAnalysis(category="форумы", forum_normalized="Арктика. Лёд тронулся"),
        "Арктика. Лёд тронулся Хотели бы поучаствовать в акции",
    )

    assert [question.text for question in questions] == [
        "Как подать заявку или зарегистрироваться?"
    ]


def test_effective_questions_drop_inferred_reimbursement_documents() -> None:
    questions = build_effective_questions(
        QueryAnalysis(
            category="форумы",
            forum_normalized="На волне",
            questions=[
                Question(
                    text="Как происходит возмещение денежных средств за поездку?",
                    category="форумы",
                    forum_normalized="На волне",
                ),
                Question(
                    text="Какие документы нужны для возмещения расходов?",
                    category="форумы",
                    forum_normalized="На волне",
                ),
            ],
        ),
        "На волне возмещение денежных средств на поездку до мероприятия",
    )

    assert [question.text for question in questions] == [
        "Как происходит возмещение денежных средств за поездку?"
    ]


def test_effective_questions_keep_reimbursement_documents_when_user_asked_them() -> None:
    questions = build_effective_questions(
        QueryAnalysis(
            category="форумы",
            forum_normalized="На волне",
            questions=[
                Question(
                    text="Какие документы нужны для возмещения расходов?",
                    category="форумы",
                    forum_normalized="На волне",
                )
            ],
        ),
        "На волне какие документы нужны для возмещения расходов на поездку",
    )

    assert [question.text for question in questions] == [
        "Какие документы нужны для возмещения расходов?"
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
    assert result["analyzer_mode"] == "deterministic"


@pytest.mark.asyncio
async def test_analyze_uses_deterministic_path_for_clear_forum_query() -> None:
    result = await analyze_query(
        {
            "message": "Машук кто оплачивает проезд и какие условия проживания?",
            "message_masked": "Машук кто оплачивает проезд и какие условия проживания?",
            "routing_hint": {"complexity": "complex"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.forum_normalized == "Машук"
    assert analysis.category == "форумы"
    assert analysis.complexity == Complexity.COMPLEX
    assert [question.text for question in analysis.questions] == [
        "Оплачивается ли проезд?",
        "Какие условия проживания?",
    ]
    questions = build_effective_questions(
        analysis,
        "Машук кто оплачивает проезд и какие условия проживания?",
    )
    assert [question.text for question in questions] == [
        "Оплачивается ли проезд?",
        "Какие условия проживания?",
    ]
    assert {question.forum_normalized for question in questions} == {"Машук"}
    assert {question.category for question in questions} == {"форумы"}


@pytest.mark.asyncio
async def test_analyze_uses_deterministic_grant_routing_without_llm() -> None:
    result = await analyze_query(
        {
            "message": "где подать проект на грант",
            "message_masked": "где подать проект на грант",
            "routing_hint": {"complexity": "simple"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.category == "гранты"
    assert analysis.complexity == Complexity.SIMPLE
    assert [question.text for question in analysis.questions] == ["Как подать заявку?"]
    questions = build_effective_questions(analysis, "где подать проект на грант")
    assert [question.text for question in questions] == ["Как подать заявку?"]
    assert questions[0].category == "гранты"
    assert "should_escalate" not in result


@pytest.mark.asyncio
async def test_analyze_routes_application_ui_failure_to_support_without_llm() -> None:
    result = await analyze_query(
        {
            "message": "Не получается выбрать направление в заявке",
            "message_masked": "Не получается выбрать направление в заявке",
            "routing_hint": {"complexity": "complex"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.category == "техподдержка"
    assert analysis.complexity == Complexity.SIMPLE
    assert analysis.forum_normalized is None


@pytest.mark.asyncio
async def test_analyze_routes_explicit_operator_request_to_escalation() -> None:
    result = await analyze_query(
        {
            "message": "Хочу поговорить с оператором",
            "message_masked": "Хочу поговорить с оператором",
            "routing_hint": {"complexity": "simple"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "operator_requested"
    assert analysis.category == "навигация"
    assert analysis.should_escalate is True
    assert analysis.escalation_reason == "operator_requested"
    assert route_after_analyze({"analysis": analysis}) == "escalate"


@pytest.mark.asyncio
async def test_analyze_uses_deterministic_safe_offtopic_without_llm() -> None:
    result = await analyze_query(
        {
            "message": "Какая погода завтра в Москве?",
            "message_masked": "Какая погода завтра в Москве?",
            "routing_hint": {"complexity": "complex"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert "should_escalate" not in result
    assert analysis.category == "offtopic"
    assert analysis.is_offtopic is True
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert route_after_analyze({"analysis": analysis}) == "clarify"


@pytest.mark.asyncio
async def test_analyze_treats_phone_repair_as_safe_offtopic_without_llm() -> None:
    result = await analyze_query(
        {
            "message": "как починить телефон",
            "message_masked": "как починить телефон",
            "routing_hint": {"complexity": "complex"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.category == "offtopic"
    assert analysis.is_offtopic is True
    assert analysis.should_escalate is False
    assert route_after_analyze({"analysis": analysis}) == "clarify"


@pytest.mark.asyncio
async def test_analyze_uses_deterministic_fallback_intent_prefixes_without_llm() -> None:
    tech = await analyze_query(
        {
            "message": "технические вопросы.языки",
            "message_masked": "технические вопросы.языки",
            "routing_hint": {"complexity": "complex"},
            "llm_client": FailingLLM(),
        }
    )
    recommendations = await analyze_query(
        {
            "message": "рекомендации.общие",
            "message_masked": "рекомендации.общие",
            "routing_hint": {"complexity": "complex"},
            "llm_client": FailingLLM(),
        }
    )

    assert tech["analyzer_mode"] == "deterministic"
    assert tech["analysis"].category == "техподдержка"
    assert tech["analysis"].complexity == Complexity.SIMPLE
    assert recommendations["analyzer_mode"] == "deterministic"
    assert recommendations["analysis"].category == "общее"
    assert recommendations["analysis"].complexity == Complexity.SIMPLE


@pytest.mark.asyncio
async def test_analyze_uses_deterministic_common_fallback_intents_without_llm() -> None:
    cases = [
        ("Предложение о сотрудничестве", "общее", False),
        ("Возможности бота / abilities", "общее", False),
        ("Что такое Росмолодёжь?", "платформа_фгаис", False),
        ("Оставить обратную связь о сотрудн", "навигация", False),
        ("Подать заявку на участие", "форумы", True),
    ]

    for message, category, needs_clarification in cases:
        result = await analyze_query(
            {
                "message": message,
                "message_masked": message,
                "routing_hint": {"complexity": "complex"},
                "llm_client": FailingLLM(),
            }
        )

        assert result["analyzer_mode"] == "deterministic"
        assert result["analysis"].category == category
        assert result["analysis"].complexity == Complexity.SIMPLE
        assert result["analysis"].needs_clarification is needs_clarification


@pytest.mark.asyncio
async def test_analyze_uses_deterministic_grant_reporting_without_llm() -> None:
    result = await analyze_query(
        {
            "message": "Оформление отчёта",
            "message_masked": "Оформление отчёта",
            "routing_hint": {"complexity": "simple"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.category == "гранты"
    questions = build_effective_questions(analysis, "Оформление отчёта")
    assert [question.text for question in questions] == [
        "Как оформить отчётность по гранту?"
    ]


@pytest.mark.asyncio
async def test_analyze_routes_project_application_without_forum_to_grants() -> None:
    result = await analyze_query(
        {
            "message": "Где подать заявку на участие в проекте?",
            "message_masked": "Где подать заявку на участие в проекте?",
            "routing_hint": {"complexity": "simple"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.category == "гранты"


@pytest.mark.asyncio
async def test_analyze_keeps_llm_for_unclear_query_when_available() -> None:
    llm = CapturingAnalysisLLM(
        '{"forum": null, "forum_normalized": null, "category": "общее"}'
    )

    result = await analyze_query(
        {
            "message": "какие возможности есть?",
            "message_masked": "какие возможности есть?",
            "routing_hint": {"complexity": "complex"},
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["analysis"].category == "общее"
    assert "analyzer_mode" not in result


@pytest.mark.asyncio
async def test_analyze_forces_forum_category_for_llm_forum_logistics() -> None:
    llm = CapturingAnalysisLLM(
        '{"forum": "Территория смыслов", "forum_normalized": "Территория смыслов", '
        '"category": "платформа_фгаис"}'
    )

    result = await analyze_query(
        {
            "message": "Что нужно подготовить участнику?",
            "message_masked": "Что нужно подготовить участнику?",
            "routing_hint": {"complexity": "complex"},
            "llm_client": llm,
        }
    )

    analysis = result["analysis"]
    assert analysis.forum_normalized == "Территория смыслов"
    assert analysis.category == "форумы"


@pytest.mark.asyncio
async def test_analyze_keeps_platform_category_for_forum_technical_issue() -> None:
    llm = CapturingAnalysisLLM(
        '{"forum": "Территория смыслов", "forum_normalized": "Территория смыслов", '
        '"category": "платформа_фгаис"}'
    )

    result = await analyze_query(
        {
            "message": "Не приходит письмо подтверждение по заявке",
            "message_masked": "Не приходит письмо подтверждение по заявке",
            "routing_hint": {"complexity": "complex"},
            "llm_client": llm,
        }
    )

    analysis = result["analysis"]
    assert analysis.forum_normalized == "Территория смыслов"
    assert analysis.category == "платформа_фгаис"


@pytest.mark.asyncio
async def test_analyze_routes_generic_registration_to_platform_without_llm() -> None:
    result = await analyze_query(
        {
            "message": "Подскажите, пожалуйста, по какой ссылке можно пройти регистрацию?",
            "message_masked": (
                "Подскажите, пожалуйста, по какой ссылке можно пройти регистрацию?"
            ),
            "routing_hint": {"complexity": "simple"},
            "llm_client": FailingLLM(),
        }
    )

    analysis = result["analysis"]
    assert result["analyzer_mode"] == "deterministic"
    assert analysis.category == "платформа_фгаис"
    assert analysis.forum_normalized is None
    questions = build_effective_questions(
        analysis,
        "Подскажите, пожалуйста, по какой ссылке можно пройти регистрацию?",
    )
    assert [question.text for question in questions] == [
        "Как подать заявку или зарегистрироваться?"
    ]
    assert questions[0].category == "платформа_фгаис"


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
            30,
        ),
        (
            "Гранты для физических лиц Подать заявку на участие",
            {"category": "гранты"},
            10,
        ),
        (
            "Гранты для физических лиц Подать заявку на участие",
            {},
            30,
        ),
    ]


@pytest.mark.asyncio
async def test_retrieve_adds_keyword_recall_candidates_on_broad_attempt() -> None:
    retriever = KeywordRecallRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                category="grants",
                questions=[Question(text="Can I upload a grant report after correction?")],
            ),
            "retriever": retriever,
        }
    )

    assert [chunk.chunk_id for chunk in result["retrieved_chunks"]] == [
        "xlsx_exact",
        "ticket_answer_bank_exact"
    ]
    assert retriever.calls == [
        ("Can I upload a grant report after correction?", {"category": "grants"}, 10),
        ("Can I upload a grant report after correction?", {}, 30),
    ]
    assert retriever.keyword_calls == [
        (
            "Can I upload a grant report after correction?",
            {"category": "grants"},
            6,
            2048,
            2.0,
            "xlsx",
        ),
        (
            "Can I upload a grant report after correction?",
            {"category": "grants"},
            6,
            2048,
            2.0,
            "docx",
        ),
        (
            "Can I upload a grant report after correction?",
            {},
            6,
            2048,
            2.0,
            "xlsx",
        ),
        (
            "Can I upload a grant report after correction?",
            {},
            6,
            2048,
            2.0,
            "docx",
        ),
        (
            "Can I upload a grant report after correction?",
            {},
            6,
            2048,
            2.0,
            "ticket_answer_bank",
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
            30,
        ),
        (
            "Какие документы нужны?",
            {"category": "участие"},
            30,
        ),
        (
            "Какие документы нужны?",
            {},
            30,
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
        ("Where is the schedule?", {"forum_normalized": "Forum A"}, 30),
        ("Where is the schedule?", {"category": "forums"}, 30),
        ("Where is the schedule?", {}, 30),
    ]


@pytest.mark.asyncio
async def test_retrieve_stops_after_dense_strict_forum_hit() -> None:
    retriever = ExactForumDenseRetriever()
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
        "forum_exact_0",
        "forum_exact_1",
        "forum_exact_2",
    ]
    assert retriever.calls == [
        ("Where is the schedule?", {"forum_normalized": "Forum A", "category": "forums"}, 10)
    ]


@pytest.mark.asyncio
async def test_retrieve_keeps_broad_attempts_for_multi_aspect_forum_hit() -> None:
    retriever = MultiAspectDenseForumRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                forum_normalized="Forum A",
                category="forums",
                questions=[
                    Question(text="Where is the schedule?"),
                    Question(text="Which documents are needed?"),
                ],
            ),
            "retriever": retriever,
        }
    )

    assert "forum_broad_multi_aspect" in [
        chunk.chunk_id for chunk in result["retrieved_chunks"]
    ]
    assert ("Where is the schedule?", {"forum_normalized": "Forum A"}, 30) in retriever.calls
    assert ("Which documents are needed?", {"forum_normalized": "Forum A"}, 30) in retriever.calls


@pytest.mark.asyncio
async def test_retrieve_keeps_broad_attempts_for_collapsed_multi_aspect_message() -> None:
    retriever = MultiAspectDenseForumRetriever()
    result = await retrieve(
        {
            "analysis": QueryAnalysis(
                forum_normalized="Forum A",
                category="forums",
                questions=[Question(text="What is known from the source?")],
            ),
            "message_masked": "Forum A: место проведения, требования по дресс-коду, магазин.",
            "retriever": retriever,
        }
    )

    assert "forum_broad_multi_aspect" in [
        chunk.chunk_id for chunk in result["retrieved_chunks"]
    ]
    assert (
        "What is known from the source?",
        {"forum_normalized": "Forum A"},
        30,
    ) in retriever.calls


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
        lambda: SimpleNamespace(
            ml_unload_after_use=False,
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
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


@pytest.mark.asyncio
async def test_rerank_uses_source_only_fast_path_for_exact_forum_scope(
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
            chunk_id="amur_docs",
            text="Документы для форума Амур.",
            metadata={
                "intent_name": "Документы",
                "category": "форумы",
                "forum_normalized": "Амур",
            },
            score=0.96,
        ),
        Chunk(
            chunk_id="amur_transfer",
            text="Трансфер для форума Амур.",
            metadata={
                "intent_name": "Трансфер",
                "category": "форумы",
                "forum_normalized": "Амур",
            },
            score=0.9,
        ),
        Chunk(
            chunk_id="amur_age",
            text="Возраст участников форума Амур.",
            metadata={
                "intent_name": "Возраст участников",
                "category": "форумы",
                "forum_normalized": "Амур",
            },
            score=0.9,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Амур: документы, трансфер и возраст участников?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Амур",
                questions=[
                    Question(text="Какие документы нужны?"),
                    Question(text="Есть ли трансфер?"),
                    Question(text="Какой возраст участников?"),
                ],
            ),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    chunk_ids = {chunk.chunk_id for chunk in result["reranked_chunks"]}
    assert {"amur_docs", "amur_transfer", "amur_age"} <= chunk_ids
    assert result["max_confidence"] == 0.7


@pytest.mark.asyncio
async def test_rerank_pins_original_exact_match_for_multi_question(
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
    distractors = [
        Chunk(
            chunk_id=f"distractor_{index}",
            text=f"Distractor {index}",
            metadata={"intent_examples": [f"other example {index}"]},
            score=1.0,
        )
        for index in range(5)
    ]
    exact = Chunk(
        chunk_id="ticket_answer_bank_exact",
        text="Exact source answer.",
        metadata={
            "source_type": "ticket_answer_bank",
            "intent_examples": ["original complex question"],
        },
        score=0.1,
    )

    result = await rerank(
        {
            "message_masked": "original complex question",
            "analysis": QueryAnalysis(
                questions=[
                    Question(text="first aspect"),
                    Question(text="second aspect"),
                ]
            ),
            "retrieved_chunks": [*distractors, exact],
            "reranker": InputOrderGroupReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "ticket_answer_bank_exact"
    assert result["reranked_chunks"][0].reranker_score >= 0.7


@pytest.mark.asyncio
async def test_rerank_keeps_exact_platform_registration_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(ml_unload_after_use=False, reranker_threshold_low=0.4),
    )
    chunks = [
        Chunk(
            chunk_id="platform_navigation",
            text="Можно найти мероприятия и подать заявку на подходящее событие.",
            metadata={
                "category": "платформа_фгаис",
                "topic": "napravleniya_i_cennosti_rosmola",
            },
            score=0.9,
        ),
        Chunk(
            chunk_id="fgais_registration",
            text="Пройти регистрацию в ФГАИС можно по ссылке: https://myrosmol.ru/auth/register",
            metadata={
                "category": "платформа_фгаис",
                "topic": "kak_zaregistrirovatsya_na_fgais",
                "intent_name": "Как зарегистрироваться на ФГАИС",
            },
            score=0.8,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "По какой ссылке можно пройти регистрацию?",
            "analysis": QueryAnalysis(
                category="платформа_фгаис",
                questions=[
                    Question(
                        text="Как подать заявку или зарегистрироваться?",
                        category="платформа_фгаис",
                    )
                ],
            ),
            "retrieved_chunks": chunks,
            "reranker": DroppingExactReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "fgais_registration"
    assert "fgais_registration" in [chunk.chunk_id for chunk in result["reranked_chunks"]]


@pytest.mark.asyncio
async def test_rerank_pins_answer_bank_intent_example_match(
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
            chunk_id="generic_grant",
            text="По вопросам грантового конкурса используйте личный кабинет.",
            metadata={"category": "гранты"},
            score=0.95,
        ),
        Chunk(
            chunk_id="ticket_answer_bank_001",
            text="Свяжитесь с куратором грантового конкурса.",
            metadata={
                "category": "гранты",
                "source_type": "ticket_answer_bank",
                "intent_examples": ["Как получить консультацию по отчетности?"],
            },
            score=0.4,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Как получить консультацию по отчетности?",
            "analysis": QueryAnalysis(
                category="гранты",
                questions=[Question(text="Как получить консультацию по отчетности?")],
            ),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "ticket_answer_bank_001"
    assert result["reranked_chunks"][0].reranker_score == 0.7
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_rerank_keeps_answer_bank_match_across_category_scope(
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
            chunk_id="technical_profile",
            text="Если не открывается личный кабинет, очистите кэш браузера.",
            metadata={"category": "техподдержка"},
            score=0.9,
        ),
        Chunk(
            chunk_id="ticket_answer_bank_002",
            text="Для консультации по грантовой отчетности обратитесь к куратору конкурса.",
            metadata={
                "category": "гранты",
                "source_type": "ticket_answer_bank",
                "intent_examples": ["К кому обратиться по грантовой отчетности?"],
            },
            score=0.3,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "К кому обратиться по грантовой отчетности?",
            "analysis": QueryAnalysis(
                category="техподдержка",
                questions=[Question(text="К кому обратиться по грантовой отчетности?")],
            ),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "ticket_answer_bank_002"
    assert result["reranked_chunks"][0].reranker_score == 0.7


@pytest.mark.asyncio
async def test_rerank_uses_original_query_for_answer_bank_priority(
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
            chunk_id="generic_reporting",
            text="Для отчётности приложите документы, подтверждающие расходы.",
            metadata={"category": "гранты"},
            score=0.95,
        ),
        Chunk(
            chunk_id="ticket_answer_bank_expenses",
            text="В отчёте по расходам приложите договор, накладную, акт и подтверждение оплаты.",
            metadata={
                "category": "гранты",
                "source_type": "ticket_answer_bank",
                "intent_examples": ["Вопрос по расходам"],
            },
            score=0.3,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Вопрос по расходам",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "ticket_answer_bank_expenses"
    assert result["reranked_chunks"][0].reranker_score == 0.7


@pytest.mark.asyncio
async def test_rerank_prefers_exact_intent_example_over_broad_overlap(
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
            chunk_id="broad_rejection",
            text="Причины отклонения заявки отображаются в личном кабинете.",
            metadata={
                "category": "гранты",
                "intent_examples": [
                    "Можно узнать причину отклонения?",
                    "Прошу помочь разобраться в причине отклонения заявки",
                ],
            },
            score=0.9,
        ),
        Chunk(
            chunk_id="ticket_answer_bank_rejection",
            text="Причину отклонения грантовой заявки можно посмотреть в карточке проекта.",
            metadata={
                "category": "гранты",
                "source_type": "ticket_answer_bank",
                "intent_examples": [
                    "Прошу указать причину отклонения заявки на грантовый конкурс."
                ],
            },
            score=0.5,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Прошу указать причину отклонения заявки на грантовый конкурс.",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "ticket_answer_bank_rejection"


@pytest.mark.asyncio
async def test_rerank_prefers_forum_category_intersection(
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
            chunk_id="forum_registration",
            text="Forum registration status answer.",
            metadata={
                "forum_normalized": "Forum A",
                "category": "forums",
                "topic": "registration",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="platform_registration",
            text="Platform profile registration answer.",
            metadata={
                "forum_normalized": "Forum A",
                "category": "platform",
                "topic": "registration",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "How do I fix platform registration for Forum A?",
            "analysis": QueryAnalysis(
                forum_normalized="Forum A",
                category="platform",
                questions=[
                    Question(
                        text="How do I fix platform registration?",
                        forum_normalized="Forum A",
                        category="platform",
                    )
                ],
            ),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert [chunk.chunk_id for chunk in result["reranked_chunks"]] == [
        "platform_registration",
    ]


@pytest.mark.asyncio
async def test_rerank_keeps_same_forum_different_category_candidate(
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
            chunk_id="forum_status",
            text="Forum application status answer.",
            metadata={
                "forum_normalized": "Forum A",
                "category": "форумы",
                "topic": "status",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="forum_platform_exact",
            text="Forum platform account answer.",
            metadata={
                "forum_normalized": "Forum A",
                "category": "платформа_фгаис",
                "source_type": "ticket_answer_bank",
                "intent_examples": ["I cannot update my platform account for Forum A"],
            },
            score=0.6,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "I cannot update my platform account for Forum A",
            "analysis": QueryAnalysis(
                forum_normalized="Forum A",
                category="форумы",
                questions=[
                    Question(
                        text="I cannot update my platform account for Forum A",
                        forum_normalized="Forum A",
                        category="форумы",
                    )
                ],
            ),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "forum_platform_exact"


@pytest.mark.asyncio
async def test_rerank_keeps_compatible_fallback_category_candidate(
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
            chunk_id="generic_abilities",
            text="Я могу подсказать по форумам и грантам.",
            metadata={
                "category": "общее",
                "topic": "vozmozhnosti_bota",
                "intent_name": "Возможности бота",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="what_is_rosmol",
            text="Росмолодёжь поддерживает молодёжные инициативы.",
            metadata={
                "category": "платформа_фгаис",
                "source_category": "fallback",
                "topic": "chto_takoe_rosmolodezh",
                "intent_name": "Что такое Росмолодёжь?",
            },
            score=0.5,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Что такое Росмолодёжь?",
            "analysis": QueryAnalysis(category="общее"),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "what_is_rosmol"
    assert result["reranked_chunks"][0].reranker_score >= 0.7


@pytest.mark.asyncio
async def test_rerank_keeps_platform_status_source_for_grant_status_question(
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
            chunk_id="grant_results",
            text="Результаты грантового конкурса публикуются после отбора.",
            metadata={
                "category": "гранты",
                "topic": "rezultaty_rm",
                "intent_name": "Результаты РМ",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="grant_status_location",
            text="Статус заявки можно посмотреть в личном кабинете.",
            metadata={
                "category": "платформа_фгаис",
                "source_category": "fallback",
                "topic": "gde_smotret_status_zayavok_v",
                "intent_name": "Где смотреть статус заявок в",
            },
            score=0.5,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Где посмотреть статус заявки на грант?",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "grant_status_location"
    assert result["reranked_chunks"][0].reranker_score >= 0.7


@pytest.mark.asyncio
async def test_rerank_short_circuits_promotable_priority_without_cross_encoder(
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
            chunk_id="generic_status",
            text="Статусы публикуются после отбора.",
            metadata={"category": "гранты", "topic": "rezultaty_rm"},
            score=1.0,
        ),
        Chunk(
            chunk_id="status_location",
            text="Статус заявки можно посмотреть в личном кабинете.",
            metadata={
                "category": "платформа_фгаис",
                "source_category": "fallback",
                "topic": "gde_smotret_status_zayavok_v",
                "intent_name": "Где смотреть статус заявок",
            },
            score=0.5,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Где смотреть статус заявок",
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "status_location"
    assert result["reranked_chunks"][0].reranker_score >= 0.7


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


def test_rerank_candidate_prefilter_penalizes_forum_grant_for_unscoped_grant_query() -> None:
    chunks = [
        Chunk(
            chunk_id="forum_grant",
            text="В рамках форума будет грантовый конкурс.",
            metadata={
                "category": "гранты",
                "source_category": "Добрино",
                "intent_name": "Условия и сроки участия_гранты",
                "intent_examples": [
                    "подскажите пжл когда будет ближайший грантовый конкурс для физических лиц",
                    "могу ли я подать на грант свой проект?",
                ],
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="generic_grant_change",
            text="Чтобы внести изменения в проект, напишите на почту грантового конкурса.",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Внести изменения в проект",
                "topic": "vnesti_izmeneniya_v_proekt",
            },
            score=0.2,
        ),
    ]

    candidates = _candidate_chunks_for_question(
        "Гранты для физических лиц Внести изменения в проект",
        chunks,
        2,
    )

    assert candidates[0].chunk_id == "generic_grant_change"


@pytest.mark.asyncio
async def test_rerank_promotes_generic_grant_project_change_before_cross_encoder() -> None:
    chunks = [
        Chunk(
            chunk_id="forum_grant_terms",
            text="В рамках форума будет проводиться грантовый конкурс.",
            metadata={
                "category": "гранты",
                "source_category": "Добрино",
                "intent_name": "Условия и сроки участия_гранты",
                "intent_examples": ["Могу ли я подать на грант свой проект?"],
                "source_type": "xlsx",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="generic_grant_change",
            text="Нужно изменить смету? Напиши на почту grant2024@fadm.gov.ru.",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Внести изменения в проект",
                "topic": "vnesti_izmeneniya_v_proekt",
                "source_type": "xlsx",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Гранты для физических лиц Внести изменения в проект",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "generic_grant_change"
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_rerank_promotes_feedback_source_before_cross_encoder() -> None:
    chunks = [
        Chunk(
            chunk_id="grant_terms",
            text="Условия участия в грантовом конкурсе.",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Условия и сроки участия",
                "source_type": "xlsx",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="feedback_organizer",
            text="Поделись своими впечатлениями, а я передам информацию организаторам.",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Оставить обратную связь организаторам",
                "topic": "ostavit_obratnuyu_svyaz_o",
                "source_type": "xlsx",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Гранты для физических лиц Оставить обратную связь организаторам",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "feedback_organizer"
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_rerank_promotes_expert_feedback_before_leave_feedback() -> None:
    chunks = [
        Chunk(
            chunk_id="feedback_organizer",
            text="Поделись своими впечатлениями, а я передам информацию организаторам.",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Оставить обратную связь организаторам",
                "topic": "ostavit_obratnuyu_svyaz_o",
                "source_type": "xlsx",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="expert_feedback",
            text="Чтобы получить обратную связь по заявке, зайди в профиль и выбери «Мои заявки».",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Запрос обратной связи куратора",
                "topic": "zapros_obratnoy_svyazi_kuratora",
                "source_type": "xlsx",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": (
                "предоставить подробную обратную связь по результатам "
                "экспертной оценки моей заявки"
            ),
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "expert_feedback"
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_rerank_promotes_password_recovery_before_cross_encoder() -> None:
    chunks = [
        Chunk(
            chunk_id="generic_support",
            text="Если возникла техническая ошибка, обратитесь в поддержку.",
            metadata={
                "category": "платформа_фгаис",
                "topic": "tehnicheskaya_oshibka",
                "source_type": "xlsx",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="password_recovery",
            text=(
                "Чтобы восстановить пароль, перейди по ссылке входа "
                "и нажми «Восстановить пароль»."
            ),
            metadata={
                "category": "платформа_фгаис",
                "source_category": "fallback",
                "intent_name": "Восстановить пароль",
                "topic": "vosstanovit_parol",
                "source_type": "xlsx",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Восстановить пароль",
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "password_recovery"
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_rerank_promotes_generic_grant_application_before_forum_application() -> None:
    chunks = [
        Chunk(
            chunk_id="forum_application",
            text="Чтобы подать заявку на форум, выбери событие на сайте.",
            metadata={
                "category": "гранты",
                "source_category": "Утро",
                "intent_name": "Подача заявки на проект",
                "topic": "podacha_zayavki_na_proekt",
                "intent_examples": ["Как подать заявку на участие в конкурсе"],
                "source_type": "xlsx",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="generic_grant_application",
            text="Чтобы подать заявку на участие в гранте, выбери номинацию конкурса.",
            metadata={
                "category": "гранты",
                "source_category": "Гранты для физических лиц",
                "intent_name": "Подать заявку на участие",
                "topic": "podat_zayavku_na_uchastie",
                "source_type": "xlsx",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Гранты для физических лиц Подать заявку на участие",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "generic_grant_application"
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_rerank_promotes_forum_invitation_letter_before_cross_encoder() -> None:
    chunks = [
        Chunk(
            chunk_id="ivolga_transfer",
            text="Трансфер на форум Иволга будет организован от точки сбора.",
            metadata={
                "category": "форумы",
                "forum_normalized": "Иволга",
                "source_category": "Иволга",
                "intent_name": "Трансфер",
                "topic": "transfer_do_mesta_provedeniya_meropriyatiya",
                "source_type": "xlsx",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="ivolga_invitation",
            text="Письмо-вызов можно получить по запросу после заполнения формы.",
            metadata={
                "category": "форумы",
                "forum_normalized": "Иволга",
                "source_category": "Иволга",
                "intent_name": "Письмо-вызов",
                "topic": "pismo_vyzov",
                "source_type": "xlsx",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "Иволга Письмо-вызов",
            "analysis": QueryAnalysis(category="форумы", forum_normalized="Иволга"),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "ivolga_invitation"
    assert result["max_confidence"] >= 0.7


def test_rerank_candidate_prefilter_prefers_same_forum_specific_topic() -> None:
    chunks = [
        Chunk(
            chunk_id="forum_food",
            text="Food, water and cafe locations for Forum A participants.",
            metadata={
                "forum_normalized": "Forum A",
                "topic": "food",
                "intent_name": "Food conditions",
                "source_category": "Forum A",
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="forum_transfer",
            text="Transfer information for Forum A participants.",
            metadata={
                "forum_normalized": "Forum A",
                "topic": "transfer_to_venue",
                "intent_name": "Transfer to venue",
                "source_category": "Forum A",
            },
            score=0.1,
        ),
    ]

    candidates = _candidate_chunks_for_question("How do I get transfer to the venue?", chunks, 2)

    assert [chunk.chunk_id for chunk in candidates] == ["forum_transfer", "forum_food"]


def test_rerank_candidate_prefilter_penalizes_generic_fallback() -> None:
    chunks = [
        Chunk(
            chunk_id="generic_forum",
            text="Forum A registration, transfer, food, documents and program details.",
            metadata={
                "forum_normalized": "Forum A",
                "topic": "general",
                "intent_name": "Forum A fallback",
                "source_category": "fallback",
                "is_generic": True,
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="specific_transfer",
            text="Transfer details for Forum A.",
            metadata={
                "forum_normalized": "Forum A",
                "topic": "transfer_to_venue",
                "intent_name": "Transfer to venue",
                "source_category": "Forum A",
                "is_generic": False,
            },
            score=0.1,
        ),
    ]

    candidates = _candidate_chunks_for_question("How do I get transfer to the venue?", chunks, 2)

    assert [chunk.chunk_id for chunk in candidates] == ["specific_transfer", "generic_forum"]


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
async def test_rerank_keeps_second_candidate_for_multi_aspect_question(
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

    docs_word = "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442"
    age_word = "\u0432\u043e\u0437\u0440\u0430\u0441\u0442"

    class TwoCandidateReranker:
        def rerank_groups(
            self,
            groups: list[tuple[str, list[Chunk], int]],
        ) -> list[list[ScoredChunk]]:
            return [self.rerank(query, chunks, top_k) for query, chunks, top_k in groups]

        def rerank(self, query: str, chunks: list[Chunk], top_k: int) -> list[ScoredChunk]:
            query_lower = query.casefold()
            if docs_word in query_lower:
                order = ["docs_main", "docs_extra", "age"]
            elif age_word in query_lower:
                order = ["age", "docs_main", "docs_extra"]
            else:
                order = ["docs_main", "age", "docs_extra"]
            by_id = {chunk.chunk_id: chunk for chunk in chunks}
            ranked = [by_id[chunk_id] for chunk_id in order if chunk_id in by_id]
            return [
                ScoredChunk(
                    **chunk.model_dump(exclude={"score"}),
                    score=chunk.score,
                    reranker_score=0.9 - index * 0.1,
                )
                for index, chunk in enumerate(ranked[:top_k])
            ]

    chunks = [
        Chunk(
            chunk_id="docs_main",
            text=f"{docs_word}: passport.",
            metadata={"intent_name": docs_word},
            score=0.8,
        ),
        Chunk(
            chunk_id="docs_extra",
            text=f"{docs_word}: medical certificate.",
            metadata={"intent_name": docs_word},
            score=0.7,
        ),
        Chunk(
            chunk_id="age",
            text=f"{age_word}: 14-35.",
            metadata={"intent_name": age_word},
            score=0.6,
        ),
    ]

    result = await rerank(
        {
            "message_masked": f"{docs_word} \u0438 {age_word}?",
            "analysis": QueryAnalysis(questions=[]),
            "retrieved_chunks": chunks,
            "reranker": TwoCandidateReranker(),
        }
    )

    assert "docs_extra" in [chunk.chunk_id for chunk in result["reranked_chunks"]]


@pytest.mark.asyncio
async def test_rerank_pins_topic_candidates_for_forum_multi_aspect(
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
    forum = "ГосСтарт"
    chunks = [
        Chunk(
            chunk_id="neighbor_volunteers",
            text="Волонтёры помогают на площадке форума.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "xlsx",
                "topic": "volontery_foruma",
            },
            score=0.99,
        ),
        Chunk(
            chunk_id="confirmation",
            text="Подтверждение участия проходит в личном кабинете.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "xlsx",
                "topic": "podtverzhdenie_uchastiya_i_org_momenty",
            },
            score=0.2,
        ),
        Chunk(
            chunk_id="digital_week",
            text="Цифровая неделя — это онлайн-этап перед очным мероприятием.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "xlsx",
                "topic": "cifrovaya_nedelya",
            },
            score=0.2,
        ),
        Chunk(
            chunk_id="dates",
            text="Даты начала мероприятия указаны в карточке форума.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "xlsx",
                "topic": "daty_nachala_meropriyatiya",
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": (
                "ГосСтарт: что с подтверждением участия, "
                "что такое цифровая неделя и когда начинается мероприятие?"
            ),
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized=forum,
                complexity=Complexity.COMPLEX,
            ),
            "retrieved_chunks": chunks,
            "reranker": FailingReranker(),
        }
    )

    chunk_ids = {chunk.chunk_id for chunk in result["reranked_chunks"]}
    assert {"confirmation", "digital_week", "dates"} <= chunk_ids
    assert "neighbor_volunteers" not in [chunk.chunk_id for chunk in result["reranked_chunks"][:3]]


@pytest.mark.asyncio
async def test_rerank_prefers_official_forum_source_over_answer_bank(
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

    forum = "BCT"
    category = "\u0444\u043e\u0440\u0443\u043c\u044b"
    registration = "\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f"
    chunks = [
        Chunk(
            chunk_id="ticket_answer_bank_account",
            text=f"{registration}: account owner request.",
            metadata={
                "forum_normalized": forum,
                "category": category,
                "source_type": "ticket_answer_bank",
                "intent_name": registration,
                "intent_examples": [registration],
            },
            score=1.0,
        ),
        Chunk(
            chunk_id="official_registration",
            text=f"{registration}: official festival dates.",
            metadata={
                "forum_normalized": forum,
                "category": category,
                "source_type": "docx",
                "intent_name": registration,
            },
            score=0.2,
        ),
    ]

    result = await rerank(
        {
            "message_masked": registration,
            "analysis": QueryAnalysis(
                forum_normalized=forum,
                category=category,
                questions=[
                    Question(text=registration, forum_normalized=forum, category=category)
                ],
            ),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    chunk_ids = [chunk.chunk_id for chunk in result["reranked_chunks"]]
    assert chunk_ids[0] == "official_registration"
    assert "ticket_answer_bank_account" not in chunk_ids


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
async def test_rerank_scopes_candidates_to_analysis_category(
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
            chunk_id="forum_application",
            text="Подать заявку на Тавриду можно на сайте события.",
            metadata={"category": "форумы", "forum_normalized": "Таврида"},
            score=0.9,
        ),
        Chunk(
            chunk_id="grant_application",
            text="Чтобы подать заявку на грант, заполните проектную форму на ФГАИС.",
            metadata={"category": "гранты"},
            score=0.8,
        ),
    ]

    result = await rerank(
        {
            "message_masked": "где подать проект на грант",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": chunks,
            "reranker": InputOrderReranker(),
        }
    )

    assert [chunk.chunk_id for chunk in result["reranked_chunks"]] == ["grant_application"]


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
        text="Проезд на форум оплачивает организатор.",
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

    assert result["generated_response"] == "Проезд на форум оплачивает организатор. [src:ctx_1]"
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ctx_1"]


@pytest.mark.asyncio
async def test_generate_synthesizes_multi_aspect_answer_from_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    apply_chunk = ScoredChunk(
        chunk_id="apply",
        text="Регистрация на форум закрыта, даты приёма заявок объявят позже.",
        metadata={"chunk_id": "apply", "category": "форумы", "topic": "podacha_zayavki"},
        score=0.9,
        reranker_score=0.9,
    )
    travel_chunk = ScoredChunk(
        chunk_id="travel",
        text="Проезд обычно оплачивает направляющая сторона или сам участник.",
        metadata={"chunk_id": "travel", "category": "форумы", "topic": "oplata_proezda"},
        score=0.8,
        reranker_score=0.82,
    )
    llm = CapturingLLM(
        "Регистрация закрыта, даты объявят позже. [src:apply]\n\n"
        "Проезд обычно оплачивает направляющая сторона или сам участник. [src:travel]"
    )

    result = await generate(
        {
            "message_masked": "Как подать заявку и оплачивается ли проезд?",
            "analysis": QueryAnalysis(
                category="форумы",
                questions=[
                    Question(text="Как подать заявку?", category="форумы"),
                    Question(text="Кто оплачивает проезд?", category="форумы"),
                ],
            ),
            "reranked_chunks": [apply_chunk, travel_chunk],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert llm.kwargs[0]["model"] == "GigaChat/GigaChat-2-Max"
    assert result["generator_model"] == "GigaChat/GigaChat-2-Max"
    assert result["generated_response"].startswith("Регистрация закрыта")
    assert result["cited_sources"] == ["apply", "travel"]


@pytest.mark.asyncio
async def test_generate_falls_back_to_sources_when_llm_omits_multi_aspect_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum = "Больше, чем путешествие"
    transfer = ScoredChunk(
        chunk_id="transfer",
        text="Трансфер до площадки фестиваля будет организован для участников.",
        metadata={
            "category": "форумы",
            "forum_normalized": forum,
            "source_type": "xlsx",
            "topic": "transfer",
            "intent_name": "Трансфер до места проведения мероприятия",
        },
        score=0.8,
        reranker_score=0.7,
    )
    food = ScoredChunk(
        chunk_id="food",
        text="На площадке фестиваля предусмотрены питание и питьевая вода.",
        metadata={
            "category": "форумы",
            "forum_normalized": forum,
            "source_type": "xlsx",
            "topic": "pitanie_i_pite",
            "intent_name": "Питание и питье",
        },
        score=0.78,
        reranker_score=0.7,
    )
    llm = CapturingLLM("Трансфер будет организован для участников. [src:transfer]")

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized=forum,
                questions=[
                    Question(text="Есть ли трансфер?", category="форумы", forum_normalized=forum),
                    Question(text="Есть ли питание?", category="форумы", forum_normalized=forum),
                ],
            ),
            "message_masked": (
                "Больше, чем путешествие: если я еду с семьёй, будет ли питание и трансфер?"
            ),
            "reranked_chunks": [transfer, food],
            "max_confidence": 0.7,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["transfer", "food"]
    assert "Трансфер до площадки фестиваля" in result["generated_response"]
    assert "питание и питьевая вода" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_falls_back_to_sources_when_llm_claims_insufficient_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    application = ScoredChunk(
        chunk_id="application",
        text="Регистрация на форум закрыта, даты объявят позже.",
        metadata={
            "category": "форумы",
            "source_type": "xlsx",
            "topic": "podacha_zayavki_na_proekt",
        },
        score=0.8,
        reranker_score=0.7,
    )
    decline = ScoredChunk(
        chunk_id="decline",
        text="Если не можешь поехать после подтверждения, сообщи нам.",
        metadata={
            "category": "форумы",
            "source_type": "xlsx",
            "topic": "otkaz_ot_uchastiya",
        },
        score=0.8,
        reranker_score=0.7,
    )
    llm = CapturingLLM(
        "Из представленных источников невозможно ответить на часть вопроса. "
        "[src:application] [src:decline]"
    )

    result = await generate(
        {
            "message_masked": "Как подать заявку и что делать, если не могу поехать?",
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                questions=[
                    Question(text="Как подать заявку?", category="форумы"),
                    Question(text="Как отказаться от участия?", category="форумы"),
                ],
            ),
            "reranked_chunks": [application, decline],
            "max_confidence": 0.7,
            "llm_client": llm,
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["application", "decline"]
    assert "Передаю обращение" not in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_synthesizes_simple_multi_aspect_answer_with_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    apply_chunk = ScoredChunk(
        chunk_id="apply",
        text="Регистрация на форум закрыта, даты приёма заявок объявят позже.",
        metadata={"chunk_id": "apply", "category": "форумы", "topic": "podacha_zayavki"},
        score=0.9,
        reranker_score=0.9,
    )
    travel_chunk = ScoredChunk(
        chunk_id="travel",
        text="Проезд обычно оплачивает направляющая сторона или сам участник.",
        metadata={"chunk_id": "travel", "category": "форумы", "topic": "oplata_proezda"},
        score=0.8,
        reranker_score=0.82,
    )
    llm = CapturingLLM(
        "Регистрация закрыта, даты объявят позже. [src:apply]\n\n"
        "Проезд обычно оплачивает направляющая сторона или сам участник. [src:travel]"
    )

    result = await generate(
        {
            "message_masked": "Как подать заявку и оплачивается ли проезд?",
            "analysis": QueryAnalysis(
                category="форумы",
                complexity=Complexity.SIMPLE,
                questions=[
                    Question(text="Как подать заявку?", category="форумы"),
                    Question(text="Кто оплачивает проезд?", category="форумы"),
                ],
            ),
            "reranked_chunks": [apply_chunk, travel_chunk],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert llm.kwargs[0]["model"] == "GigaChat/GigaChat-2-Max"
    assert result["generator_model"] != "source_chunk"
    assert result["generated_response"].startswith("Регистрация закрыта")
    assert result["cited_sources"] == ["apply", "travel"]


@pytest.mark.asyncio
async def test_generate_synthesizes_contextual_followup_from_single_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    decline_chunk = ScoredChunk(
        chunk_id="decline",
        text=(
            "Сейчас регистрация на мероприятие ещё не доступна.\n"
            "Если ты успешно пройдёшь конкурсный отбор, но затем решишь отказаться "
            "от участия — пожалуйста, сообщи нам. Мы обязательно поможем!"
        ),
        metadata={
            "chunk_id": "decline",
            "category": "форумы",
            "topic": "otkaz_ot_uchastiya",
        },
        score=0.86,
        reranker_score=0.86,
    )
    llm = CapturingLLM(
        "Если уже подтвердил участие, но не можешь поехать, сообщите организаторам — "
        "мы поможем с отказом от участия. [src:decline]"
    )

    result = await generate(
        {
            "message_masked": (
                "А что делать, если я уже подтвердил участие, но теперь не могу поехать?"
            ),
            "contextual_message": (
                "Контекст предыдущего вопроса: форум «Амур».\n"
                "А что делать, если я уже подтвердил участие, но теперь не могу поехать?"
            ),
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Амур",
                questions=[
                    Question(
                        text="Что делать, если подтвердил участие, но не могу поехать?",
                        category="форумы",
                        forum_normalized="Амур",
                    )
                ],
            ),
            "reranked_chunks": [decline_chunk],
            "max_confidence": 0.86,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["generated_response"] == (
        "Если уже подтвердил участие, но не можешь поехать, сообщи нам — "
        "мы поможем с отказом от участия. [src:decline]"
    )
    assert result["cited_sources"] == ["decline"]


@pytest.mark.asyncio
async def test_generate_uses_masked_message_when_analysis_has_no_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="official_exact",
        text="Официальный ответ из базы.",
        metadata={
            "chunk_id": "official_exact",
            "source_type": "xlsx",
            "intent_examples": ["Как создать кабинет организации?"],
        },
        score=1.0,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Как создать кабинет организации?",
            "analysis": QueryAnalysis(category="платформа_фгаис", questions=[]),
            "reranked_chunks": [chunk],
            "max_confidence": 0.7,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == "Официальный ответ из базы. [src:official_exact]"
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["official_exact"]


@pytest.mark.asyncio
async def test_generate_trusts_high_confidence_official_source_for_single_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="official_high_confidence",
        text="Официальный ответ из утверждённой базы.",
        metadata={
            "chunk_id": "official_high_confidence",
            "source_type": "xlsx",
        },
        score=1.0,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Нестандартная формулировка пользователя",
            "analysis": QueryAnalysis(category="общее", questions=[]),
            "reranked_chunks": [chunk],
            "max_confidence": 0.7,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "Официальный ответ из утверждённой базы. [src:official_high_confidence]"
    )
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["official_high_confidence"]


@pytest.mark.asyncio
async def test_generate_trusts_high_confidence_official_source_before_category_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    exact_fallback = ScoredChunk(
        chunk_id="official_fallback",
        text="Официальный fallback-ответ из Excel.",
        metadata={
            "chunk_id": "official_fallback",
            "source_type": "xlsx",
            "category": "общее",
        },
        score=1.0,
        reranker_score=0.7,
    )
    scoped_but_weak = ScoredChunk(
        chunk_id="platform_generic",
        text="Общий ответ по платформе.",
        metadata={
            "chunk_id": "platform_generic",
            "source_type": "xlsx",
            "category": "платформа_фгаис",
        },
        score=0.2,
        reranker_score=0.01,
    )

    result = await generate(
        {
            "message_masked": "Как создать кабинет организации?",
            "analysis": QueryAnalysis(category="платформа_фгаис", questions=[]),
            "reranked_chunks": [exact_fallback, scoped_but_weak],
            "max_confidence": 0.7,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "Официальный fallback-ответ из Excel. [src:official_fallback]"
    )
    assert result["cited_sources"] == ["official_fallback"]


@pytest.mark.asyncio
async def test_generate_uses_answer_bank_intent_examples_for_source_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    broad_chunk = ScoredChunk(
        chunk_id="generic_grant",
        text="По вопросам грантового конкурса используйте личный кабинет.",
        metadata={"chunk_id": "generic_grant", "category": "гранты"},
        score=0.95,
        reranker_score=0.95,
    )
    answer_bank_chunk = ScoredChunk(
        chunk_id="ticket_answer_bank_001",
        text="Свяжитесь с куратором грантового конкурса.",
        metadata={
            "chunk_id": "ticket_answer_bank_001",
            "source_type": "ticket_answer_bank",
            "intent_examples": ["Как получить консультацию по отчетности?"],
        },
        score=0.9,
        reranker_score=0.82,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                questions=[Question(text="Как получить консультацию по отчетности?")]
            ),
            "reranked_chunks": [broad_chunk, answer_bank_chunk],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "Свяжитесь с куратором грантового конкурса. [src:ticket_answer_bank_001]"
    )
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ticket_answer_bank_001"]


@pytest.mark.asyncio
async def test_generate_uses_original_query_for_answer_bank_source_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    broad_chunk = ScoredChunk(
        chunk_id="generic_reporting",
        text="Для отчётности приложите документы, подтверждающие расходы.",
        metadata={"chunk_id": "generic_reporting", "category": "гранты"},
        score=1.0,
        reranker_score=0.96,
    )
    answer_bank_chunk = ScoredChunk(
        chunk_id="ticket_answer_bank_expenses",
        text="В отчёте по расходам приложите договор, накладную, акт и подтверждение оплаты.",
        metadata={
            "chunk_id": "ticket_answer_bank_expenses",
            "category": "гранты",
            "source_type": "ticket_answer_bank",
            "intent_examples": ["Вопрос по расходам"],
        },
        score=0.7,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Вопрос по расходам",
            "analysis": QueryAnalysis(category="гранты"),
            "reranked_chunks": [broad_chunk, answer_bank_chunk],
            "max_confidence": 0.96,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "В отчёте по расходам приложите договор, накладную, акт и подтверждение "
        "оплаты. [src:ticket_answer_bank_expenses]"
    )
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ticket_answer_bank_expenses"]


@pytest.mark.asyncio
async def test_generate_prefers_exact_intent_example_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    broad_chunk = ScoredChunk(
        chunk_id="broad_rejection",
        text="Причины отклонения заявки отображаются в личном кабинете.",
        metadata={
            "chunk_id": "broad_rejection",
            "category": "гранты",
            "intent_examples": ["Можно узнать причину отклонения?"],
        },
        score=1.0,
        reranker_score=0.95,
    )
    exact_chunk = ScoredChunk(
        chunk_id="ticket_answer_bank_rejection",
        text="Причину отклонения грантовой заявки можно посмотреть в карточке проекта.",
        metadata={
            "chunk_id": "ticket_answer_bank_rejection",
            "category": "гранты",
            "source_type": "ticket_answer_bank",
            "intent_examples": [
                "Прошу указать причину отклонения заявки на грантовый конкурс."
            ],
        },
        score=0.8,
        reranker_score=0.8,
    )

    result = await generate(
        {
            "message_masked": "Прошу указать причину отклонения заявки на грантовый конкурс.",
            "analysis": QueryAnalysis(category="гранты"),
            "reranked_chunks": [broad_chunk, exact_chunk],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["cited_sources"] == ["ticket_answer_bank_rejection"]


@pytest.mark.asyncio
async def test_generate_prefers_specific_topic_source_over_same_forum_neighbor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    neighbor = ScoredChunk(
        chunk_id="forum_food",
        text="Food, water and cafe locations for Forum A participants.",
        metadata={
            "chunk_id": "forum_food",
            "forum_normalized": "Forum A",
            "category": "forum",
            "topic": "food",
            "intent_name": "Food conditions",
            "source_category": "Forum A",
        },
        score=1.0,
        reranker_score=0.9,
    )
    specific = ScoredChunk(
        chunk_id="forum_transfer",
        text="Transfer information for Forum A participants.",
        metadata={
            "chunk_id": "forum_transfer",
            "forum_normalized": "Forum A",
            "category": "forum",
            "topic": "transfer_to_venue",
            "intent_name": "Transfer to venue",
            "source_category": "Forum A",
        },
        score=0.7,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "How do I get transfer to the venue?",
            "analysis": QueryAnalysis(
                category="forum",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="How do I get transfer to the venue?",
                        category="forum",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [neighbor, specific],
            "max_confidence": 0.9,
            "llm_client": FailingLLM(),
        }
    )

    assert result["cited_sources"] == ["forum_transfer"]


@pytest.mark.asyncio
async def test_generate_prefers_specific_source_over_generic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    generic = ScoredChunk(
        chunk_id="generic_forum",
        text="Forum A registration, transfer, food, documents and program details.",
        metadata={
            "chunk_id": "generic_forum",
            "forum_normalized": "Forum A",
            "category": "forum",
            "topic": "general",
            "intent_name": "Forum A fallback",
            "source_category": "fallback",
            "is_generic": True,
        },
        score=1.0,
        reranker_score=0.9,
    )
    specific = ScoredChunk(
        chunk_id="specific_transfer",
        text="Transfer details for Forum A.",
        metadata={
            "chunk_id": "specific_transfer",
            "forum_normalized": "Forum A",
            "category": "forum",
            "topic": "transfer_to_venue",
            "intent_name": "Transfer to venue",
            "source_category": "Forum A",
            "is_generic": False,
        },
        score=0.6,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "How do I get transfer to the venue?",
            "analysis": QueryAnalysis(
                category="forum",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="How do I get transfer to the venue?",
                        category="forum",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [generic, specific],
            "max_confidence": 0.9,
            "llm_client": FailingLLM(),
        }
    )

    assert result["cited_sources"] == ["specific_transfer"]


@pytest.mark.asyncio
async def test_generate_prefers_forum_category_intersection_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum_neighbor = ScoredChunk(
        chunk_id="forum_registration",
        text="Forum registration status answer.",
        metadata={
            "chunk_id": "forum_registration",
            "forum_normalized": "Forum A",
            "category": "forums",
            "topic": "registration",
        },
        score=1.0,
        reranker_score=0.95,
    )
    platform_source = ScoredChunk(
        chunk_id="platform_registration",
        text="Platform profile registration answer.",
        metadata={
            "chunk_id": "platform_registration",
            "forum_normalized": "Forum A",
            "category": "platform",
            "topic": "registration",
        },
        score=0.5,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "How do I fix platform registration for Forum A?",
            "analysis": QueryAnalysis(
                category="platform",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="How do I fix platform registration?",
                        category="platform",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [forum_neighbor, platform_source],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["platform_registration"]


@pytest.mark.asyncio
async def test_generate_uses_same_forum_answer_bank_despite_category_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    neighbor = ScoredChunk(
        chunk_id="forum_status",
        text="Forum application status answer.",
        metadata={
            "chunk_id": "forum_status",
            "source_type": "xlsx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "status",
            "intent_name": "Application status",
        },
        score=1.0,
        reranker_score=0.95,
    )
    exact_answer_bank = ScoredChunk(
        chunk_id="forum_platform_exact",
        text="Forum platform account answer.",
        metadata={
            "chunk_id": "forum_platform_exact",
            "source_type": "ticket_answer_bank",
            "forum_normalized": "Forum A",
            "category": "платформа_фгаис",
            "intent_examples": ["I cannot update my platform account for Forum A"],
        },
        score=0.6,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "I cannot update my platform account for Forum A",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="I cannot update my platform account for Forum A",
                        category="форумы",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [neighbor, exact_answer_bank],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["forum_platform_exact"]


@pytest.mark.asyncio
async def test_generate_trusts_top_same_forum_compatible_answer_bank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    compatible_answer_bank = ScoredChunk(
        chunk_id="forum_navigation_ticket",
        text="Forum-specific curator contact answer.",
        metadata={
            "chunk_id": "forum_navigation_ticket",
            "source_type": "ticket_answer_bank",
            "forum_normalized": "Forum A",
            "category": "навигация",
            "is_generic": False,
        },
        score=0.8,
        reranker_score=0.9,
    )
    same_forum_status = ScoredChunk(
        chunk_id="forum_status",
        text="Forum application status answer.",
        metadata={
            "chunk_id": "forum_status",
            "source_type": "xlsx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "status",
        },
        score=1.0,
        reranker_score=0.8,
    )

    result = await generate(
        {
            "message_masked": "Need curator contact for Forum A",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="Need curator contact for Forum A",
                        category="форумы",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [compatible_answer_bank, same_forum_status],
            "max_confidence": 0.9,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["forum_navigation_ticket"]


@pytest.mark.asyncio
async def test_generate_does_not_use_same_forum_grant_answer_bank_for_forum_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    wrong_grant_answer_bank = ScoredChunk(
        chunk_id="forum_grant_ticket",
        text="Grant application answer for the same forum.",
        metadata={
            "chunk_id": "forum_grant_ticket",
            "source_type": "ticket_answer_bank",
            "forum_normalized": "Forum A",
            "category": "гранты",
            "intent_examples": ["How do I submit a grant application for Forum A?"],
        },
        score=1.0,
        reranker_score=0.95,
    )
    official_programme = ScoredChunk(
        chunk_id="forum_programme",
        text="Official Forum A programme answer.",
        metadata={
            "chunk_id": "forum_programme",
            "source_type": "docx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "programme",
            "intent_name": "Programme",
        },
        score=0.8,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "What is the Forum A programme?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="What is the Forum A programme?",
                        category="форумы",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [wrong_grant_answer_bank, official_programme],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["forum_programme"]


@pytest.mark.asyncio
async def test_generate_trusts_exact_top_answer_bank_with_safe_category_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    exact_answer_bank = ScoredChunk(
        chunk_id="forum_grant_technical_ticket",
        text="Forum-specific account recovery answer.",
        metadata={
            "chunk_id": "forum_grant_technical_ticket",
            "source_type": "ticket_answer_bank",
            "forum_normalized": "Forum A",
            "category": "гранты",
            "topic": "доступ_и_техническая_ошибка",
            "intent_examples": ["I cannot access my Forum A application"],
        },
        score=0.8,
        reranker_score=0.9,
    )
    official_neighbor = ScoredChunk(
        chunk_id="forum_reserve_list",
        text="Official reserve-list answer.",
        metadata={
            "chunk_id": "forum_reserve_list",
            "source_type": "xlsx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "reserve_list",
        },
        score=1.0,
        reranker_score=0.8,
    )

    result = await generate(
        {
            "message_masked": "I cannot access my Forum A application",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="I cannot access my Forum A application",
                        category="форумы",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [exact_answer_bank, official_neighbor],
            "max_confidence": 0.9,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["forum_grant_technical_ticket"]


@pytest.mark.asyncio
async def test_generate_does_not_trust_offscope_top_official_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    offscope = ScoredChunk(
        chunk_id="other_forum_requirements",
        text="Requirements for another forum.",
        metadata={
            "chunk_id": "other_forum_requirements",
            "source_type": "xlsx",
            "forum_normalized": "Other Forum",
            "category": "forums",
            "topic": "requirements",
        },
        score=1.0,
        reranker_score=0.95,
    )
    inscope = ScoredChunk(
        chunk_id="target_forum_age",
        text="Age requirements for Target Forum.",
        metadata={
            "chunk_id": "target_forum_age",
            "source_type": "docx",
            "forum_normalized": "Target Forum",
            "category": "forums",
            "topic": "age_requirements",
            "intent_name": "Age requirements",
        },
        score=0.7,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "What are age requirements for Target Forum?",
            "analysis": QueryAnalysis(
                category="forums",
                forum_normalized="Target Forum",
                questions=[
                    Question(
                        text="What are age requirements?",
                        category="forums",
                        forum_normalized="Target Forum",
                    )
                ],
            ),
            "reranked_chunks": [offscope, inscope],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["target_forum_age"]


@pytest.mark.asyncio
async def test_generate_does_not_trust_offscope_exact_answer_bank_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    offscope_exact = ScoredChunk(
        chunk_id="other_forum_ticket",
        text="Answer bank source for another forum.",
        metadata={
            "chunk_id": "other_forum_ticket",
            "source_type": "ticket_answer_bank",
            "forum_normalized": "Other Forum",
            "category": "forums",
            "intent_examples": ["How do I get an invitation letter?"],
        },
        score=1.0,
        reranker_score=0.95,
    )
    inscope = ScoredChunk(
        chunk_id="target_forum_letter",
        text="Invitation letter source for Target Forum.",
        metadata={
            "chunk_id": "target_forum_letter",
            "source_type": "docx",
            "forum_normalized": "Target Forum",
            "category": "forums",
            "topic": "invitation_letter",
            "intent_name": "Invitation letter",
        },
        score=0.7,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "How do I get an invitation letter?",
            "analysis": QueryAnalysis(
                category="forums",
                forum_normalized="Target Forum",
                questions=[
                    Question(
                        text="How do I get an invitation letter?",
                        category="forums",
                        forum_normalized="Target Forum",
                    )
                ],
            ),
            "reranked_chunks": [offscope_exact, inscope],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["target_forum_letter"]


@pytest.mark.asyncio
async def test_rerank_promotes_safe_answer_bank_topic_across_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
        ),
    )
    forum_neighbor = Chunk(
        chunk_id="forum_registration",
        text="Forum registration answer.",
        metadata={
            "chunk_id": "forum_registration",
            "source_type": "ticket_answer_bank",
            "category": "форумы",
            "topic": "регистрация_и_заявка",
        },
        score=0.9,
    )
    operator_answer = Chunk(
        chunk_id="operator_contact",
        text="Operator contact answer.",
        metadata={
            "chunk_id": "operator_contact",
            "source_type": "ticket_answer_bank",
            "category": "навигация",
            "topic": "контакты_и_оператор",
        },
        score=0.7,
    )

    result = await rerank(
        {
            "message_masked": "Как связаться с оператором?",
            "analysis": QueryAnalysis(
                category="форумы",
                questions=[Question(text="Как связаться с оператором?", category="форумы")],
            ),
            "retrieved_chunks": [forum_neighbor, operator_answer],
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "operator_contact"


@pytest.mark.asyncio
async def test_rerank_prefers_specific_answer_bank_topic_over_misc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
        ),
    )
    misc = Chunk(
        chunk_id="misc_answer",
        text="Misc answer.",
        metadata={
            "chunk_id": "misc_answer",
            "source_type": "ticket_answer_bank",
            "category": "форумы",
            "topic": "прочее",
            "intent_examples": ["Вопрос по премии Время молодых"],
        },
        score=1.0,
    )
    specific = Chunk(
        chunk_id="specific_answer",
        text="Specific registration answer.",
        metadata={
            "chunk_id": "specific_answer",
            "source_type": "ticket_answer_bank",
            "category": "платформа_фгаис",
            "topic": "регистрация_и_заявка",
            "intent_examples": ["Вопрос по премии Время молодых"],
        },
        score=0.7,
    )

    result = await rerank(
        {
            "message_masked": "Вопрос по премии Время молодых",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Время молодых",
                questions=[
                    Question(
                        text="Вопрос по премии Время молодых",
                        category="форумы",
                        forum_normalized="Время молодых",
                    )
                ],
            ),
            "retrieved_chunks": [misc, specific],
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "specific_answer"


@pytest.mark.asyncio
async def test_rerank_does_not_protect_exact_match_from_other_forum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
        ),
    )
    off_forum_exact = Chunk(
        chunk_id="other_forum_arrival",
        text="Other forum arrival answer.",
        metadata={
            "chunk_id": "other_forum_arrival",
            "source_type": "xlsx",
            "forum_normalized": "Other Forum",
            "category": "форумы",
            "topic": "vremya_zaezda_i_vyezda",
            "intent_examples": ["Когда заезд и выезд?"],
        },
        score=1.0,
    )
    target_forum_source = Chunk(
        chunk_id="target_forum_arrival",
        text="Target forum arrival answer.",
        metadata={
            "chunk_id": "target_forum_arrival",
            "source_type": "docx",
            "forum_normalized": "Target Forum",
            "category": "форумы",
            "topic": "vremya_zaezda_i_vyezda",
        },
        score=0.7,
    )

    result = await rerank(
        {
            "message_masked": "Когда заезд и выезд?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Target Forum",
                questions=[
                    Question(
                        text="Когда заезд и выезд?",
                        category="форумы",
                        forum_normalized="Target Forum",
                    )
                ],
            ),
            "retrieved_chunks": [off_forum_exact, target_forum_source],
            "reranker": InputOrderReranker(),
        }
    )

    assert [chunk.chunk_id for chunk in result["reranked_chunks"]] == ["target_forum_arrival"]


@pytest.mark.asyncio
async def test_rerank_promotes_specific_technical_fallback_over_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
        ),
    )
    generic_error = Chunk(
        chunk_id="generic_error",
        text="Generic technical error answer.",
        metadata={
            "chunk_id": "generic_error",
            "category": "техподдержка",
            "source_category": "fallback",
            "topic": "tehnicheskaya_oshibka",
            "intent_name": "Техническая ошибка",
        },
        score=1.0,
    )
    language_settings = Chunk(
        chunk_id="language_settings",
        text="Language settings answer.",
        metadata={
            "chunk_id": "language_settings",
            "category": "техподдержка",
            "source_category": "fallback",
            "topic": "tehnicheskie_voprosy_yazyki",
            "intent_name": "Технические вопросы: языки",
        },
        score=0.7,
    )

    result = await rerank(
        {
            "message_masked": "Как поменять язык на платформе?",
            "analysis": QueryAnalysis(category="техподдержка"),
            "retrieved_chunks": [generic_error, language_settings],
            "reranker": InputOrderReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == "language_settings"


@pytest.mark.asyncio
async def test_generate_prefers_transfer_topic_over_travel_payment_neighbor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    travel_payment = ScoredChunk(
        chunk_id="travel_payment",
        text="Travel payment answer.",
        metadata={
            "chunk_id": "travel_payment",
            "source_type": "xlsx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "oplata_proezda",
            "intent_name": "Оплата проезда",
        },
        score=1.0,
        reranker_score=0.95,
    )
    transfer = ScoredChunk(
        chunk_id="transfer",
        text="Transfer answer.",
        metadata={
            "chunk_id": "transfer",
            "source_type": "xlsx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "transfer_do_mesta_provedeniya_meropriyatiya",
            "intent_name": "Трансфер до места проведения мероприятия",
        },
        score=0.6,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Есть ли трансфер до места проведения?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="Есть ли трансфер до места проведения?",
                        category="форумы",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [travel_payment, transfer],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["transfer"]


@pytest.mark.asyncio
async def test_generate_prefers_specific_answer_bank_topic_over_misc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    misc = ScoredChunk(
        chunk_id="misc_answer",
        text="Misc answer.",
        metadata={
            "chunk_id": "misc_answer",
            "source_type": "ticket_answer_bank",
            "category": "форумы",
            "topic": "прочее",
            "intent_examples": ["Вопрос по премии Время молодых"],
        },
        score=1.0,
        reranker_score=0.95,
    )
    specific = ScoredChunk(
        chunk_id="specific_answer",
        text="Specific registration answer.",
        metadata={
            "chunk_id": "specific_answer",
            "source_type": "ticket_answer_bank",
            "category": "платформа_фгаис",
            "topic": "регистрация_и_заявка",
            "intent_examples": ["Вопрос по премии Время молодых"],
        },
        score=0.7,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Вопрос по премии Время молодых",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Время молодых",
                questions=[
                    Question(
                        text="Вопрос по премии Время молодых",
                        category="форумы",
                        forum_normalized="Время молодых",
                    )
                ],
            ),
            "reranked_chunks": [misc, specific],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["specific_answer"]


@pytest.mark.asyncio
async def test_generate_prefers_specific_technical_fallback_over_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    generic_error = ScoredChunk(
        chunk_id="generic_error",
        text="Generic technical error answer.",
        metadata={
            "chunk_id": "generic_error",
            "category": "техподдержка",
            "source_category": "fallback",
            "topic": "tehnicheskaya_oshibka",
            "intent_name": "Техническая ошибка",
        },
        score=1.0,
        reranker_score=0.95,
    )
    language_settings = ScoredChunk(
        chunk_id="language_settings",
        text="Language settings answer.",
        metadata={
            "chunk_id": "language_settings",
            "category": "техподдержка",
            "source_category": "fallback",
            "topic": "tehnicheskie_voprosy_yazyki",
            "intent_name": "Технические вопросы: языки",
        },
        score=0.7,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Как поменять язык на платформе?",
            "analysis": QueryAnalysis(
                category="техподдержка",
                questions=[
                    Question(text="Как поменять язык на платформе?", category="техподдержка")
                ],
            ),
            "reranked_chunks": [generic_error, language_settings],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["language_settings"]


@pytest.mark.asyncio
async def test_generate_prefers_arrival_departure_over_general_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    general_dates = ScoredChunk(
        chunk_id="general_dates",
        text="General event dates answer.",
        metadata={
            "chunk_id": "general_dates",
            "source_type": "docx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "mesto_i_daty_provedeniya_meropriyatiya",
            "intent_name": "Место и даты проведения мероприятия",
        },
        score=1.0,
        reranker_score=0.95,
    )
    arrival_departure = ScoredChunk(
        chunk_id="arrival_departure",
        text="Arrival and departure answer.",
        metadata={
            "chunk_id": "arrival_departure",
            "source_type": "docx",
            "forum_normalized": "Forum A",
            "category": "форумы",
            "topic": "vremya_zaezda_i_vyezda",
            "intent_name": "Время заезда и выезда",
        },
        score=0.6,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Когда заезд и выезд?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Forum A",
                questions=[
                    Question(
                        text="Когда заезд и выезд?",
                        category="форумы",
                        forum_normalized="Forum A",
                    )
                ],
            ),
            "reranked_chunks": [general_dates, arrival_departure],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["arrival_departure"]


@pytest.mark.asyncio
async def test_generate_trusts_top_answer_bank_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    answer_bank_chunk = ScoredChunk(
        chunk_id="ticket_answer_bank_application",
        text="Чтобы подать заявку на участие в гранте, заполните проектную форму на ФГАИС.",
        metadata={
            "chunk_id": "ticket_answer_bank_application",
            "category": "гранты",
            "source_type": "ticket_answer_bank",
        },
        score=0.7,
        reranker_score=0.7,
    )
    broad_chunk = ScoredChunk(
        chunk_id="generic_application",
        text="Подать заявку на мероприятие можно через платформу.",
        metadata={"chunk_id": "generic_application", "category": "гранты"},
        score=1.0,
        reranker_score=0.95,
    )

    result = await generate(
        {
            "message_masked": "Как подать заявку?",
            "analysis": QueryAnalysis(category="гранты"),
            "reranked_chunks": [answer_bank_chunk, broad_chunk],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "Чтобы подать заявку на участие в гранте, заполните проектную форму "
        "на ФГАИС. [src:ticket_answer_bank_application]"
    )
    assert result["cited_sources"] == ["ticket_answer_bank_application"]


@pytest.mark.asyncio
async def test_generate_prefers_original_exact_answer_bank_over_neighbor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    neighbor = ScoredChunk(
        chunk_id="ticket_answer_bank_neighbor",
        text="Neighbor answer.",
        metadata={
            "chunk_id": "ticket_answer_bank_neighbor",
            "category": "grants",
            "source_type": "ticket_answer_bank",
            "intent_examples": ["Where is my application status?"],
        },
        score=0.99,
        reranker_score=0.99,
    )
    exact = ScoredChunk(
        chunk_id="ticket_answer_bank_exact",
        text="Exact answer.",
        metadata={
            "chunk_id": "ticket_answer_bank_exact",
            "category": "grants",
            "source_type": "ticket_answer_bank",
            "intent_examples": ["Why was my grant application rejected?"],
        },
        score=0.8,
        reranker_score=0.8,
    )

    result = await generate(
        {
            "message_masked": "Why was my grant application rejected?",
            "analysis": QueryAnalysis(category="grants"),
            "reranked_chunks": [neighbor, exact],
            "max_confidence": 0.99,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == "Exact answer. [src:ticket_answer_bank_exact]"
    assert result["cited_sources"] == ["ticket_answer_bank_exact"]


@pytest.mark.asyncio
async def test_generate_trusts_exact_top_answer_bank_before_category_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    answer_bank_chunk = ScoredChunk(
        chunk_id="ticket_answer_bank_profile",
        text="Проверьте данные профиля в личном кабинете грантового конкурса.",
        metadata={
            "chunk_id": "ticket_answer_bank_profile",
            "category": "гранты",
            "source_type": "ticket_answer_bank",
            "intent_examples": ["Не могу обновить данные профиля для гранта"],
        },
        score=0.8,
        reranker_score=0.7,
    )
    technical_chunk = ScoredChunk(
        chunk_id="technical_fallback",
        text="При технической ошибке очистите кэш браузера.",
        metadata={"chunk_id": "technical_fallback", "category": "техподдержка"},
        score=1.0,
        reranker_score=0.95,
    )

    result = await generate(
        {
            "message_masked": "Не могу обновить данные профиля для гранта",
            "analysis": QueryAnalysis(category="техподдержка"),
            "reranked_chunks": [answer_bank_chunk, technical_chunk],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generated_response"] == (
        "Проверьте данные профиля в личном кабинете грантового конкурса. "
        "[src:ticket_answer_bank_profile]"
    )
    assert result["cited_sources"] == ["ticket_answer_bank_profile"]


@pytest.mark.asyncio
async def test_generate_does_not_return_source_chunk_when_only_metadata_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="docs",
        text="Итоговая программа будет направлена в чат участников.",
        metadata={"intent_name": "Документы мероприятия"},
        score=1.0,
        reranker_score=0.95,
    )
    llm = CapturingLLM()

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                questions=[Question(text="Вышлите положение")],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"
    assert result["generator_model"] == "source_only"


@pytest.mark.asyncio
async def test_generate_covers_compatible_fallback_metadata_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    source = ScoredChunk(
        chunk_id="what_is_rosmol",
        text="Росмолодёжь поддерживает молодёжные инициативы.",
        metadata={
            "chunk_id": "what_is_rosmol",
            "source_type": "xlsx",
            "source_category": "fallback",
            "category": "платформа_фгаис",
            "topic": "chto_takoe_rosmolodezh",
            "intent_name": "Что такое Росмолодёжь?",
        },
        score=0.8,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "message_masked": "Что такое Росмолодёжь?",
            "analysis": QueryAnalysis(category="общее"),
            "reranked_chunks": [source],
            "max_confidence": 0.7,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["what_is_rosmol"]


@pytest.mark.asyncio
async def test_generate_uses_max_for_single_covered_complex_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="travel",
        text="Проезд до места проведения форума и обратно оплачивает направляющая сторона.",
        metadata={"intent_name": "Оплата проезда"},
        score=0.8,
        reranker_score=0.63,
    )

    llm = CapturingLLM("Проезд до места проведения оплачивает направляющая сторона. [src:travel]")

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                questions=[Question(text="Возмещение денежных средств на поездку")],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.63,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert llm.kwargs[0]["model"] == "GigaChat/GigaChat-2-Max"
    assert result["generator_model"] == "GigaChat/GigaChat-2-Max"
    assert result["cited_sources"] == ["travel"]


@pytest.mark.asyncio
async def test_generate_uses_source_chunk_for_single_official_complex_forum_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="ivolga_memo",
        text="Список всего необходимого будет перечислен в памятке участника форума.",
        metadata={
            "source_type": "xlsx",
            "category": "форумы",
            "forum_normalized": "Иволга",
            "topic": "pamyatka_uchastnika_foruma",
            "intent_examples": ["какие документы и вещи взять с собой"],
        },
        score=0.8,
        reranker_score=0.72,
    )
    llm = FailingLLM()

    result = await generate(
        {
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Иволга",
                complexity=Complexity.COMPLEX,
                questions=[Question(text="Какие документы нужны?", forum_normalized="Иволга")],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.72,
            "llm_client": llm,
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ivolga_memo"]
    assert "памятке участника" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_prefers_event_overview_source_for_forum_summary_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    overview = ScoredChunk(
        chunk_id="russian_north_overview",
        text=(
            "Форум «Российский Север» — межнациональная площадка для активной молодежи. "
            "Главная тема — креативные индустрии и сохранение культурного наследия."
        ),
        metadata={
            "source_type": "xlsx",
            "category": "форумы",
            "forum_normalized": "Российский Север",
            "topic": "o_meropriyatii",
            "intent_name": "Суть форума и направления",
        },
        score=0.9,
        reranker_score=0.91,
    )
    programme = ScoredChunk(
        chunk_id="russian_north_programme",
        text="Подробная сетка расписания со всеми лекциями откроется за день до начала форума.",
        metadata={
            "source_type": "xlsx",
            "category": "форумы",
            "forum_normalized": "Российский Север",
            "topic": "programma_foruma",
            "intent_name": "Программа форума",
        },
        score=0.88,
        reranker_score=0.9,
    )

    result = await generate(
        {
            "message_masked": "Российский Север Суть форума и направления",
            "analysis": QueryAnalysis(
                category="форумы",
                complexity=Complexity.COMPLEX,
                forum_normalized="Российский Север",
                questions=[
                    Question(
                        text="Российский Север Суть форума и направления",
                        category="форумы",
                        forum_normalized="Российский Север",
                    )
                ],
            ),
            "reranked_chunks": [programme, overview],
            "max_confidence": 0.91,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["russian_north_overview"]


@pytest.mark.parametrize(
    ("query", "category", "expected_chunk_id", "topic", "answer"),
    [
        (
            "Оставить обратную связь о сотрудн",
            "навигация",
            "staff_feedback",
            "ostavit_obratnuyu_svyaz_o_sotrudn",
            "Перевожу диалог на оператора",
        ),
        (
            "Предложение о сотрудничестве",
            "общее",
            "cooperation",
            "predlozhenie_sotrudnichestva",
            "По вопросам сотрудничества напишите на partner@fadm.gov.ru.",
        ),
        (
            "Возможности бота / abilities",
            "общее",
            "bot_abilities",
            "vozmozhnosti_bota_abilities",
            "Бот информирует о деятельности Росмолодёжи, форумах и грантах.",
        ),
        (
            "Что такое Росмолодёжь?",
            "платформа_фгаис",
            "what_is_rosmol",
            "chto_takoe_rosmolodezh",
            "Росмолодёжь — федеральный орган исполнительной власти.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_prioritizes_exact_fallback_sources(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    category: str,
    expected_chunk_id: str,
    topic: str,
    answer: str,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    competing_chunk = ScoredChunk(
        chunk_id="competing",
        text="Мы всегда рады получить обратную связь по мероприятию.",
        metadata={"source_type": "xlsx", "category": category, "topic": "generic"},
        score=1.0,
        reranker_score=0.95,
    )
    expected_chunk = ScoredChunk(
        chunk_id=expected_chunk_id,
        text=answer,
        metadata={"source_type": "xlsx", "category": category, "topic": topic},
        score=0.5,
        reranker_score=0.5,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                category=category,
                complexity=Complexity.SIMPLE,
                questions=[Question(text=query, category=category)],
            ),
            "reranked_chunks": [competing_chunk, expected_chunk],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == [expected_chunk_id]
    assert answer in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_returns_source_chunk_for_duplicate_covered_aspect_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="travel",
        text="Проезд до места проведения форума и обратно оплачивает направляющая сторона.",
        metadata={"forum_normalized": "Истоки Школа", "intent_name": "Оплата проезда"},
        score=0.75,
        reranker_score=0.63,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                forum_normalized="Истоки Школа",
                questions=[
                    Question(
                        text="Кто возмещает денежные средства на поездку?",
                        forum_normalized="Истоки Школа",
                    ),
                    Question(
                        text="Какие условия возмещения расходов?",
                        forum_normalized="Истоки Школа",
                    ),
                ],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.63,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["travel"]


@pytest.mark.asyncio
async def test_generate_returns_source_chunk_for_costs_covered_by_travel_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="travel",
        text=(
            "Проезд до Махачкалы и обратно оплачивает направляющая сторона. "
            "Трансфер, проживание и питание обеспечивают организаторы."
        ),
        metadata={"forum_normalized": "Каспий", "intent_name": "Оплата проезда"},
        score=1.0,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                forum_normalized="Каспий",
                questions=[
                    Question(
                        text="Кто возмещает денежные средства на поездку?",
                        forum_normalized="Каспий",
                    ),
                    Question(
                        text="Какие расходы покрываются организаторами?",
                        forum_normalized="Каспий",
                    ),
                ],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.7,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["travel"]


@pytest.mark.asyncio
async def test_generate_prefers_unscoped_grant_source_for_generic_grant_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum_grant = ScoredChunk(
        chunk_id="utro_grant",
        text="На форуме «УТРО» подать заявку на грант можно на платформе ФГАИС.",
        metadata={"category": "гранты", "forum_normalized": "Гранты для физических лиц"},
        score=0.3,
        reranker_score=0.95,
    )
    generic_grant = ScoredChunk(
        chunk_id="generic_grant_application",
        text="Чтобы подать заявку на участие в гранте, заполните проектную форму на ФГАИС.",
        metadata={"category": "гранты"},
        score=1.0,
        reranker_score=0.9,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="гранты",
                questions=[Question(text="Где подать проект на грант?", category="гранты")],
            ),
            "reranked_chunks": [forum_grant, generic_grant],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["generic_grant_application"]
    assert "ФГАИС" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_prefers_generic_grant_source_category_over_forum_grant_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum_grant = ScoredChunk(
        chunk_id="forum_grant_terms",
        text="Подать заявку можно на грантовый конкурс форума «Шум».",
        metadata={
            "category": "гранты",
            "source_category": "Шум",
            "intent_name": "Условия и сроки участия. Гранты",
        },
        score=1.0,
        reranker_score=0.95,
    )
    generic_grant = ScoredChunk(
        chunk_id="generic_grant_application",
        text="Чтобы подать заявку на участие в гранте, заполните проектную форму на ФГАИС.",
        metadata={
            "category": "гранты",
            "source_category": "Гранты для физических лиц",
            "intent_name": "Подать заявку на участие",
        },
        score=0.8,
        reranker_score=0.9,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="гранты",
                questions=[
                    Question(
                        text="Гранты для физических лиц Подать заявку на участие",
                        category="гранты",
                    )
                ],
            ),
            "reranked_chunks": [forum_grant, generic_grant],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["generic_grant_application"]


@pytest.mark.asyncio
async def test_generate_prefers_grant_project_change_over_grant_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    grant_change = ScoredChunk(
        chunk_id="generic_grant_change",
        text="Нужно изменить смету? Напиши на почту grant2024@fadm.gov.ru.",
        metadata={
            "category": "гранты",
            "source_category": "Гранты для физических лиц",
            "intent_name": "Внести изменения в проект",
            "topic": "vnesti_izmeneniya_v_proekt",
            "source_type": "xlsx",
        },
        score=1.0,
        reranker_score=0.7,
    )
    grant_terms = ScoredChunk(
        chunk_id="generic_grant_terms",
        text=(
            "В конкурсе могут участвовать граждане Российской Федерации от 14 до 35 лет. "
            "Физическое лицо вправе представить один проект."
        ),
        metadata={
            "category": "гранты",
            "source_category": "Гранты для физических лиц",
            "intent_name": "Условия и сроки участия",
            "topic": "usloviya_i_sroki_uchastiya",
            "source_type": "xlsx",
        },
        score=0.9,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="гранты",
                questions=[],
            ),
            "message_masked": "Гранты для физических лиц Внести изменения в проект",
            "reranked_chunks": [grant_change, grant_terms],
            "max_confidence": 0.7,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["generic_grant_change"]
    assert "grant2024@fadm.gov.ru" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_uses_source_chunk_for_complex_single_official_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    grant_agreement = ScoredChunk(
        chunk_id="grant_agreement",
        text="По вопросам грантового соглашения нужно обратиться к куратору конкурса.",
        metadata={"category": "гранты", "source_type": "xlsx"},
        score=0.9,
        reranker_score=0.85,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="гранты",
                questions=[
                    Question(
                        text="Что делать, если грантовое соглашение на старые данные?",
                        category="гранты",
                    )
                ],
            ),
            "reranked_chunks": [grant_agreement],
            "max_confidence": 0.85,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["grant_agreement"]


@pytest.mark.asyncio
async def test_generate_prefers_municipal_admin_access_over_generic_technical_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    generic_technical = ScoredChunk(
        chunk_id="generic_technical",
        text="Очистите кеш и попробуйте зайти с другого устройства.",
        metadata={"category": "техподдержка", "topic": "tehnicheskaya_oshibka"},
        score=1.0,
        reranker_score=0.95,
    )
    admin_access = ScoredChunk(
        chunk_id="municipal_admin_access",
        text="Для доступа муниципального администратора обратитесь в региональный ОИВ.",
        metadata={
            "category": "техподдержка",
            "topic": "tehnicheskie_voprosy_dostup_municipalnogo_administratora",
        },
        score=0.7,
        reranker_score=0.72,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="техподдержка",
                questions=[
                    Question(
                        text="технические вопросы доступ муниципального администратора",
                        category="техподдержка",
                    )
                ],
            ),
            "reranked_chunks": [generic_technical, admin_access],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["cited_sources"] == ["municipal_admin_access"]


@pytest.mark.asyncio
async def test_generate_prefers_sport_recommendation_over_generic_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    generic_recommendation = ScoredChunk(
        chunk_id="recommendation_generic",
        text="Посмотри все доступные форумы на events.myrosmol.ru/forumy.",
        metadata={"category": "общее", "topic": "rekomendacii_obschie"},
        score=1.0,
        reranker_score=0.95,
    )
    sport_recommendation = ScoredChunk(
        chunk_id="recommendation_sport",
        text="Для спорта подойдёт смена «Физическая культура и спорт» на форуме «ТИМ Бирюса».",
        metadata={"category": "платформа_фгаис", "topic": "rekomendacii_sport"},
        score=0.7,
        reranker_score=0.72,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="платформа_фгаис",
                questions=[
                    Question(text="рекомендации спорт", category="платформа_фгаис")
                ],
            ),
            "reranked_chunks": [generic_recommendation, sport_recommendation],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["cited_sources"] == ["recommendation_sport"]


@pytest.mark.asyncio
async def test_generate_prefers_student_recommendation_over_generic_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    generic_recommendation = ScoredChunk(
        chunk_id="recommendation_generic",
        text="Посмотри все доступные форумы на events.myrosmol.ru/forumy.",
        metadata={"category": "общее", "topic": "rekomendacii_obschie"},
        score=1.0,
        reranker_score=0.95,
    )
    student_recommendation = ScoredChunk(
        chunk_id="recommendation_student",
        text=(
            "Яркие представители студенческих сообществ соберутся на форумах "
            "«Истоки», «Утро» и «Полюс»."
        ),
        metadata={
            "category": "платформа_фгаис",
            "topic": "rekomendacii_studenty",
            "intent_name": "рекомендации.студенты",
        },
        score=0.7,
        reranker_score=0.72,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="платформа_фгаис",
                questions=[
                    Question(text="рекомендации.студенты", category="платформа_фгаис")
                ],
            ),
            "reranked_chunks": [generic_recommendation, student_recommendation],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["recommendation_student"]


@pytest.mark.asyncio
async def test_generate_prefers_password_recovery_over_generic_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    generic_support = ScoredChunk(
        chunk_id="generic_support",
        text="Если возникла техническая ошибка, обратитесь в поддержку.",
        metadata={"category": "платформа_фгаис", "topic": "tehnicheskaya_oshibka"},
        score=1.0,
        reranker_score=0.95,
    )
    password_recovery = ScoredChunk(
        chunk_id="password_recovery",
        text="Чтобы восстановить пароль, перейди по ссылке входа и нажми «Восстановить пароль».",
        metadata={
            "category": "платформа_фгаис",
            "source_category": "fallback",
            "intent_name": "Восстановить пароль",
            "topic": "vosstanovit_parol",
        },
        score=0.7,
        reranker_score=0.72,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="платформа_фгаис",
                questions=[Question(text="Восстановить пароль", category="платформа_фгаис")],
            ),
            "reranked_chunks": [generic_support, password_recovery],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["password_recovery"]


@pytest.mark.asyncio
async def test_generate_prefers_forum_invitation_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    transfer = ScoredChunk(
        chunk_id="ivolga_transfer",
        text="Трансфер на форум Иволга будет организован от точки сбора.",
        metadata={
            "category": "форумы",
            "forum_normalized": "Иволга",
            "topic": "transfer_do_mesta_provedeniya_meropriyatiya",
        },
        score=1.0,
        reranker_score=0.95,
    )
    invitation = ScoredChunk(
        chunk_id="ivolga_invitation",
        text="Письмо-вызов можно получить по запросу после заполнения формы.",
        metadata={
            "category": "форумы",
            "forum_normalized": "Иволга",
            "intent_name": "Письмо-вызов",
            "topic": "pismo_vyzov",
        },
        score=0.7,
        reranker_score=0.72,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Иволга",
                questions=[Question(text="Иволга Письмо-вызов", category="форумы")],
            ),
            "message_masked": "Иволга Письмо-вызов",
            "reranked_chunks": [transfer, invitation],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["ivolga_invitation"]


@pytest.mark.asyncio
async def test_generate_prefers_exact_platform_registration_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    broad_navigation = ScoredChunk(
        chunk_id="platform_navigation",
        text=(
            "На сайте Росмолодёжь.Форумы можно найти мероприятия и подать заявку "
            "на подходящее событие."
        ),
        metadata={
            "category": "платформа_фгаис",
            "topic": "napravleniya_i_cennosti_rosmola",
            "intent_name": "Направления и ценности Росмола",
        },
        score=0.9,
        reranker_score=0.95,
    )
    exact_registration = ScoredChunk(
        chunk_id="fgais_registration",
        text="Пройти регистрацию в ФГАИС можно по ссылке: https://myrosmol.ru/auth/register",
        metadata={
            "category": "платформа_фгаис",
            "topic": "kak_zaregistrirovatsya_na_fgais",
            "intent_name": "Как зарегистрироваться на ФГАИС",
        },
        score=0.8,
        reranker_score=0.75,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="платформа_фгаис",
                questions=[
                    Question(
                        text="Как подать заявку или зарегистрироваться?",
                        category="платформа_фгаис",
                    )
                ],
            ),
            "reranked_chunks": [broad_navigation, exact_registration],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["fgais_registration"]
    assert "auth/register" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_prefers_exact_profile_id_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    broad_support = ScoredChunk(
        chunk_id="account_error",
        text="Если возникла ошибка входа в личный кабинет, обратитесь в поддержку.",
        metadata={"category": "техподдержка", "topic": "oshibka_vhoda"},
        score=0.9,
        reranker_score=0.9,
    )
    exact_id = ScoredChunk(
        chunk_id="profile_id",
        text="Чтобы скопировать ID профиля, нажмите на кнопку ID рядом с аватаром.",
        metadata={
            "category": "техподдержка",
            "topic": "gde_nayti_id_profilya",
            "intent_name": "Где найти ID профиля?",
        },
        score=0.8,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="техподдержка",
                questions=[Question(text="Где найти ID профиля?", category="техподдержка")],
            ),
            "reranked_chunks": [broad_support, exact_id],
            "max_confidence": 0.9,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["profile_id"]


@pytest.mark.asyncio
async def test_generate_does_not_return_source_chunk_for_uncovered_multi_aspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk = ScoredChunk(
        chunk_id="travel",
        text="Проезд до места проведения форума и обратно оплачивает направляющая сторона.",
        metadata={"forum_normalized": "Истоки Школа", "intent_name": "Оплата проезда"},
        score=0.75,
        reranker_score=0.63,
    )
    llm = CapturingLLM()

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                forum_normalized="Истоки Школа",
                questions=[
                    Question(text="Кто оплачивает проезд?", forum_normalized="Истоки Школа"),
                    Question(text="Какие условия проживания?", forum_normalized="Истоки Школа"),
                ],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.63,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"
    assert result["generator_model"] == "source_only"


@pytest.mark.asyncio
async def test_generate_combines_multiple_covered_source_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunks = [
        ScoredChunk(
            chunk_id="travel",
            text="Проезд до форума оплачивает направляющая сторона.",
            metadata={"forum_normalized": "Машук", "category": "форумы"},
            score=0.8,
            reranker_score=0.7,
        ),
        ScoredChunk(
            chunk_id="housing",
            text="Формат проживания: участников размещают в палатках на площадке форума.",
            metadata={"forum_normalized": "Машук", "category": "форумы"},
            score=0.8,
            reranker_score=0.68,
        ),
    ]
    llm = CapturingLLM(
        "Проезд до форума оплачивает направляющая сторона. [src:travel]\n\n"
        "Участников размещают в палатках на площадке форума. [src:housing]"
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                forum_normalized="Машук",
                category="форумы",
                questions=[
                    Question(text="Кто оплачивает проезд?", forum_normalized="Машук"),
                    Question(text="Какие условия проживания?", forum_normalized="Машук"),
                ],
            ),
            "reranked_chunks": chunks,
            "max_confidence": 0.7,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert llm.kwargs[0]["model"] == "GigaChat/GigaChat-2-Max"
    assert result["generator_model"] == "GigaChat/GigaChat-2-Max"
    assert result["cited_sources"] == ["travel", "housing"]
    assert "Проезд до форума" in result["generated_response"]
    assert "палатках" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_repairs_known_source_ref_transliteration_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunk_id = "xlsx_category_r0218_podacha_zayavki_na_proekt"
    chunk = ScoredChunk(
        chunk_id=chunk_id,
        text="После подачи заявки ты сможешь следить за её статусом в личном кабинете.",
        metadata={
            "category": "форумы",
            "forum_normalized": "Амур",
            "source_type": "xlsx",
            "topic": "podacha_zayavki_na_proekt",
        },
        score=0.9,
        reranker_score=0.9,
    )
    llm = CapturingLLM(
        "После подачи заявки статус можно смотреть в личном кабинете. "
        "[src:xlsx_category_r0218_podacha_zayavki_na_projekt]"
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized="Амур",
                questions=[
                    Question(
                        text="Как подать заявку?",
                        category="форумы",
                        forum_normalized="Амур",
                        topic="podacha_zayavki_na_proekt",
                    )
                ],
            ),
            "reranked_chunks": [chunk],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert result["generator_model"] == "GigaChat/GigaChat-2-Max"
    assert result["cited_sources"] == [chunk_id]
    assert f"[src:{chunk_id}]" in result["generated_response"]
    assert "projekt" not in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_uses_extractive_answer_for_official_forum_multi_aspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum = "Больше, чем путешествие"
    chunks = [
        ScoredChunk(
            chunk_id="registration",
            text="Регистрация на фестивальный день открыта до 11 июля включительно.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "docx",
                "intent_name": "registraciya",
                "topic": "sroki_registracii_i_rezultaty_otbora",
            },
            score=0.8,
            reranker_score=0.8,
        ),
        ScoredChunk(
            chunk_id="age",
            text="Возрастная маркировка фестиваля 0+.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "docx",
                "intent_name": "vozrast",
                "topic": "vozrastnye_ogranicheniya",
            },
            score=0.8,
            reranker_score=0.8,
        ),
        ScoredChunk(
            chunk_id="travel",
            text="Победителям оплачивают проезд, питание и проживание.",
            metadata={
                "forum_normalized": forum,
                "category": "форумы",
                "source_type": "docx",
                "intent_name": "oplata_proezda_i_prozhivaniya",
                "topic": "oplata_proezda_i_prozhivaniya",
            },
            score=0.8,
            reranker_score=0.8,
        ),
    ]
    llm = FailingLLM()

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized=forum,
                questions=[
                    Question(text="Как зарегистрироваться?", forum_normalized=forum),
                    Question(text="Какой возраст?", forum_normalized=forum),
                    Question(text="Оплата проезда и проживания?", forum_normalized=forum),
                ],
            ),
            "message_masked": (
                "Больше, чем путешествие: регистрация, возраст, дорога и проживание?"
            ),
            "reranked_chunks": chunks,
            "max_confidence": 0.8,
            "llm_client": llm,
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["registration", "age", "travel"]
    assert "Регистрация на фестивальный день открыта" in result["generated_response"]
    assert "Возрастная маркировка" in result["generated_response"]
    assert "Победителям оплачивают проезд" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_selects_source_for_each_multi_aspect_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum = "Амур"
    application = ScoredChunk(
        chunk_id="amur_application",
        text="Обратите внимание: регистрация на форум «Амур» закрыта.",
        metadata={
            "category": "форумы",
            "forum_normalized": forum,
            "source_type": "xlsx",
            "topic": "podacha_zayavki_na_proekt",
            "intent_name": "Подача заявки на проект",
            "intent_examples": ["Как подать заявку на форум?"],
        },
        score=0.98,
        reranker_score=0.95,
    )
    travel = ScoredChunk(
        chunk_id="amur_travel",
        text="Обычно оплата проезда осуществляется за счёт направляющей стороны.",
        metadata={
            "category": "форумы",
            "forum_normalized": forum,
            "source_type": "xlsx",
            "topic": "oplata_proezda",
            "intent_name": "Оплата проезда",
        },
        score=0.72,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized=forum,
                questions=[
                    Question(text="Как подать заявку или зарегистрироваться?", category="форумы"),
                    Question(text="Кто оплачивает проезд?", category="форумы"),
                ],
            ),
            "message_masked": "Амур: как подать заявку, оплачивается ли проезд?",
            "reranked_chunks": [application, travel],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["amur_application", "amur_travel"]
    assert "регистрация на форум «Амур» закрыта" in result["generated_response"]
    assert "оплата проезда" in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_selects_application_and_decline_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    forum = "Амур"
    application = ScoredChunk(
        chunk_id="xlsx_category_r0218_podacha_zayavki_na_proekt",
        text="Регистрация на форум «Амур» закрыта, статус заявки доступен в кабинете.",
        metadata={
            "category": "форумы",
            "forum_normalized": forum,
            "source_type": "xlsx",
            "topic": "podacha_zayavki_na_proekt",
        },
        score=0.9,
        reranker_score=0.7,
    )
    decline = ScoredChunk(
        chunk_id="xlsx_category_r0219_otkaz_ot_uchastiya",
        text="Если решишь отказаться от участия, пожалуйста, сообщи нам.",
        metadata={
            "category": "форумы",
            "forum_normalized": forum,
            "source_type": "xlsx",
            "topic": "otkaz_ot_uchastiya",
        },
        score=1.0,
        reranker_score=0.7,
    )
    llm = CapturingLLM(
        "Регистрация закрыта, статус заявки можно смотреть в кабинете. "
        "[src:xlsx_category_r0218_podacha_zayavki_na_proekt]\n\n"
        "Если нужно отказаться от участия, сообщи нам. "
        "[src:xlsx_category_r0219_otkaz_ot_uchastiya]"
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized=forum,
                questions=[
                    Question(
                        text="Как подать заявку?",
                        category="форумы",
                        forum_normalized=forum,
                        topic="podacha_zayavki_na_proekt",
                    ),
                    Question(
                        text="Что делать, если не получается поехать?",
                        category="форумы",
                        forum_normalized=forum,
                        topic="otkaz_ot_uchastiya",
                    ),
                ],
            ),
            "message_masked": "Амур: как подать заявку и можно ли потом отказаться?",
            "reranked_chunks": [application, decline],
            "max_confidence": 0.7,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert "xlsx_category_r0218_podacha_zayavki_na_proekt" in llm.kwargs[0]["user"]
    assert "xlsx_category_r0219_otkaz_ot_uchastiya" in llm.kwargs[0]["user"]
    assert result["cited_sources"] == [
        "xlsx_category_r0218_podacha_zayavki_na_proekt",
        "xlsx_category_r0219_otkaz_ot_uchastiya",
    ]


def test_source_chunk_response_deduplicates_repeated_paragraphs_and_links() -> None:
    chunks = [
        ScoredChunk(
            chunk_id="registration",
            text=(
                "❗️ Сейчас регистрация на мероприятие закрыта.\n"
                "Пожалуйста, ожидай обновлений на платформе https://events.myrosmol.ru/"
            ),
            metadata={"source_type": "xlsx"},
            score=0.8,
            reranker_score=0.8,
        ),
        ScoredChunk(
            chunk_id="age",
            text=(
                "Участие доступно пользователям от 14 до 35 лет включительно.\n"
                "❗️ Сейчас регистрация на мероприятие закрыта.\n"
                "Пожалуйста, ожидай обновлений на платформе https://events.myrosmol.ru/"
            ),
            metadata={"source_type": "xlsx"},
            score=0.8,
            reranker_score=0.8,
        ),
    ]

    response = build_deterministic_source_response(chunks)

    assert response is not None
    assert response.count("Сейчас регистрация на мероприятие закрыта") == 1
    assert response.count("https://events.myrosmol.ru/") == 1
    assert "[src:registration]" in response
    assert "[src:age]" in response


@pytest.mark.asyncio
async def test_generate_uses_decline_chunk_for_cannot_go_followup_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    travel = ScoredChunk(
        chunk_id="amur_travel",
        text="Проезд участников форума оплачивается направляющей стороной.",
        metadata={
            "category": "форумы",
            "forum_normalized": "Амур",
            "source_type": "xlsx",
            "topic": "oplata_proezda",
            "intent_name": "Оплата проезда",
        },
        score=0.96,
        reranker_score=0.95,
    )
    decline = ScoredChunk(
        chunk_id="amur_decline",
        text=(
            "Если ты успешно пройдёшь конкурсный отбор, но затем решишь отказаться "
            "от участия — пожалуйста, обязательно сообщи нам."
        ),
        metadata={
            "category": "форумы",
            "forum_normalized": "Амур",
            "source_type": "xlsx",
            "topic": "otkaz_ot_uchastiya",
            "intent_name": "Отказ от участия",
            "intent_examples": [
                "Подала согласие на заявку, но принять участие не смогу",
                "Я не смогу приехать.",
                "Как отказаться от участия?",
            ],
        },
        score=0.72,
        reranker_score=0.7,
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Амур",
            ),
            "message_masked": (
                "А что делать, если я уже подтвердил участие, но теперь не могу поехать?"
            ),
            "contextual_message": (
                "Амур: А что делать, если я уже подтвердил участие, "
                "но теперь не могу поехать?"
            ),
            "reranked_chunks": [travel, decline],
            "max_confidence": 0.95,
            "llm_client": FailingLLM(),
        }
    )

    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == ["amur_decline"]
    assert "отказаться от участия" in result["generated_response"]
    assert "Проезд участников" not in result["generated_response"]


@pytest.mark.asyncio
async def test_generate_complex_message_uses_multiple_fallback_aspect_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(reranker_threshold_low=0.4, reranker_threshold_high=0.7),
    )
    chunks = [
        ScoredChunk(
            chunk_id="docs",
            text="Для поездки нужны паспорт и медицинская справка.",
            metadata={"forum_normalized": "Российский Север", "category": "форумы"},
            score=0.8,
            reranker_score=0.82,
        ),
        ScoredChunk(
            chunk_id="transfer",
            text="Для участников будет организован бесплатный трансфер.",
            metadata={"forum_normalized": "Российский Север", "category": "форумы"},
            score=0.8,
            reranker_score=0.8,
        ),
        ScoredChunk(
            chunk_id="food",
            text="На площадке будут организованы точки питания и питьевая вода.",
            metadata={"forum_normalized": "Российский Север", "category": "форумы"},
            score=0.8,
            reranker_score=0.78,
        ),
    ]
    llm = CapturingLLM(
        "Нужны паспорт и медицинская справка. [src:docs]\n\n"
        "Трансфер для участников бесплатный. [src:transfer]\n\n"
        "На площадке будут точки питания и питьевая вода. [src:food]"
    )

    result = await generate(
        {
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized="Российский Север",
                questions=[],
            ),
            "message_masked": (
                "Российский Север: какие документы нужны, есть ли трансфер и питание?"
            ),
            "reranked_chunks": chunks,
            "max_confidence": 0.82,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert llm.kwargs[0]["model"] == "GigaChat/GigaChat-2-Max"
    assert result["generator_model"] == "GigaChat/GigaChat-2-Max"
    assert result["cited_sources"] == ["docs", "transfer", "food"]


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
async def test_generate_escalates_for_uncovered_complex_question(
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

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"
    assert result["generator_model"] == "source_only"


@pytest.mark.asyncio
async def test_generate_escalates_when_masked_message_is_not_covered(
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

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"
