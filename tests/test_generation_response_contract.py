from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.graph.nodes.analyze import _fallback_analysis
from src.graph.nodes.generate import (
    COMPLEX_RESPONSE_MAX_CHARS,
    SIMPLE_RESPONSE_MAX_CHARS,
    _claim_fact_numbers,
    _condition_keys,
    _is_redundant_source_chunk,
    _linked_named_section_date_source,
    _llm_claims_have_bound_source_facts,
    _question_topic_group,
    _source_chunk_covers_question,
    _source_matches_explicit_question_constraints,
    generate,
)
from src.graph.nodes.verify import verify
from src.graph.question_utils import build_effective_questions
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import Chunk, Complexity, QueryAnalysis, Question, ScoredChunk
from src.response_contract import ResponseProfileName


class RaisingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs) -> str:
        self.calls += 1
        raise RuntimeError("generator unavailable")


class StubLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.requests: list[dict] = []

    async def generate(self, **kwargs) -> str:
        self.calls += 1
        self.requests.append(kwargs)
        return self.response


class SequenceLLM(StubLLM):
    def __init__(self, responses: list[str]) -> None:
        super().__init__("")
        self.responses = responses

    async def generate(self, **kwargs) -> str:
        self.calls += 1
        self.requests.append(kwargs)
        return self.responses[self.calls - 1]


def test_question_topic_alias_normalization_is_bounded() -> None:
    assert (
        _question_topic_group(
            Question(text="Кто оплачивает дорогу?", topic="оплата_проезда")
        )
        == _question_topic_group(
            Question(text="Кто оплачивает дорогу?", topic="oplata_proezda")
        )
    )
    assert (
        _question_topic_group(
            Question(
                text="Какие документы нужны для подачи заявки?",
                topic="документы_для_заявки",
            )
        )
        == "документы_для_заявки"
    )


def test_claim_fact_numbers_ignores_each_numbered_list_marker() -> None:
    assert _claim_fact_numbers("1. Первый подтверждённый пункт\n2. Второй пункт") == set()


def test_claim_fact_numbers_keeps_numbers_inside_numbered_list_items() -> None:
    assert _claim_fact_numbers(
        "1. Участники 18–35 лет\n2. Заявки принимают до 20 августа 2026 года"
    ) == {"18", "20", "35", "2026"}


def _source(text: str) -> ScoredChunk:
    return ScoredChunk(
        chunk_id="yonote_program",
        text=text,
        metadata={
            "chunk_id": "yonote_program",
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "programma_i_artisty",
            "intent_examples": ["Что будет в программе Машука?"],
        },
        score=0.95,
        reranker_score=0.95,
    )


def _state(chunk: ScoredChunk, llm_client: object, *, complexity: Complexity) -> dict:
    return {
        "message_masked": "Что будет в программе Машука?",
        "analysis": QueryAnalysis(
            complexity=complexity,
            category="форумы",
            forum_normalized="Машук",
            questions=[
                Question(
                    text="Что будет в программе Машука?",
                    category="форумы",
                    forum_normalized="Машук",
                    topic="programma_i_artisty",
                )
            ],
        ),
        "reranked_chunks": [chunk],
        "max_confidence": 0.95,
        "llm_client": llm_client,
    }


def _multi_source_state(llm_client: object) -> dict:
    forum = "Машук"
    application = ScoredChunk(
        chunk_id="yonote_application",
        text="Заявку подают на платформе Росмолодёжь.События.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": forum,
            "topic": "podacha_zayavki_na_proekt",
            "intent_examples": ["Как подать заявку?"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    travel = ScoredChunk(
        chunk_id="yonote_travel",
        text="Проезд участник оплачивает самостоятельно.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": forum,
            "topic": "oplata_proezda",
            "intent_examples": ["Кто оплачивает проезд?"],
        },
        score=0.9,
        reranker_score=0.9,
    )
    return {
        "message_masked": "Как подать заявку на Машук и кто оплачивает проезд?",
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
                    text="Кто оплачивает проезд?",
                    category="форумы",
                    forum_normalized=forum,
                    topic="oplata_proezda",
                ),
            ],
        ),
        "reranked_chunks": [application, travel],
        "max_confidence": 0.95,
        "llm_client": llm_client,
    }


