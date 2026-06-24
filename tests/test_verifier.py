from __future__ import annotations

import pytest

from src.graph.nodes.verify import verify
from src.models import QueryAnalysis, Question, ScoredChunk


class JudgeLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def generate(self, **kwargs) -> str:
        self.calls += 1
        return self.payload


@pytest.mark.asyncio
async def test_verifier_rejects_unknown_source_marker() -> None:
    result = await verify(
        {
            "generated_response": "Ответ [src:missing]",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is True


@pytest.mark.asyncio
async def test_verifier_blocks_llm_answer_without_source_citations() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 1.0}')

    result = await verify(
        {
            "generated_response": "Проезд оплачивает направляющая сторона.",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="travel",
                    text="Проезд оплачивает направляющая сторона.",
                    metadata={},
                    reranker_score=0.9,
                )
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["verification"].has_hallucination is True
    assert result["verifier_triggered"] is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "missing_source_citations"


@pytest.mark.asyncio
async def test_verifier_accepts_high_confidence_without_judge() -> None:
    result = await verify(
        {
            "generated_response": "Ответ [src:ctx_1]",
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_uses_answer_bank_intent_examples_for_coverage() -> None:
    result = await verify(
        {
            "message_masked": "Как получить консультацию по отчетности?",
            "analysis": QueryAnalysis(
                questions=[Question(text="Как получить консультацию по отчетности?")]
            ),
            "generated_response": (
                "Свяжитесь с куратором грантового конкурса. [src:ticket_answer_bank_001]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["ticket_answer_bank_001"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="ticket_answer_bank_001",
                    text="Свяжитесь с куратором грантового конкурса.",
                    metadata={
                        "source_type": "ticket_answer_bank",
                        "intent_examples": ["Как получить консультацию по отчетности?"],
                    },
                    reranker_score=0.9,
                )
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_judges_llm_generation_even_with_high_confidence() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 0.88, "details": "grounded"}')

    result = await verify(
        {
            "generated_response": "Ответ по источнику. [src:ctx_1]",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["verification"].has_hallucination is False
    assert result["verification"].triggered_llm_judge is True
    assert result["verifier_triggered"] is True


@pytest.mark.asyncio
async def test_verifier_rejects_no_question_response_when_user_asked_question() -> None:
    result = await verify(
        {
            "message_masked": "Какие документы нужны на Российский Север?",
            "generated_response": "Похоже, у вас пока нет вопросов. Задайте ваш вопрос.",
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.9)
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is True
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_escalates_when_answer_admits_sources_are_insufficient() -> None:
    result = await verify(
        {
            "generated_response": (
                "В предоставленных источниках нет информации о возврате средств. "
                "Рекомендую направить ваш вопрос специалисту службы поддержки."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.7)
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_escalates_polite_specialist_handoff_for_missing_facts() -> None:
    result = await verify(
        {
            "generated_response": (
                "В предоставленных источниках нет информации о порядке обращения. "
                "Пожалуйста, передайте ваш вопрос специалисту для получения точной консультации."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.7)
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_when_answer_says_info_absent_and_redirects() -> None:
    result = await verify(
        {
            "generated_response": (
                "Из представленных источников невозможно ответить на вопрос. "
                "Информация об условиях отсутствует. Рекомендую обратиться к специалистам."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_when_answer_says_source_has_no_info() -> None:
    result = await verify(
        {
            "generated_response": (
                "В источнике нет информации по вашему вопросу. "
                "Рекомендую обратиться к специалисту для получения детальных сведений."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.5)
            ],
            "max_confidence": 0.5,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_absent_info_even_without_redirect() -> None:
    result = await verify(
        {
            "generated_response": (
                "Информация по запросу «Госстарт.Стажировки - Федеральный этап» "
                "в источниках отсутствует."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.5)
            ],
            "max_confidence": 0.5,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_fact_not_specified_in_sources() -> None:
    result = await verify(
        {
            "generated_response": (
                "Точная дата проведения мероприятия не указана в предоставленных источниках. "
                "Трансфер организован от аэропорта."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.5)
            ],
            "max_confidence": 0.5,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_concrete_info_with_care_service_redirect() -> None:
    result = await verify(
        {
            "generated_response": (
                "В предоставленных источниках нет конкретной информации о возврате средств. "
                "Рекомендую обратиться напрямую в Службу Заботы Росмолодёжи."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.7)
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_precise_info_with_support_redirect() -> None:
    result = await verify(
        {
            "generated_response": (
                "На данный момент точной информации о различиях между форумами нет. "
                "Рекомендуем обратиться непосредственно в службу поддержки Росмолодёжи."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_info_with_direct_handoff() -> None:
    result = await verify(
        {
            "generated_response": (
                "Точная информация о различиях между форумами отсутствует. "
                "Передам ваш запрос специалисту для уточнения деталей."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_escalates_missing_info_with_this_request_handoff() -> None:
    result = await verify(
        {
            "generated_response": (
                "Информация о порядке оплаты проезда на форуме Машук отсутствует. "
                "Передайте этот запрос специалисту для уточнения деталей."
            ),
            "reranked_chunks": [
                ScoredChunk(chunk_id="ctx_1", text="Источник", metadata={}, reranker_score=0.8)
            ],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "insufficient_sources"


@pytest.mark.asyncio
async def test_verifier_allows_support_instruction_when_sources_are_sufficient() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 0.9, "details": "grounded"}')

    result = await verify(
        {
            "generated_response": (
                "Если проблема сохраняется, обратитесь в службу поддержки с описанием ошибки. "
                "[src:ctx_1]"
            ),
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="ctx_1",
                    text="При технической ошибке обратитесь в службу поддержки.",
                    metadata={},
                    reranker_score=0.7,
                )
            ],
            "max_confidence": 0.7,
            "llm_client": llm,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_verifier_escalates_multi_forum_partial_source_coverage() -> None:
    result = await verify(
        {
            "message_masked": (
                "Чем отличаются Машук и Территория смыслов по регистрации, "
                "проживанию и оплате проезда?"
            ),
            "analysis": QueryAnalysis(
                category="форумы",
                extracted_params={"detected_forums": ["Машук", "Территория смыслов"]},
            ),
            "generated_response": "По Территории смыслов есть регистрация, по Машуку есть проезд.",
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="territory_registration",
                    text="Чтобы подать заявку на Территорию смыслов, зарегистрируйтесь в ФГАИС.",
                    metadata={
                        "forum_normalized": "Территория смыслов",
                        "intent_name": "Регистрация",
                    },
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="territory_lodging",
                    text="Проживание на форуме Территория смыслов организовано на площадке.",
                    metadata={
                        "forum_normalized": "Территория смыслов",
                        "intent_name": "Проживание",
                    },
                    reranker_score=0.8,
                ),
                ScoredChunk(
                    chunk_id="territory_travel",
                    text="Проезд до площадки Территории смыслов от Москвы оплачивают организаторы.",
                    metadata={
                        "forum_normalized": "Территория смыслов",
                        "intent_name": "Оплата проезда",
                    },
                    reranker_score=0.8,
                ),
                ScoredChunk(
                    chunk_id="mashuk_travel",
                    text="Билеты до Пятигорска на форум Машук участник оплачивает самостоятельно.",
                    metadata={"forum_normalized": "Машук", "intent_name": "Оплата проезда"},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "partial_source_coverage"
    assert "Машук" in result["verification"].details


@pytest.mark.asyncio
async def test_verifier_escalates_multi_aspect_partial_source_coverage() -> None:
    result = await verify(
        {
            "message_masked": (
                "Нужен ли ноутбук, как доехать до площадки и можно ли отказаться от участия?"
            ),
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": (
                "До площадки можно добраться трансфером. "
                "Если нужно отказаться от участия, отзовите заявку в личном кабинете."
            ),
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="travel",
                    text="До площадки можно добраться на трансфере от вокзала.",
                    metadata={"intent_name": "Трансфер и проезд"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="cancel",
                    text="Если нужно отказаться от участия, отзовите заявку в личном кабинете.",
                    metadata={"intent_name": "Отказ от участия"},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "partial_source_coverage"
    assert "Что нужно взять с собой?" in result["verification"].details


@pytest.mark.asyncio
async def test_verifier_allows_multi_aspect_answer_when_sources_cover_each_aspect() -> None:
    result = await verify(
        {
            "message_masked": (
                "Нужен ли ноутбук, как доехать до площадки и можно ли отказаться от участия?"
            ),
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": (
                "Возьмите ноутбук, если он нужен для вашей программы. "
                "До площадки можно добраться трансфером. "
                "Если нужно отказаться от участия, отзовите заявку в личном кабинете."
            ),
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="items",
                    text="Что взять с собой: ноутбук, зарядное устройство и удобную одежду.",
                    metadata={"intent_name": "Вещи и снаряжение"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="travel",
                    text="До площадки можно добраться на трансфере от вокзала.",
                    metadata={"intent_name": "Трансфер и проезд"},
                    reranker_score=0.8,
                ),
                ScoredChunk(
                    chunk_id="cancel",
                    text="Если нужно отказаться от участия, отзовите заявку в личном кабинете.",
                    metadata={"intent_name": "Отказ от участия"},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_does_not_escalate_single_aspect_source_coverage() -> None:
    result = await verify(
        {
            "message_masked": "Можно ли отказаться от участия?",
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": "Если нужно отказаться от участия, отзовите заявку.",
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="cancel",
                    text="Если нужно отказаться от участия, отзовите заявку в личном кабинете.",
                    metadata={"intent_name": "Отказ от участия"},
                    reranker_score=0.9,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_escalates_ambiguous_forum_specific_sources() -> None:
    result = await verify(
        {
            "message_masked": (
                "Можно приехать в Кемерово вечером и будет ли трансфер до площадки?"
            ),
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": "Можно приехать, трансфер будет. [src:morning] [src:sheregesh]",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="morning",
                    text="Информация о форуме Утро.",
                    metadata={"forum_normalized": "Утро"},
                    reranker_score=0.6,
                ),
                ScoredChunk(
                    chunk_id="sheregesh",
                    text="Трансфер от Кемерово до Шерегеша.",
                    metadata={"forum_normalized": "Шерегеш"},
                    reranker_score=0.5,
                ),
            ],
            "cited_sources": ["morning", "sheregesh"],
            "max_confidence": 0.6,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"
    assert "Утро" in result["verification"].details


@pytest.mark.asyncio
async def test_verifier_escalates_single_cited_forum_when_candidates_are_ambiguous() -> None:
    message = (
        "\u041c\u043e\u0436\u043d\u043e "
        "\u043f\u0440\u0438\u0435\u0445\u0430\u0442\u044c "
        "\u0432 \u041a\u0435\u043c\u0435\u0440\u043e\u0432\u043e "
        "\u0438 \u0431\u0443\u0434\u0435\u0442 \u043b\u0438 "
        "\u0442\u0440\u0430\u043d\u0441\u0444\u0435\u0440?"
    )
    category = "\u0444\u043e\u0440\u0443\u043c\u044b"
    response = (
        "\u0422\u0440\u0430\u043d\u0441\u0444\u0435\u0440 "
        "\u0431\u0443\u0434\u0435\u0442. [src:morning]"
    )
    morning = "\u0423\u0442\u0440\u043e"
    sheregesh = "\u0428\u0435\u0440\u0435\u0433\u0435\u0448"
    result = await verify(
        {
            "message_masked": message,
            "analysis": QueryAnalysis(category=category),
            "generated_response": response,
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="morning",
                    text=f"{response} {morning}.",
                    metadata={"forum_normalized": morning},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="sheregesh",
                    text=f"{response} {sheregesh}.",
                    metadata={"forum_normalized": sheregesh},
                    reranker_score=0.8,
                ),
            ],
            "cited_sources": ["morning"],
            "max_confidence": 0.9,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"

@pytest.mark.asyncio
async def test_verifier_allows_generic_multi_forum_sources_without_forum_specific_query() -> None:
    result = await verify(
        {
            "message_masked": "Можно ли получить письмо-вызов?",
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": "Письмо-вызов можно получить по запросу. [src:a] [src:b]",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="a",
                    text="Письмо-вызов можно получить по запросу.",
                    metadata={"forum_normalized": "Утро"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="b",
                    text="Письмо-вызов можно получить по запросу.",
                    metadata={"forum_normalized": "Машук"},
                    reranker_score=0.9,
                ),
            ],
            "cited_sources": ["a", "b"],
            "max_confidence": 0.9,
        }
    )

    assert "should_escalate" not in result


@pytest.mark.asyncio
async def test_verifier_allows_multi_forum_answer_when_sources_cover_each_forum() -> None:
    result = await verify(
        {
            "message_masked": "Кто оплачивает проезд на Машук и Территорию смыслов?",
            "analysis": QueryAnalysis(
                category="форумы",
                extracted_params={"detected_forums": ["Машук", "Территория смыслов"]},
            ),
            "generated_response": (
                "Проезд отличается по форумам [src:mashuk_travel] [src:territory_travel]"
            ),
            "generator_model": "source_chunk",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="mashuk_travel",
                    text="Билеты до Пятигорска на форум Машук участник оплачивает самостоятельно.",
                    metadata={"forum_normalized": "Машук", "intent_name": "Оплата проезда"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="territory_travel",
                    text="Проезд до площадки Территории смыслов от Москвы оплачивают организаторы.",
                    metadata={
                        "forum_normalized": "Территория смыслов",
                        "intent_name": "Оплата проезда",
                    },
                    reranker_score=0.9,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False
