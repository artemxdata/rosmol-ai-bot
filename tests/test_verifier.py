from __future__ import annotations

import pytest

from src.graph.nodes.verify import _allows_partial_source_response, verify
from src.models import QueryAnalysis, Question, ScoredChunk


class JudgeLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.requests: list[dict] = []

    async def generate(self, **kwargs) -> str:
        self.calls += 1
        self.requests.append(kwargs)
        return self.payload


def test_allows_partial_source_response_only_with_explicit_missing_note() -> None:
    assert _allows_partial_source_response(
        {
            "generated_response": (
                "Ответ по найденным источникам.\n\n"
                "По этим пунктам в базе нет подтверждённых данных: документы."
            ),
            "partial_source_missing_coverage": ["документы"],
        },
        ["документы"],
    )
    assert not _allows_partial_source_response(
        {
            "generated_response": "Ответ по найденным источникам.",
            "partial_source_missing_coverage": ["документы"],
        },
        ["документы"],
    )
    assert not _allows_partial_source_response(
        {
            "generated_response": "По этим пунктам в базе нет подтверждённых данных: документы.",
        },
        ["документы"],
    )


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
async def test_verifier_escalates_unsupported_registration_instruction() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 1.0}')

    result = await verify(
        {
            "generated_response": (
                "Обратитесь напрямую к организаторам фестиваля через адрес электронной почты. "
                "Предоставьте следующую информацию: полные ФИО, регион и населённый пункт, "
                "электронную почту. Это поможет уточнить вашу регистрацию. [src:reg]"
            ),
            "generator_model": "GigaChat/GigaChat-2-Max",
            "cited_sources": ["reg"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="reg",
                    text=(
                        "Конкурсный отбор уже завершён, победители получили уведомления 5 июня. "
                        "На фестивальный день регистрация открыта до 11 июля включительно."
                    ),
                    metadata={"forum_normalized": "Больше, чем путешествие"},
                    reranker_score=0.9,
                )
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["verification"].has_hallucination is True
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "unsupported_instruction"


@pytest.mark.asyncio
async def test_verifier_blocks_organizer_recipient_when_source_says_us() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 1.0}')

    result = await verify(
        {
            "generated_response": (
                "Если уже подтвердили участие, но не можете поехать, "
                "сообщите организаторам. [src:decline]"
            ),
            "generator_model": "GigaChat/GigaChat-2-Max",
            "cited_sources": ["decline"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="decline",
                    text=(
                        "Если ты успешно пройдёшь конкурсный отбор, но затем решишь "
                        "отказаться от участия — пожалуйста, сообщи нам. Мы обязательно поможем!"
                    ),
                    metadata={"forum_normalized": "Амур"},
                    reranker_score=0.9,
                )
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["verification"].has_hallucination is True
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "unsupported_instruction"


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
async def test_verifier_escalates_single_cited_forum_when_forum_context_is_ambiguous() -> None:
    result = await verify(
        {
            "message_masked": "Подать заявку на участие",
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": (
                "Подать заявку на форум «Утро» можно в личном кабинете. "
                "[src:utro_application]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["utro_application"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="utro_application",
                    text="Подать заявку на форум «Утро» можно в личном кабинете.",
                    metadata={"forum_normalized": "Утро"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="mashuk_application",
                    text="Подать заявку на форум «Машук» можно в личном кабинете.",
                    metadata={"forum_normalized": "Машук"},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"


@pytest.mark.asyncio
async def test_verifier_escalates_single_forum_source_for_unanchored_forum_question() -> None:
    result = await verify(
        {
            "message_masked": "Подать заявку на участие",
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": (
                "Подать заявку на форум «Утро» можно в личном кабинете. "
                "[src:utro_application]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["utro_application"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="utro_application",
                    text="Подать заявку на форум «Утро» можно в личном кабинете.",
                    metadata={"forum_normalized": "Утро"},
                    reranker_score=0.9,
                )
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"


@pytest.mark.asyncio
async def test_verifier_allows_generic_platform_registration_source_with_forum_candidates() -> None:
    result = await verify(
        {
            "message_masked": "Как зарегистрироваться на ФГАИС?",
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "generated_response": (
                "Пройти регистрацию во ФГАИС можно по ссылке: "
                "https://myrosmol.ru/auth/register [src:fgais_registration]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["fgais_registration"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="fgais_registration",
                    text="Пройти регистрацию во ФГАИС можно по ссылке: https://myrosmol.ru/auth/register",
                    metadata={"category": "платформа_фгаис"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="utro_application",
                    text="Подать заявку на форум «Утро» можно в личном кабинете.",
                    metadata={"forum_normalized": "Утро"},
                    reranker_score=0.7,
                ),
                ScoredChunk(
                    chunk_id="mashuk_application",
                    text="Подать заявку на форум «Машук» можно в личном кабинете.",
                    metadata={"forum_normalized": "Машук"},
                    reranker_score=0.7,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_allows_unscoped_technical_application_issue() -> None:
    result = await verify(
        {
            "message_masked": "Не получается выбрать направление в заявке",
            "analysis": QueryAnalysis(category="техподдержка"),
            "generated_response": (
                "Попробуйте очистить кеш и cookie браузера, открыть сайт в другом браузере "
                "и повторить попытку. [src:technical_error]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["technical_error"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="technical_error",
                    text=(
                        "При технической ошибке очистите кеш и cookie браузера, "
                        "откройте сайт в другом браузере и повторите попытку."
                    ),
                    metadata={"category": "техподдержка"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="forum_application",
                    text="Подать заявку на форум можно в личном кабинете.",
                    metadata={"forum_normalized": "Утро", "category": "форумы"},
                    reranker_score=0.7,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_skips_judge_for_low_confidence_official_technical_fallback() -> None:
    llm = JudgeLLM('{"has_hallucination": true, "confidence": 0}')

    result = await verify(
        {
            "message_masked": "Не могу выбрать проект при заполнении заявки",
            "analysis": QueryAnalysis(category="техподдержка"),
            "generated_response": (
                "Сожалеем, что пришлось столкнуться с техническими сложностями. "
                "Попробуй выполнить следующие действия: очисти кеш и cookie браузера, "
                "открой сайт в другом браузере, попробуй зайти с другого устройства, "
                "убедись, что VPN выключен, подожди некоторое время и повтори попытку. "
                "Если ошибка сохраняется, опиши текстом, на каком этапе возникает ошибка "
                "и какой текст ошибки отображается."
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["technical_error"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="technical_error",
                    text=(
                        "Сожалеем, что пришлось столкнуться с техническими сложностями. "
                        "Попробуй выполнить следующие действия: очисти кеш и cookie браузера, "
                        "открой сайт в другом браузере, попробуй зайти с другого устройства, "
                        "убедись, что VPN выключен, подожди некоторое время и повтори попытку. "
                        "Если ошибка сохраняется, опиши текстом: на каком этапе возникает ошибка, "
                        "какой текст ошибки отображается, что именно не загружается, "
                        "не сохраняется или не отправляется."
                    ),
                    metadata={
                        "category": "техподдержка",
                        "topic": "tehnicheskaya_oshibka",
                        "source_type": "xlsx",
                        "intent_examples": [
                            "Не получается выбрать направление",
                            "При подаче заявки возникает ошибка",
                            "Не могу завершить заполнение заявки",
                        ],
                    },
                    reranker_score=0.001,
                )
            ],
            "max_confidence": 0.015,
            "llm_client": llm,
        }
    )

    assert result["verification"].has_hallucination is False
    assert "should_escalate" not in result
    assert result["verifier_triggered"] is False
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_verifier_escalates_unanchored_forum_call_letter_source() -> None:
    result = await verify(
        {
            "message_masked": "Можно получить письмо-вызов на форум для работы?",
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": (
                "Нужно официальное письмо-вызов? Заполни форму участника. "
                "[src:dobrino_call_letter]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["dobrino_call_letter"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="dobrino_call_letter",
                    text="Письмо-вызов для форума «Добрино» можно получить через форму.",
                    metadata={"category": "форумы", "forum_normalized": "Добрино"},
                    reranker_score=0.7,
                )
            ],
            "max_confidence": 0.7,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"
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
async def test_verifier_skips_judge_for_high_confidence_official_llm_answer() -> None:
    llm = JudgeLLM('{"has_hallucination": true, "confidence": 0.0}')

    result = await verify(
        {
            "generated_response": "Ответ по официальному источнику. [src:ctx_1]",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "cited_sources": ["ctx_1"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="ctx_1",
                    text="Официальный источник.",
                    metadata={"source_type": "xlsx"},
                    reranker_score=0.9,
                )
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["verification"].has_hallucination is False
    assert result["verification"].triggered_llm_judge is False
    assert result["verifier_triggered"] is False


@pytest.mark.asyncio
async def test_verifier_judge_uses_only_cited_sources() -> None:
    llm = JudgeLLM('{"has_hallucination": false, "confidence": 0.91, "details": "grounded"}')

    result = await verify(
        {
            "generated_response": "Answer from cited source. [src:ctx_1]",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "cited_sources": ["ctx_1"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="ctx_1",
                    text="Cited source text.",
                    metadata={},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="ctx_2",
                    text="Uncited source text should not be sent to judge.",
                    metadata={},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert result["verification"].has_hallucination is False
    assert llm.calls == 1
    assert llm.requests[0]["max_tokens"] == 200
    assert "[src:ctx_1]" in llm.requests[0]["user"]
    assert "Cited source text." in llm.requests[0]["user"]
    assert "ctx_2" not in llm.requests[0]["user"]
    assert "Uncited source text" not in llm.requests[0]["user"]


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
async def test_verifier_escalates_when_answer_says_sources_lack_sufficient_data() -> None:
    result = await verify(
        {
            "generated_response": (
                "В источниках нет достаточных данных о конкретных документах. "
                "Однако указано, что положение будет доступно в карточке мероприятия."
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
async def test_verifier_adds_missing_note_for_cited_partial_source_answer() -> None:
    result = await verify(
        {
            "message_masked": (
                "Нужен ли ноутбук, как доехать до площадки и можно ли отказаться от участия?"
            ),
            "analysis": QueryAnalysis(category="форумы"),
            "generated_response": (
                "До площадки можно добраться трансфером. "
                "Если нужно отказаться от участия, отзови заявку в личном кабинете."
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["travel", "cancel"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="travel",
                    text="До площадки можно добраться на трансфере от вокзала.",
                    metadata={"intent_name": "Трансфер и проезд"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="cancel",
                    text="Если нужно отказаться от участия, отзови заявку в личном кабинете.",
                    metadata={"intent_name": "Отказ от участия"},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert result["verification"].has_hallucination is False
    assert result["should_escalate"] is False
    assert result["escalation_reason"] is None
    assert "в базе нет подтверждённых данных" in result["generated_response"]
    assert result["partial_source_missing_coverage"]


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
async def test_verifier_allows_date_marker_coverage_from_topic_alias() -> None:
    result = await verify(
        {
            "message_masked": "День молодёжи: когда проходит событие?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="День молодёжи",
            ),
            "generated_response": "27 июня 2026 года по всей стране пройдёт День молодёжи.",
            "generator_model": "source_chunk",
            "cited_sources": ["date"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="date",
                    text="27 июня 2026 года по всей стране пройдёт День молодёжи.",
                    metadata={
                        "source_type": "xlsx",
                        "category": "форумы",
                        "forum_normalized": "День молодёжи",
                        "topic": "sut_festivalya_i_data",
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


@pytest.mark.asyncio
async def test_verifier_allows_overview_marker_coverage_from_topic_alias() -> None:
    result = await verify(
        {
            "message_masked": "Российский Север: в чём суть форума?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Российский Север",
            ),
            "generated_response": "Форум посвящён развитию молодых специалистов Севера.",
            "generator_model": "source_chunk",
            "cited_sources": ["overview"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="overview",
                    text="Форум посвящён развитию молодых специалистов Севера.",
                    metadata={
                        "source_type": "xlsx",
                        "category": "форумы",
                        "forum_normalized": "Российский Север",
                        "topic": "o_meropriyatii",
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


@pytest.mark.asyncio
async def test_verifier_allows_items_coverage_from_document_topic_metadata() -> None:
    result = await verify(
        {
            "message_masked": (
                "Больше, чем путешествие: какие вещи взять, что с медпунктом и можно ли с ОВЗ?"
            ),
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Больше, чем путешествие",
            ),
            "generated_response": (
                "Список вещей будет в документах участника. "
                "На площадке есть медпункт. "
                "Участники с ОВЗ могут участвовать по правилам источника."
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["docs", "medical", "ovz"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="docs",
                    text="Документы и список вещей будут доступны участникам.",
                    metadata={
                        "forum_normalized": "Больше, чем путешествие",
                        "topic": "dokumenty_meropriyatiya",
                        "intent_name": "Документы мероприятия",
                    },
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="medical",
                    text="На площадке предусмотрен медпункт.",
                    metadata={
                        "forum_normalized": "Больше, чем путешествие",
                        "topic": "informaciya_o_ploschadke_medicina",
                        "intent_name": "Информация о площадке. Медицина",
                    },
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="ovz",
                    text="Участники с ОВЗ могут участвовать с учётом условий площадки.",
                    metadata={
                        "forum_normalized": "Больше, чем путешествие",
                        "topic": "uchastniki_s_ovz",
                        "intent_name": "Участники с ОВЗ",
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


@pytest.mark.asyncio
async def test_verifier_does_not_split_expert_feedback_into_application_and_results() -> None:
    result = await verify(
        {
            "message_masked": (
                "предоставить подробную обратную связь по результатам "
                "экспертной оценки моей заявки"
            ),
            "analysis": QueryAnalysis(category="гранты"),
            "generated_response": (
                "Чтобы получить обратную связь по заявке, зайди в профиль и выбери "
                "«Мои заявки». Обратная связь предоставляется участникам в течение 60 дней."
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["expert_feedback"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="expert_feedback",
                    text=(
                        "Чтобы получить обратную связь по заявке, зайди в профиль и "
                        "выбери «Мои заявки». Обратная связь предоставляется участникам "
                        "в течение 60 дней."
                    ),
                    metadata={
                        "intent_name": "Запрос обратной связи куратора",
                        "topic": "zapros_obratnoy_svyazi_kuratora",
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
async def test_verifier_escalates_forum_details_without_forum_name() -> None:
    message = (
        "\u041c\u043d\u0435 17 \u043b\u0435\u0442, "
        "\u043a\u0442\u043e \u043e\u043f\u043b\u0430\u0447\u0438\u0432\u0430\u0435\u0442 "
        "\u0434\u043e\u0440\u043e\u0433\u0443 \u0438 \u0433\u0434\u0435 "
        "\u0436\u0438\u0442\u044c \u043d\u0430 \u0444\u043e\u0440\u0443\u043c\u0435?"
    )
    category = "\u0444\u043e\u0440\u0443\u043c\u044b"
    gosstart = "\u0413\u043e\u0441\u0421\u0442\u0430\u0440\u0442"
    morning = "\u0423\u0442\u0440\u043e"

    result = await verify(
        {
            "message_masked": message,
            "analysis": QueryAnalysis(category=category),
            "generated_response": "Answer mixes forum facts. [src:age] [src:travel]",
            "generator_model": "GigaChat/GigaChat-2-Max",
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="age",
                    text="Age restrictions for one forum.",
                    metadata={"forum_normalized": gosstart},
                    reranker_score=0.8,
                ),
                ScoredChunk(
                    chunk_id="travel",
                    text="Travel and lodging details for another forum.",
                    metadata={"forum_normalized": morning},
                    reranker_score=0.7,
                ),
            ],
            "cited_sources": ["age", "travel"],
            "max_confidence": 0.8,
        }
    )

    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "ambiguous_forum_context"
    assert gosstart in result["verification"].details
    assert morning in result["verification"].details


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


@pytest.mark.asyncio
async def test_verifier_does_not_require_forum_for_grant_application_question() -> None:
    result = await verify(
        {
            "message_masked": (
                "Хочу зарегистрироваться на конкурс грантов. "
                "Что указать в поле выбора проекта?"
            ),
            "analysis": QueryAnalysis(category="гранты"),
            "generated_response": (
                "Для участия в грантовом конкурсе нужно выбрать проект в личном кабинете. "
                "[src:grant_application]"
            ),
            "generator_model": "source_chunk",
            "cited_sources": ["grant_application"],
            "reranked_chunks": [
                ScoredChunk(
                    chunk_id="grant_application",
                    text=(
                        "Чтобы зарегистрироваться на грантовый конкурс и подать заявку, "
                        "выбери проект в личном кабинете ФГАИС."
                    ),
                    metadata={"forum_normalized": "Амур", "category": "гранты"},
                    reranker_score=0.9,
                ),
                ScoredChunk(
                    chunk_id="forum_application",
                    text="Подать заявку на форум можно в личном кабинете.",
                    metadata={"forum_normalized": "Утро", "category": "форумы"},
                    reranker_score=0.8,
                ),
            ],
            "max_confidence": 0.9,
        }
    )

    assert "should_escalate" not in result