def _date_state(event: str, llm_client: object) -> dict:
    chunk = ScoredChunk(
        chunk_id="yonote_dates",
        text=(
            f"{event} пройдёт с 10 по 14 августа 2026 года. "
            + "Подтверждённый материал о датах. " * 20
        ),
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": event,
            "topic": "daty_provedeniya",
            "intent_examples": [f"Когда проходит {event}?"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    return {
        "message_masked": f"Когда проходит {event}?",
        "analysis": QueryAnalysis(
            complexity=Complexity.SIMPLE,
            category="форумы",
            forum_normalized=event,
            response_profile=ResponseProfileName.DATES,
            questions=[
                Question(
                    text=f"Когда проходит {event}?",
                    category="форумы",
                    forum_normalized=event,
                    topic="daty_provedeniya",
                )
            ],
        ),
        "reranked_chunks": [chunk],
        "max_confidence": 0.95,
        "llm_client": llm_client,
    }


def _published_seed_chunk(chunk_id: str, *, score: float) -> ScoredChunk:
    seed_path = Path(__file__).resolve().parents[1] / "data" / "knowledge_base_seed.json"
    records = json.loads(seed_path.read_text(encoding="utf-8"))
    record = next(item for item in records if item.get("chunk_id") == chunk_id)
    metadata = {
        key: value
        for key, value in record.items()
        if key not in {"text_raw", "text_clean"}
    }
    return ScoredChunk(
        chunk_id=chunk_id,
        text=record["text_clean"],
        metadata=metadata,
        score=score,
        reranker_score=score,
    )


@pytest.mark.asyncio
async def test_short_single_source_stays_extractive_within_simple_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = RaisingLLM()

    result = await generate(
        _state(
            _source("А" * SIMPLE_RESPONSE_MAX_CHARS),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 0
    assert result["generator_model"] == "source_chunk"
    assert len(result["generated_response"].split(" [src:", 1)[0]) == 450


@pytest.mark.asyncio
async def test_short_source_response_removes_unapproved_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = RaisingLLM()

    result = await generate(
        _state(
            _source("В программе будут лекции ✅"),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 0
    assert result["generated_response"] == (
        "В программе будут лекции [src:yonote_program]"
    )


@pytest.mark.asyncio
async def test_long_source_escalates_when_llm_fails_without_dumping_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    source_text = "Б" * (SIMPLE_RESPONSE_MAX_CHARS + 1)
    llm = RaisingLLM()

    result = await generate(
        _state(_source(source_text), llm, complexity=Complexity.SIMPLE)
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_generation_failed"
    assert result["generated_response"] == ""
    assert source_text not in result["generated_response"]


@pytest.mark.asyncio
async def test_legacy_only_source_cannot_produce_factual_answer() -> None:
    llm = RaisingLLM()
    legacy_source = _source("Старый ответ из таблицы.").model_copy(
        update={
            "chunk_id": "xlsx_legacy",
            "metadata": {
                "chunk_id": "xlsx_legacy",
                "source_type": "xlsx",
                "category": "форумы",
            },
        }
    )

    result = await generate(
        _state(legacy_source, llm, complexity=Complexity.SIMPLE)
    )

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_sources_for_generation"
    assert result["generated_response"] == ""
    assert result["cited_sources"] == []


@pytest.mark.asyncio
async def test_overlong_llm_response_is_rejected_without_substring_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        f"{'В' * (SIMPLE_RESPONSE_MAX_CHARS + 1)} [src:yonote_program]"
    )

    result = await generate(
        _state(
            _source("Источник " * 100),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_too_long"
    assert result["generated_response"] == ""
    assert "_rejected_candidate" not in result


@pytest.mark.asyncio
async def test_complex_llm_response_uses_900_character_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        f"{'Г' * (COMPLEX_RESPONSE_MAX_CHARS + 1)} [src:yonote_program]"
    )

    result = await generate(
        _state(
            _source("Источник " * 150),
            llm,
            complexity=Complexity.COMPLEX,
        )
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_too_long"
    assert result["generated_response"] == ""
    assert llm.requests[0]["max_tokens"] == 900
    assert "720 символов" in llm.requests[0]["user"]
    assert "585 символов" in llm.requests[1]["user"]


@pytest.mark.asyncio
async def test_invalid_first_synthesis_is_retried_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = SequenceLLM(
        [
            f"{'Д' * (SIMPLE_RESPONSE_MAX_CHARS + 1)} [src:yonote_program]",
            "В программе будут лекции. [src:yonote_program]",
        ]
    )

    result = await generate(
        _state(
            _source("Источник " * 100),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert "_rejected_candidate" not in result
    assert result["generated_response"] == (
        "В программе будут лекции. [src:yonote_program]"
    )
    assert "360 символов" in llm.requests[0]["user"]
    assert "292 символов" in llm.requests[1]["user"]
    assert "Отклонённый черновик" in llm.requests[1]["user"]
    assert "Д" * 80 in llm.requests[1]["user"]


@pytest.mark.asyncio
async def test_missing_citation_gets_corrective_retry_with_exact_allowed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = SequenceLLM(
        [
            "В программе будут лекции.",
            "В программе будут лекции. [SRC:YONOTE_PROGRAM]",
        ]
    )

    result = await generate(
        _state(
            _source("Источник " * 100),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert result["generated_response"] == (
        "В программе будут лекции. [src:yonote_program]"
    )
    assert result["cited_sources"] == ["yonote_program"]
    assert "ПОВТОРНАЯ ПОПЫТКА" not in llm.requests[0]["user"]
    assert "ПОВТОРНАЯ ПОПЫТКА" in llm.requests[1]["user"]
    assert "llm_source_citation_failed" in llm.requests[1]["user"]
    assert "[src:yonote_program]" in llm.requests[1]["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "В программе будут лекции.",
        "В программе будут лекции. [src:1]",
        "В программе будут лекции. [src:yonote_program] [src:unknown]",
    ],
)
async def test_invalid_or_unknown_citation_fails_at_generation_stage(
    response: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(response)

    result = await generate(
        _state(
            _source("Источник " * 100),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_source_citation_failed"
    assert result["generated_response"] == ""
    assert result["cited_sources"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["Машук", "Правда"])
async def test_nata_date_regression_requires_date_first(
    event: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = SequenceLLM(
        [
            f"{event} — тематическая программа форума. [src:yonote_dates]",
            f"С 10 по 14 августа 2026 года пройдёт {event}. [src:yonote_dates]",
        ]
    )

    result = await generate(_date_state(event, llm))

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert result["generated_response"].startswith("С 10 по 14 августа 2026 года")


@pytest.mark.asyncio
async def test_date_range_with_po_is_accepted_as_date_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = RaisingLLM()
    state = _date_state("Машук", llm)
    state["reranked_chunks"] = [
        state["reranked_chunks"][0].model_copy(
            update={"text": "С 10 по 14 августа 2026 года пройдёт Машук."}
        )
    ]

    result = await generate(state)

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["generated_response"].startswith("С 10 по 14 августа 2026 года")


@pytest.mark.asyncio
async def test_contextual_follow_up_requires_grounded_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        "С 10 по 14 августа 2026 года пройдёт Машук. [src:yonote_dates]"
    )
    state = _date_state("Машук", llm)
    state.update(
        {
            "message": "А когда он пройдёт?",
            "message_masked": "А когда он пройдёт?",
            "contextual_message": (
                "Пользователь: Расскажи про Машук.\n"
                "Пользователь: А когда он пройдёт?"
            ),
        }
    )

    result = await generate(state)

    assert llm.calls == 1
    assert result["generator_model"] != "source_chunk"
    assert result["cited_sources"] == ["yonote_dates"]


@pytest.mark.asyncio
async def test_date_answer_rejects_unasked_registration_curator_and_chat_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        "С 10 по 14 августа 2026 года пройдёт Машук. "
        "Также зарегистрируй заявку, напиши куратору и вступи в чат участников. "
        "[src:yonote_dates]"
    )

    result = await generate(_date_state("Машук", llm))

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_profile_failed"
    assert result["generated_response"] == ""


@pytest.mark.asyncio
async def test_real_seed_date_question_ignores_higher_scoring_transfer_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    transfer = _published_seed_chunk(
        "yonote_api_pmbmqm6lug_s0018_finansirovanie_uchastnikov",
        score=0.99,
    )
    first_shift_dates = _published_seed_chunk(
        "yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",
        score=0.8,
    )
    llm = RaisingLLM()

    result = await generate(
        {
            "message_masked": "Когда Машук?",
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.DATES,
                questions=[
                    Question(
                        text="Когда Машук?",
                        category="форумы",
                        forum_normalized="Машук",
                        topic="daty_nachala_meropriyatiya",
                    )
                ],
            ),
            "reranked_chunks": [transfer, first_shift_dates],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == [first_shift_dates.chunk_id]
    assert "августа" in result["generated_response"]
    assert "трансфер" not in result["generated_response"].casefold()


@pytest.mark.asyncio
async def test_date_question_with_only_transfer_source_does_not_answer_other_aspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    transfer = _published_seed_chunk(
        "yonote_api_pmbmqm6lug_s0018_finansirovanie_uchastnikov",
        score=0.99,
    )
    llm = RaisingLLM()

    result = await generate(
        {
            "message_masked": "Когда Машук?",
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.DATES,
                questions=[
                    Question(
                        text="Когда Машук?",
                        category="форумы",
                        forum_normalized="Машук",
                        topic="daty_nachala_meropriyatiya",
                    )
                ],
            ),
            "reranked_chunks": [transfer],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["generated_response"] == ""
    assert result["cited_sources"] == []


@pytest.mark.asyncio
async def test_non_date_profile_retries_then_rejects_cross_aspect_llm_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    application = ScoredChunk(
        chunk_id="yonote_application_long",
        text=(
            "Подать заявку на форум можно через карточку события в личном кабинете. "
            * 20
        ),
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "poryadok_registracii_na_forum",
            "intent_examples": ["Как подать заявку на Машук?"],
        },
        score=0.9,
        reranker_score=0.9,
    )
    llm = StubLLM(
        "Трансфер от вокзала до площадки организуют бесплатно. "
        "[src:yonote_application_long]"
    )

    result = await generate(
        {
            "message_masked": "Как подать заявку на Машук?",
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.APPLICATION,
                questions=[
                    Question(
                        text="Как подать заявку на Машук?",
                        category="форумы",
                        forum_normalized="Машук",
                        topic="podacha_zayavki_na_proekt",
                    )
                ],
            ),
            "reranked_chunks": [application],
            "max_confidence": 0.9,
            "llm_client": llm,
        }
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_profile_failed"
    assert result["generated_response"] == ""


@pytest.mark.asyncio
async def test_multi_source_answer_requires_grounded_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        "Заявку подают на платформе. [src:yonote_application]\n\n"
        "Проезд оплачивает участник. [src:yonote_travel]"
    )

    result = await generate(_multi_source_state(llm))

    assert llm.calls == 1
    assert result["generator_model"] != "source_chunk"
    assert result["cited_sources"] == ["yonote_application", "yonote_travel"]


@pytest.mark.asyncio
async def test_single_source_multi_aspect_requires_grounded_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    forum = "Машук"
    source = ScoredChunk(
        chunk_id="yonote_combined",
        text=(
            "Регистрация: заявку подают на платформе. "
            "Проезд участник оплачивает самостоятельно."
        ),
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": forum,
            "topic": "application_and_travel",
            "intent_examples": ["Как подать заявку?", "Кто оплачивает проезд?"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    questions = [
        Question(
            text="Как подать заявку?",
            category="форумы",
            forum_normalized=forum,
            topic="podacha_zayavki_na_proekt",
        ),
        Question(
            text="Кто оплачивает проезд?",
            category="форумы",
            forum_normalized=forum,
            topic="oplata_proezda",
        ),
    ]
    llm = StubLLM(
        "Регистрация: заявку подают на платформе. [src:yonote_combined]\n\n"
        "Проезд участник оплачивает самостоятельно. [src:yonote_combined]"
    )

    result = await generate(
        {
            "message_masked": (
                "Как подать заявку на Машук и кто оплачивает проезд?"
            ),
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized=forum,
                questions=questions,
            ),
            "reranked_chunks": [source],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert llm.calls == 1
    assert result["generator_model"] != "source_chunk"
    assert result["cited_sources"] == ["yonote_combined"]


@pytest.mark.asyncio
async def test_multi_source_answer_retries_swapped_nonnumeric_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = SequenceLLM(
        [
            (
                "Заявку подают на платформе. [src:yonote_travel]\n\n"
                "Проезд оплачивает участник. [src:yonote_application]"
            ),
            (
                "Заявку подают на платформе. [src:yonote_application]\n\n"
                "Проезд оплачивает участник. [src:yonote_travel]"
            ),
        ]
    )

    result = await generate(_multi_source_state(llm))

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_application", "yonote_travel"]
    assert llm.requests[1]["user"].count("llm_source_fact_binding_failed") == 1
    assert "Отклонённый черновик" in llm.requests[1]["user"]
    assert "ровно одним соответствующим source-маркером" in llm.requests[1]["user"]


@pytest.mark.asyncio
async def test_conditional_numeric_claims_retry_when_age_date_bindings_are_swapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    forum = "Машук"
    minors = ScoredChunk(
        chunk_id="yonote_age_14_17",
        text="Участники 14–17 лет\n08.08.2026 — 22.08.2026",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": forum,
            "topic": "dates_by_age",
            "intent_examples": ["Когда смена для участников 14–17 лет?"],
            "has_conditional_logic": True,
        },
        score=0.95,
        reranker_score=0.95,
    )
    adults = ScoredChunk(
        chunk_id="yonote_age_18_35",
        text="Участники 18–35 лет\n08.08.2026 — 15.08.2026",
        metadata={
            **minors.metadata,
            "topic": "dates_age_18_35",
            "intent_examples": ["Когда смена для участников 18–35 лет?"],
            "has_conditional_logic": True,
        },
        score=0.94,
        reranker_score=0.94,
    )
    llm = SequenceLLM(
        [
            (
                "Для участников 14–17 лет смена пройдёт 08.08.2026–15.08.2026 "
                "[src:yonote_age_14_17]\n"
                "Для участников 18–35 лет — 08.08.2026–22.08.2026 "
                "[src:yonote_age_18_35]"
            ),
            (
                "Для участников 14–17 лет смена пройдёт 08.08.2026–22.08.2026 "
                "[src:yonote_age_14_17]\n"
                "Для участников 18–35 лет — 08.08.2026–15.08.2026 "
                "[src:yonote_age_18_35]"
            ),
        ]
    )
    state = {
        "message_masked": "Когда проходит первая смена Машука для двух возрастных групп?",
        "analysis": QueryAnalysis(
            complexity=Complexity.COMPLEX,
            category="форумы",
            forum_normalized=forum,
            response_profile=ResponseProfileName.DATES,
            questions=[
                Question(
                    text="Когда смена для участников 14–17 лет?",
                    category="форумы",
                    forum_normalized=forum,
                    topic="dates_by_age",
                ),
                Question(
                    text="Когда смена для участников 18–35 лет?",
                    category="форумы",
                    forum_normalized=forum,
                    topic="dates_age_18_35",
                ),
            ],
        ),
        "reranked_chunks": [minors, adults],
        "max_confidence": 0.95,
        "llm_client": llm,
    }

    result = await generate(state)

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_age_14_17", "yonote_age_18_35"]
    assert "22.08.2026 [src:yonote_age_14_17]" in result["generated_response"]
    assert "15.08.2026 [src:yonote_age_18_35]" in result["generated_response"]


def test_conditional_parent_chunk_rejects_swapped_rows_and_date_permutation() -> None:
    parent = ScoredChunk(
        chunk_id="yonote_age_parent",
        text=(
            "Участники 14–17 лет: 08.08.2026–22.08.2026.\n"
            "Участники 18–35 лет: 08.08.2026–15.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        score=0.95,
        reranker_score=0.95,
    )
    date_source = parent.model_copy(
        update={
            "chunk_id": "yonote_single_date",
            "text": "Форум пройдёт 08.09.2026.",
            "metadata": {
                "source_type": "yonote",
                "has_conditional_logic": False,
            },
        }
    )

    assert not _llm_claims_have_bound_source_facts(
        (
            "Для участников 14–17 лет: 08.08.2026–15.08.2026 "
            "[src:yonote_age_parent]"
        ),
        [parent],
    )
    assert _llm_claims_have_bound_source_facts(
        (
            "Для участников 14–17 лет: 08.08.2026–22.08.2026 "
            "[src:yonote_age_parent]"
        ),
        [parent],
    )
    assert not _llm_claims_have_bound_source_facts(
        "Форум пройдёт 09.08.2026 [src:yonote_single_date]",
        [date_source],
    )
    shifts = parent.model_copy(
        update={
            "chunk_id": "yonote_shift_parent",
            "text": (
                "Первая смена: 08.08.2026–15.08.2026.\n"
                "Вторая смена: 15.08.2026–22.08.2026."
            ),
        }
    )
    assert not _llm_claims_have_bound_source_facts(
        "Первая смена: 15.08.2026–22.08.2026 [src:yonote_shift_parent]",
        [shifts],
    )
    later_shifts = parent.model_copy(
        update={
            "chunk_id": "yonote_later_shift_parent",
            "text": (
                "Первая смена: 17.06.2026–21.06.2026.\n"
                "Пятая смена: 16.06.2026–20.06.2026."
            ),
        }
    )
    assert not _llm_claims_have_bound_source_facts(
        "Пятая смена: 17.06.2026–21.06.2026 "
        "[src:yonote_later_shift_parent]",
        [later_shifts],
    )
    roles = parent.model_copy(
        update={
            "chunk_id": "yonote_role_parent",
            "text": (
                "Для очников: 08.08.2026.\n"
                "Для заочников: 15.08.2026."
            ),
        }
    )
    assert not _llm_claims_have_bound_source_facts(
        "Для очников: 15.08.2026 [src:yonote_role_parent]",
        [roles],
    )


@pytest.mark.parametrize(
    ("query", "minor_matches", "adult_matches"),
    [
        ("Мне 20 лет, когда проходит смена?", False, True),
        ("Мне 18, когда проходит смена?", False, True),
        ("Я несовершеннолетний, когда проходит смена?", True, False),
        ("Я совершеннолетняя, когда проходит смена?", False, True),
    ],
)
def test_explicit_age_constraint_matches_only_compatible_source_range(
    query: str,
    minor_matches: bool,
    adult_matches: bool,
) -> None:
    minor = ScoredChunk(
        chunk_id="yonote_minor_dates",
        text="Участники 14–17 лет: 08.08.2026–22.08.2026.",
        metadata={"source_type": "yonote"},
        score=0.99,
        reranker_score=0.99,
    )
    adult = minor.model_copy(
        update={
            "chunk_id": "yonote_adult_dates",
            "text": "Участники в возрасте от 18 до 35 лет: 08.08.2026–15.08.2026.",
        }
    )
    question = Question(text=query)

    assert _source_matches_explicit_question_constraints(question, minor) is minor_matches
    assert _source_matches_explicit_question_constraints(question, adult) is adult_matches


def test_age_alias_and_subset_match_broader_source_range() -> None:
    broad = ScoredChunk(
        chunk_id="yonote_age_14_35",
        text="Участники в возрасте от 14 до 35 лет.",
        metadata={"source_type": "yonote"},
        score=0.95,
        reranker_score=0.95,
    )

    assert _source_matches_explicit_question_constraints(
        Question(text="Я несовершеннолетний, могу участвовать?"),
        broad,
    )
    assert _source_matches_explicit_question_constraints(
        Question(text="Я совершеннолетний, могу участвовать?"),
        broad,
    )
    assert _source_matches_explicit_question_constraints(
        Question(text="Могут участвовать ребята 16–17 лет?"),
        broad,
    )


@pytest.mark.parametrize(
    ("query", "expected_key"),
    [
        ("Назови период первой смены.", "shift:1"),
        ("Можно приехать на первой смене?", "shift:1"),
        ("Расскажи про первую смену.", "shift:1"),
        ("Когда смена №1?", "shift:1"),
        ("Когда смена #1?", "shift:1"),
        ("Назови период второй смены.", "shift:2"),
        ("Можно приехать на второй смене?", "shift:2"),
        ("Расскажи про вторую смену.", "shift:2"),
        ("Когда смена №2?", "shift:2"),
        ("Когда смена #2?", "shift:2"),
        ("Назови период пятой смены.", "shift:5"),
        ("Когда смена №7?", "shift:7"),
        ("Когда будет 9-я смена?", "shift:9"),
    ],
)
def test_shift_condition_keys_cover_inflections_and_number_forms(
    query: str,
    expected_key: str,
) -> None:
    assert expected_key in _condition_keys(query)


@pytest.mark.asyncio
async def test_full_generate_ignores_higher_scored_wrong_single_age_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    query = "Мне 20 лет, когда проходит смена форума «Машук»?"
    common_metadata = {
        "source_type": "yonote",
        "category": "форумы",
        "forum_normalized": "Машук",
        "topic": "dates_by_age",
        "intent_examples": [query],
    }
    wrong_minor = ScoredChunk(
        chunk_id="yonote_minor_dates",
        text=(
            "Участники 14–17 лет: 08.08.2026–22.08.2026. "
            + "Описание условий для несовершеннолетних участников. " * 12
        ),
        metadata=common_metadata,
        score=0.99,
        reranker_score=0.99,
    )
    correct_adult = ScoredChunk(
        chunk_id="yonote_adult_dates",
        text=(
            "Участники 18–35 лет: 08.08.2026–15.08.2026. "
            + "Описание условий для совершеннолетних участников. " * 12
        ),
        metadata=common_metadata,
        score=0.90,
        reranker_score=0.90,
    )
    llm = StubLLM(
        "Для участников 18–35 лет смена пройдёт "
        "08.08.2026–15.08.2026 [src:yonote_adult_dates]"
    )
    question = Question(
        text=query,
        category="форумы",
        forum_normalized="Машук",
        topic="dates_by_age",
    )

    result = await generate(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.DATES,
                questions=[question],
            ),
            "reranked_chunks": [wrong_minor, correct_adult],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_adult_dates"]
    assert "18–35 лет" in result["generated_response"]


@pytest.mark.asyncio
async def test_full_generate_ignores_higher_scored_wrong_shift_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    query = "Назови период проведения первой смены форума «Машук»."
    common_metadata = {
        "source_type": "yonote",
        "category": "форумы",
        "forum_normalized": "Машук",
        "topic": "dates_by_shift",
        "intent_examples": [query],
    }
    wrong_second = ScoredChunk(
        chunk_id="yonote_second_shift",
        text=(
            "Вторая смена: 15.08.2026–22.08.2026. "
            + "Подтверждённое описание второй смены. " * 15
        ),
        metadata=common_metadata,
        score=0.99,
        reranker_score=0.99,
    )
    correct_first = ScoredChunk(
        chunk_id="yonote_first_shift",
        text=(
            "Первая смена: 08.08.2026–15.08.2026. "
            + "Подтверждённое описание первой смены. " * 15
        ),
        metadata=common_metadata,
        score=0.90,
        reranker_score=0.90,
    )
    llm = StubLLM(
        "Первая смена пройдёт 08.08.2026–15.08.2026 "
        "[src:yonote_first_shift]"
    )
    question = Question(
        text=query,
        category="форумы",
        forum_normalized="Машук",
        topic="dates_by_shift",
    )

    result = await generate(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.DATES,
                questions=[question],
            ),
            "reranked_chunks": [wrong_second, correct_first],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_first_shift"]
    assert "08.08.2026–15.08.2026" in result["generated_response"]


@pytest.mark.asyncio
async def test_full_generate_ignores_higher_scored_wrong_fifth_shift_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    query = "Назови даты пятой смены форума ТИМ «Бирюса»."
    common_metadata = {
        "source_type": "yonote",
        "category": "форумы",
        "forum_normalized": "ТИМ «Бирюса»",
        "topic": "dates_by_shift",
        "intent_examples": [query],
    }
    wrong_first = ScoredChunk(
        chunk_id="yonote_first_shift",
        text="Первая смена: 17.06.2026–21.06.2026.",
        metadata=common_metadata,
        score=0.99,
        reranker_score=0.99,
    )
    correct_fifth = ScoredChunk(
        chunk_id="yonote_fifth_shift",
        text="Пятая смена: 16.06.2026–20.06.2026.",
        metadata=common_metadata,
        score=0.90,
        reranker_score=0.90,
    )
    llm = StubLLM(
        "Пятая смена пройдёт 16.06.2026–20.06.2026 "
        "[src:yonote_fifth_shift]"
    )

    result = await generate(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="ТИМ «Бирюса»",
                response_profile=ResponseProfileName.DATES,
                questions=[
                    Question(
                        text=query,
                        category="форумы",
                        forum_normalized="ТИМ «Бирюса»",
                        topic="dates_by_shift",
                    )
                ],
            ),
            "reranked_chunks": [wrong_first, correct_fifth],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_fifth_shift"]
    assert "16.06.2026–20.06.2026" in result["generated_response"]


def test_effective_questions_do_not_add_phantom_date_question() -> None:
    analysis = QueryAnalysis(
        category="форумы",
        forum_normalized="Машук",
        response_profile=ResponseProfileName.DATES,
        questions=[
            Question(
                text="Когда смена для участников 14–17 лет?",
                category="форумы",
                forum_normalized="Машук",
                topic="dates_age_14_17",
            ),
            Question(
                text="Когда смена для участников 18–35 лет?",
                category="форумы",
                forum_normalized="Машук",
                topic="dates_age_18_35",
            ),
        ],
    )

    questions = build_effective_questions(
        analysis,
        "Когда проходит смена Машука для участников 14–17 и 18–35 лет?",
    )

    assert [question.text for question in questions] == [
        "Когда смена для участников 14–17 лет?",
        "Когда смена для участников 18–35 лет?",
    ]


@pytest.mark.asyncio
async def test_full_generate_retries_swapped_conditions_from_one_parent_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    parent = ScoredChunk(
        chunk_id="yonote_age_parent",
        text=(
            "Участники 14–17 лет: 08.08.2026–22.08.2026.\n"
            "Участники 18–35 лет: 08.08.2026–15.08.2026.\n"
            + "Подтверждённое описание смены. " * 20
        ),
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "dates_by_age",
            "intent_examples": ["Когда проходит смена Машука по возрастам?"],
            "has_conditional_logic": True,
        },
        score=0.95,
        reranker_score=0.95,
    )
    llm = SequenceLLM(
        [
            (
                "Для участников 14–17 лет: 08.08.2026–15.08.2026 "
                "[src:yonote_age_parent]\n"
                "Для участников 18–35 лет: 08.08.2026–22.08.2026 "
                "[src:yonote_age_parent]"
            ),
            (
                "Для участников 14–17 лет: 08.08.2026–22.08.2026 "
                "[src:yonote_age_parent]\n"
                "Для участников 18–35 лет: 08.08.2026–15.08.2026 "
                "[src:yonote_age_parent]"
            ),
        ]
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Машук",
        response_profile=ResponseProfileName.DATES,
        questions=[
            Question(
                text="Когда проходит смена Машука по возрастам?",
                category="форумы",
                forum_normalized="Машук",
                topic="dates_by_age",
            )
        ],
    )

    result = await generate(
        {
            "message_masked": "Когда проходит смена Машука по возрастам?",
            "analysis": analysis,
            "reranked_chunks": [parent],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert "14–17 лет: 08.08.2026–22.08.2026" in result["generated_response"]


@pytest.mark.asyncio
async def test_technical_profile_rejects_pure_application_process_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    source = ScoredChunk(
        chunk_id="yonote_technical",
        text=(
            "Если поле проекта не отображается, очисти кеш и cookie, "
            "затем открой форму в другом браузере. " * 12
        ),
        metadata={
            "source_type": "yonote",
            "category": "техподдержка",
            "topic": "technical",
            "intent_examples": ["Поле проекта не отображается"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    llm = StubLLM(
        "Регистрация проходит через личный кабинет. [src:yonote_technical]"
    )
    state = {
        "message_masked": "Поле проекта не отображается. Что делать?",
        "analysis": QueryAnalysis(
            complexity=Complexity.SIMPLE,
            category="техподдержка",
            response_profile=ResponseProfileName.TECHNICAL,
            questions=[
                Question(
                    text="Поле проекта не отображается. Что делать?",
                    category="техподдержка",
                    topic="technical",
                )
            ],
        ),
        "reranked_chunks": [source],
        "max_confidence": 0.95,
        "llm_client": llm,
    }

    result = await generate(state)

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_profile_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_answer",
    [
        "Заявку нужно подать через личный кабинет. [src:yonote_technical]",
        (
            "Регистрация проходит через кабинет. Если возникнет ошибка, "
            "повтори попытку. [src:yonote_technical]"
        ),
        (
            "Заявку подай через кабинет и нажми кнопку «Подать заявку». "
            "[src:yonote_technical]"
        ),
    ],
)
async def test_technical_profile_rejects_lexical_business_answer_bypasses(
    monkeypatch: pytest.MonkeyPatch,
    wrong_answer: str,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    source = ScoredChunk(
        chunk_id="yonote_technical",
        text=(
            "Если форма не работает, очисти кеш и cookie и открой её "
            "в другом браузере. " * 12
        ),
        metadata={
            "source_type": "yonote",
            "category": "техподдержка",
            "topic": "technical",
            "intent_examples": ["Форма не работает"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    llm = StubLLM(wrong_answer)
    analysis = QueryAnalysis(
        complexity=Complexity.SIMPLE,
        category="техподдержка",
        response_profile=ResponseProfileName.TECHNICAL,
        questions=[
            Question(
                text="Форма не работает. Что делать?",
                category="техподдержка",
                topic="technical",
            )
        ],
    )

    result = await generate(
        {
            "message_masked": "Форма не работает. Что делать?",
            "analysis": analysis,
            "reranked_chunks": [source],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_profile_failed"


@pytest.mark.asyncio
async def test_multi_question_answer_rejects_unrequested_only_aspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        "Питание предоставляют три раза в день. "
        "[src:yonote_application] [src:yonote_travel]"
    )

    result = await generate(_multi_source_state(llm))

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_profile_failed"
    assert result["generated_response"] == ""


@pytest.mark.asyncio
async def test_dynamic_llm_response_removes_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM("В программе будут лекции и мастер-классы ✅ [src:yonote_program]")

    result = await generate(
        _state(
            _source("Источник " * 100),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert result.get("should_escalate") is not True
    assert result["generated_response"] == (
        "В программе будут лекции и мастер-классы [src:yonote_program]"
    )
    assert "✅" not in result["generated_response"]


@pytest.mark.asyncio
async def test_dynamic_llm_response_rejects_multiple_user_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        "Подробности: https://example.ru/one и https://example.ru/two "
        "[src:yonote_program]"
    )

    result = await generate(
        _state(
            _source("Источник " * 100),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "llm_response_contract_failed"
    assert result["generated_response"] == ""


@pytest.mark.asyncio
async def test_bare_domains_in_source_trigger_single_link_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    llm = StubLLM(
        "Программа опубликована на https://example.ru/program "
        "[src:yonote_program]"
    )

    result = await generate(
        _state(
            _source("Материалы: example.ru и events.myrosmol.ru."),
            llm,
            complexity=Complexity.SIMPLE,
        )
    )

    assert llm.calls == 1
    assert result.get("should_escalate") is not True
    assert result["generator_model"] != "source_chunk"
    assert result["generated_response"].count("https://") == 1


def test_generator_prompt_locks_structure_and_response_budget() -> None:
    prompt = build_generator_user(
        questions=[Question(text="Когда проходит Машук?")],
        chunks=[
            Chunk(
                chunk_id="yonote_dates",
                text="Даты указаны в опубликованном документе.",
                metadata={"source_type": "yonote"},
            )
        ],
        session=None,
        max_chars=SIMPLE_RESPONSE_MAX_CHARS,
        response_profile="dates",
        profile_guidance="Сначала дай подтверждённую дату.",
    )

    assert "Максимальная длина ответа" in prompt
    assert "450 символов" in prompt
    assert "не обрезай текст" in prompt
    assert "Профиль ответа: dates" in prompt
    assert "Сначала дай подтверждённую дату." in prompt
    assert "Начинай с прямого ответа" in RESPONSE_GENERATOR_SYSTEM
    assert "не более одной" in RESPONSE_GENERATOR_SYSTEM
    assert "эмодзи" in RESPONSE_GENERATOR_SYSTEM
    assert "ровно один source-маркер" in RESPONSE_GENERATOR_SYSTEM
    assert "не ставь несколько source-маркеров" in prompt
    assert "не создавай такой пункт" in prompt
    assert "что в источниках нет достаточных данных" not in prompt


def test_generator_retry_prompt_bounds_rejected_draft() -> None:
    prompt = build_generator_user(
        questions=[Question(text="Когда проходит Машук?")],
        chunks=[
            Chunk(
                chunk_id="yonote_dates",
                text="Машук пройдёт в августе.",
                metadata={"source_type": "yonote"},
            )
        ],
        session=None,
        max_chars=SIMPLE_RESPONSE_MAX_CHARS,
        retry_reason="llm_response_too_long",
        rejected_draft="Я" * 1500,
    )

    assert "Я" * 1200 + "…" in prompt
    assert "Я" * 1201 not in prompt


def test_specific_profile_needs_fact_anchor_not_only_two_shared_tokens() -> None:
    question = Question(
        text="Какая программа форума Машук?",
        category="форумы",
        forum_normalized="Машук",
        topic="programma_foruma",
    )
    sibling = ScoredChunk(
        chunk_id="yonote_mashuk_results",
        text="Программа форума Машук объединяет участников, прошедших отбор.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "rezultaty_otbora",
        },
        score=0.9,
        reranker_score=0.9,
    )
    fact = sibling.model_copy(
        update={
            "chunk_id": "yonote_mashuk_program",
            "metadata": {
                **sibling.metadata,
                "topic": "programma_foruma",
            },
        }
    )

    assert _source_chunk_covers_question(question, sibling) is False
    assert _source_chunk_covers_question(question, fact) is True


def test_conditional_fact_chunk_is_not_dropped_as_redundant() -> None:
    existing = ScoredChunk(
        chunk_id="yonote_shift_summary",
        text=(
            "Первая смена Машука проходит с 8 по 15 августа "
            "для совершеннолетних участников."
        ),
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "pervaya_smena",
            "has_conditional_logic": False,
        },
        score=0.9,
        reranker_score=0.9,
    )
    conditional = existing.model_copy(
        update={
            "chunk_id": "yonote_shift_by_age",
            "metadata": {
                **existing.metadata,
                "has_conditional_logic": True,
                "conditions_summary": [
                    "14–17 лет: 8–22 августа",
                    "18–35 лет: 8–15 августа",
                ],
            },
        }
    )
    duplicate = existing.model_copy(update={"chunk_id": "yonote_shift_duplicate"})

    assert _is_redundant_source_chunk(duplicate, [existing]) is True
    assert _is_redundant_source_chunk(conditional, [existing]) is False


def test_citation_binding_allows_natural_punctuation_after_marker() -> None:
    source = ScoredChunk(
        chunk_id="yonote_fact",
        text="Подтверждённый факт.",
        metadata={"source_type": "yonote"},
        reranker_score=0.95,
    )

    assert _llm_claims_have_bound_source_facts(
        "Подтверждённый факт [src:yonote_fact].",
        [source],
    )


def test_conditional_binding_understands_natural_age_aliases() -> None:
    source = ScoredChunk(
        chunk_id="yonote_age_parent",
        text=(
            "Участники 14–17 лет: 08.08.2026–22.08.2026.\n"
            "Участники 18–35 лет: 08.08.2026–15.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Подросткам смена пройдёт 08.08.2026–15.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )
    assert not _llm_claims_have_bound_source_facts(
        "Взрослым смена пройдёт 08.08.2026–22.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )
    assert _llm_claims_have_bound_source_facts(
        "Подросткам смена пройдёт 08.08.2026–22.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )
    assert not _llm_claims_have_bound_source_facts(
        "Смена пройдёт 08.08.2026–15.08.2026 [src:yonote_age_parent]",
        [source],
        [Question(text="Мне 16 лет, когда проходит смена?")],
    )


def test_explicit_age_binding_rejects_wrong_adult_subrange() -> None:
    source = ScoredChunk(
        chunk_id="yonote_age_parent",
        text=(
            "Участники 18–25 лет: дата смены 08.08.2026.\n"
            "Участники 26–35 лет: дата смены 09.08.2026."
        ),
        metadata={"source_type": "yonote", "has_conditional_logic": True},
        reranker_score=0.95,
    )
    question = [Question(text="Мне 20 лет, когда проходит смена?")]

    assert not _llm_claims_have_bound_source_facts(
        "Смена проходит 09.08.2026 [src:yonote_age_parent]",
        [source],
        question,
    )
    assert _llm_claims_have_bound_source_facts(
        "Смена проходит 08.08.2026 [src:yonote_age_parent]",
        [source],
        question,
    )


@pytest.mark.asyncio
async def test_full_generate_retries_wrong_date_for_explicit_age_subrange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    source = ScoredChunk(
        chunk_id="yonote_age_parent",
        text=(
            "Участники 18–25 лет: дата смены 08.08.2026.\n"
            "Участники 26–35 лет: дата смены 09.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "has_conditional_logic": True,
        },
        score=0.95,
        reranker_score=0.95,
    )
    llm = SequenceLLM(
        [
            "Смена проходит 09.08.2026 [src:yonote_age_parent]",
            "Смена проходит 08.08.2026 [src:yonote_age_parent]",
        ]
    )

    result = await generate(
        {
            "message_masked": "Мне 20 лет, когда проходит смена форума Машук?",
            "analysis": QueryAnalysis(
                complexity=Complexity.COMPLEX,
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.DATES,
                questions=[
                    Question(
                        text="Мне 20 лет, когда проходит смена?",
                        category="форумы",
                        forum_normalized="Машук",
                    )
                ],
            ),
            "reranked_chunks": [source],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert llm.calls == 2
    assert result.get("should_escalate") is not True
    assert "08.08.2026" in result["generated_response"]


def test_single_source_binding_rejects_contradicted_travel_payer() -> None:
    source = ScoredChunk(
        chunk_id="yonote_travel",
        text="Проезд участник оплачивает самостоятельно.",
        metadata={"source_type": "yonote"},
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Проезд полностью оплачивает организатор [src:yonote_travel]",
        [source],
    )
    assert _llm_claims_have_bound_source_facts(
        "Проезд участник оплачивает самостоятельно [src:yonote_travel]",
        [source],
    )


def test_single_source_binding_rejects_contradicted_accommodation_type() -> None:
    source = ScoredChunk(
        chunk_id="yonote_accommodation",
        text="Участников размещают в гостинице.",
        metadata={"source_type": "yonote"},
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Участники будут жить в палатках [src:yonote_accommodation]",
        [source],
    )
    assert _llm_claims_have_bound_source_facts(
        "Участников разместят в отеле [src:yonote_accommodation]",
        [source],
    )


@pytest.mark.asyncio
async def test_fact_card_contract_rejects_reproducible_source_contradiction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ScoredChunk(
        chunk_id="yonote_accommodation",
        text="Участников размещают в гостинице.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "usloviya_prozhivaniya",
            "intent_examples": ["Где живут участники Машука?"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    corrupt_draft = SimpleNamespace(
        response=(
            "Участников размещают в палатках. [src:yonote_accommodation]"
        ),
        cited_sources=("yonote_accommodation",),
    )
    monkeypatch.setattr(
        "src.graph.nodes.generate.compose_fact_cards",
        lambda *_args, **_kwargs: corrupt_draft,
    )
    llm = RaisingLLM()

    result = await generate(
        {
            "message_masked": "Где живут участники Машука?",
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="форумы",
                forum_normalized="Машук",
                questions=[
                    Question(
                        text="Где живут участники Машука?",
                        category="форумы",
                        forum_normalized="Машук",
                        topic="usloviya_prozhivaniya",
                    )
                ],
            ),
            "reranked_chunks": [source],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "source_response_contract_failed"
    assert result["generated_response"] == ""


def test_single_source_binding_rejects_reversed_permission() -> None:
    source = ScoredChunk(
        chunk_id="yonote_rules",
        text="На площадку нельзя приносить животных.",
        metadata={"source_type": "yonote"},
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "На площадку можно приносить животных [src:yonote_rules]",
        [source],
    )


def test_conditional_binding_rejects_swapped_nonnumeric_roles() -> None:
    source = ScoredChunk(
        chunk_id="yonote_role_parent",
        text=(
            "Для наставников: проезд оплачивает организатор.\n"
            "Для школьников: проезд участник оплачивает самостоятельно."
        ),
        metadata={"source_type": "yonote", "has_conditional_logic": True},
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Для наставников проезд участник оплачивает самостоятельно "
        "[src:yonote_role_parent]",
        [source],
    )


def test_age_alias_after_for_is_only_an_age_condition() -> None:
    source = ScoredChunk(
        chunk_id="yonote_age_parent",
        text=(
            "Участники 14–17 лет: 08.08.2026–22.08.2026.\n"
            "Участники 18–35 лет: 08.08.2026–15.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        reranker_score=0.95,
    )

    assert _condition_keys("Когда смена для подростков?") == {"age_group:minor"}
    assert _condition_keys("Когда смена для взрослых?") == {"age_group:adult"}
    assert _llm_claims_have_bound_source_facts(
        "Для подростков смена пройдёт 08.08.2026–22.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )
    assert _llm_claims_have_bound_source_facts(
        "Для взрослых смена пройдёт 08.08.2026–15.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )
    assert not _llm_claims_have_bound_source_facts(
        "Для подростков смена пройдёт 08.08.2026–15.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )
    assert not _llm_claims_have_bound_source_facts(
        "Для взрослых смена пройдёт 08.08.2026–22.08.2026 "
        "[src:yonote_age_parent]",
        [source],
    )


def test_conditional_binding_inherits_shift_when_claim_omits_label() -> None:
    source = ScoredChunk(
        chunk_id="yonote_shift_parent",
        text=(
            "Первая смена: 08.08.2026–15.08.2026.\n"
            "Вторая смена: 15.08.2026–22.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Смена пройдёт 15.08.2026–22.08.2026 [src:yonote_shift_parent]",
        [source],
        [Question(text="Когда проходит первая смена?")],
    )
    assert _llm_claims_have_bound_source_facts(
        "Смена пройдёт 08.08.2026–15.08.2026 [src:yonote_shift_parent]",
        [source],
        [Question(text="Когда проходит первая смена?")],
    )


def test_claim_shift_overrides_question_shift_for_fact_binding() -> None:
    source = ScoredChunk(
        chunk_id="yonote_shift_parent",
        text=(
            "Первая смена: 08.08.2026–15.08.2026.\n"
            "Вторая смена: 15.08.2026–22.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Вторая смена пройдёт 08.08.2026–15.08.2026 "
        "[src:yonote_shift_parent]",
        [source],
        [Question(text="Когда проходит первая смена?")],
    )


def test_claim_audience_overrides_question_audience_for_fact_binding() -> None:
    source = ScoredChunk(
        chunk_id="yonote_role_parent",
        text=(
            "Для наставников: 08.08.2026.\n"
            "Для школьников: 15.08.2026."
        ),
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Для школьников дата 08.08.2026 [src:yonote_role_parent]",
        [source],
        [Question(text="Когда программа для наставников?")],
    )
    assert _llm_claims_have_bound_source_facts(
        "Дата 08.08.2026 [src:yonote_role_parent]",
        [source],
        [Question(text="Когда программа для наставников?")],
    )


def test_conditional_binding_splits_comma_rows_and_normalizes_roles() -> None:
    source = ScoredChunk(
        chunk_id="yonote_role_parent",
        text="Для очников — 08.08.2026, для заочников — 15.08.2026.",
        metadata={
            "source_type": "yonote",
            "has_conditional_logic": True,
        },
        reranker_score=0.95,
    )

    assert not _llm_claims_have_bound_source_facts(
        "Очным участникам дата 15.08.2026 [src:yonote_role_parent]",
        [source],
    )
    assert not _llm_claims_have_bound_source_facts(
        "Заочным участникам дата 08.08.2026 [src:yonote_role_parent]",
        [source],
    )
    assert _llm_claims_have_bound_source_facts(
        "Очным участникам дата 08.08.2026 [src:yonote_role_parent]",
        [source],
    )


def test_date_binding_accepts_numeric_rendering_of_word_month_range() -> None:
    source = ScoredChunk(
        chunk_id="yonote_mashuk_first_shift",
        text="1 смена: 8–15 августа.",
        metadata={
            "source_type": "yonote",
            "source_heading_path": ["Форум Машук 2026", "Первая смена"],
        },
        reranker_score=0.95,
    )

    assert _llm_claims_have_bound_source_facts(
        "Первая смена пройдёт с 08.08.2026 по 15.08.2026 "
        "[src:yonote_mashuk_first_shift]",
        [source],
    )


def test_generic_shift_question_does_not_bind_adjacent_named_shift_date() -> None:
    question = Question(
        text="Когда смена на форуме Машук?",
        category="форумы",
        forum_normalized="Машук",
    )
    anchor = ScoredChunk(
        chunk_id="yonote_truth_anchor",
        text="На смене «Правда» участники работают с медиапроектами.",
        metadata={
            "source_type": "yonote",
            "forum_normalized": "Машук",
            "source_document_id": "mashuk-doc",
            "source_row": 10,
        },
        reranker_score=0.99,
    )
    child = ScoredChunk(
        chunk_id="yonote_truth_date",
        text="Даты смены: 26–30 июля 2026 года.",
        metadata={
            "source_type": "yonote",
            "forum_normalized": "Машук",
            "source_document_id": "mashuk-doc",
            "source_row": 11,
            "parent_chunk_id": anchor.chunk_id,
        },
        reranker_score=0.99,
    )

    assert _linked_named_section_date_source(question, [anchor, child]) is None


@pytest.mark.asyncio
async def test_full_generate_prefers_overall_date_for_generic_shift_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    overall = ScoredChunk(
        chunk_id="yonote_mashuk_overall_date",
        text="Общий период проведения форума: 1–10 августа 2026 года.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "topic": "daty_nachala_meropriyatiya",
        },
        score=0.95,
        reranker_score=0.95,
    )
    named_anchor = ScoredChunk(
        chunk_id="yonote_truth_anchor",
        text="На смене «Правда» участники работают с медиапроектами.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "source_document_id": "mashuk-doc",
            "source_row": 10,
        },
        score=0.99,
        reranker_score=0.99,
    )
    named_date = ScoredChunk(
        chunk_id="yonote_truth_date",
        text="Даты смены: 26–30 июля 2026 года.",
        metadata={
            "source_type": "yonote",
            "category": "форумы",
            "forum_normalized": "Машук",
            "source_document_id": "mashuk-doc",
            "source_row": 11,
            "parent_chunk_id": named_anchor.chunk_id,
        },
        score=0.99,
        reranker_score=0.99,
    )
    llm = RaisingLLM()

    state = {
            "message_masked": "Когда смена на форуме Машук?",
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Машук",
                response_profile=ResponseProfileName.DATES,
                questions=[
                    Question(
                        text="Когда смена на форуме Машук?",
                        topic="daty_nachala_meropriyatiya",
                        category="форумы",
                        forum_normalized="Машук",
                    )
                ],
            ),
            "reranked_chunks": [named_anchor, named_date, overall],
            "max_confidence": 0.99,
            "llm_client": llm,
    }

    result = await generate(state)
    verification = await verify({**state, **result})

    assert llm.calls == 0
    assert result["cited_sources"] == [overall.chunk_id]
    assert "1–10 августа" in result["generated_response"]
    assert verification["verification"].has_hallucination is False
    assert verification["verification"].triggered_llm_judge is False


@pytest.mark.asyncio
async def test_fallback_pipeline_preserves_age_constraint_for_date_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    query = "Мне 20 лет, когда проходит первая смена форума Машук?"
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)
    assert analysis is not None
    questions = build_effective_questions(analysis, query)
    assert len(questions) == 1
    assert "возраст 20 лет" in questions[0].text
    assert "первая смена" in questions[0].text.casefold()

    common_metadata = {
        "source_type": "yonote",
        "category": "форумы",
        "forum_normalized": "Машук",
        "topic": questions[0].topic or "daty_nachala_meropriyatiya",
        "intent_examples": [questions[0].text],
    }
    wrong_minor = ScoredChunk(
        chunk_id="yonote_minor_dates",
        text="Первая смена для участников 14–17 лет: 08.08.2026–22.08.2026.",
        metadata=common_metadata,
        score=0.99,
        reranker_score=0.99,
    )
    correct_adult = wrong_minor.model_copy(
        update={
            "chunk_id": "yonote_adult_dates",
            "text": "Первая смена для участников 18–35 лет: 08.08.2026–15.08.2026.",
            "score": 0.90,
            "reranker_score": 0.90,
        }
    )

    result = await generate(
        {
            "message_masked": query,
            "analysis": analysis,
            "reranked_chunks": [wrong_minor, correct_adult],
            "max_confidence": 0.99,
            "llm_client": RaisingLLM(),
        }
    )

    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_adult_dates"]
    assert "18–35 лет" in result["generated_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        (
            "Когда проходит смена «Правда» и кто оплачивает проезд "
            "на форуме Территория смыслов?"
        ),
        (
            "Когда проходит смена Правда и кто оплачивает проезд "
            "на форуме Территория смыслов?"
        ),
    ],
)
async def test_fallback_pipeline_links_named_section_to_adjacent_date_chunk(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    forum = "Территория смыслов"
    document_id = "territory-2026"
    base_metadata = {
        "source_type": "yonote",
        "category": "форумы",
        "forum_normalized": forum,
        "source_document_id": document_id,
    }
    overall = ScoredChunk(
        chunk_id="yonote_territory_overall",
        text="Форум проходит с 20 июля по 6 августа 2026 года.",
        metadata={**base_metadata, "topic": "o_meropriyatii", "source_row": 1},
        score=0.99,
        reranker_score=0.99,
    )
    anchor = ScoredChunk(
        chunk_id="yonote_territory_pravda",
        text="Правда — тематическая смена форума.",
        metadata={
            **base_metadata,
            "topic": "pravda",
            "intent_name": "Правда",
            "source_row": 8,
        },
        score=0.95,
        reranker_score=0.95,
    )
    pravda_dates = ScoredChunk(
        chunk_id="yonote_territory_pravda_dates",
        text="Даты: 26–30 июля 2026 года.",
        metadata={
            **base_metadata,
            "topic": "daty_26_30_iyulya_2026_goda",
            "source_row": 9,
        },
        score=0.90,
        reranker_score=0.90,
    )
    travel = ScoredChunk(
        chunk_id="yonote_territory_travel",
        text="Проезд участники оплачивают самостоятельно.",
        metadata={
            **base_metadata,
            "topic": "oplata_proezda",
            "intent_examples": ["Кто оплачивает проезд?"],
            "source_row": 20,
        },
        score=0.94,
        reranker_score=0.94,
    )
    analysis = _fallback_analysis(query, query, {"complexity": "complex"}, None)
    assert analysis is not None
    effective = build_effective_questions(analysis, query)
    assert any("Правда" in question.text for question in effective)
    llm = StubLLM(
        "Смена «Правда» проходит с 26 по 30 июля 2026 года "
        "[src:yonote_territory_pravda_dates]\n"
        "Проезд участники оплачивают самостоятельно "
        "[src:yonote_territory_travel]"
    )

    result = await generate(
        {
            "message_masked": query,
            "analysis": analysis,
            "reranked_chunks": [overall, anchor, travel, pravda_dates],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert result.get("should_escalate") is not True
    assert set(result["cited_sources"]) == {
        "yonote_territory_pravda_dates",
        "yonote_territory_travel",
    }
    assert "20 июля" not in result["generated_response"]


@pytest.mark.asyncio
async def test_technical_profile_accepts_error_specific_recovery_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.generate.get_settings",
        lambda: SimpleNamespace(
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )
    source = ScoredChunk(
        chunk_id="yonote_technical_recovery",
        text=(
            "Если кнопка подачи заявки не работает, заполни недостающие обязательные "
            "поля и повторно нажми кнопку «Подать заявку». " * 10
        ),
        metadata={
            "source_type": "yonote",
            "category": "техподдержка",
            "topic": "technical",
            "intent_examples": ["Кнопка подачи заявки не работает"],
        },
        score=0.95,
        reranker_score=0.95,
    )
    llm = StubLLM(
        "Если кнопка не работает, заполни недостающие обязательные поля "
        "и повторно нажми «Подать заявку» [src:yonote_technical_recovery]"
    )
    query = "Кнопка подачи заявки не работает. Что делать?"

    result = await generate(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(
                complexity=Complexity.SIMPLE,
                category="техподдержка",
                response_profile=ResponseProfileName.TECHNICAL,
                questions=[
                    Question(
                        text=query,
                        category="техподдержка",
                        topic="technical",
                    )
                ],
            ),
            "reranked_chunks": [source],
            "max_confidence": 0.95,
            "llm_client": llm,
        }
    )

    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == ["yonote_technical_recovery"]
