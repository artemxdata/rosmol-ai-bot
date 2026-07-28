from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.nodes.generate import (
    COMPLEX_RESPONSE_MAX_CHARS,
    SIMPLE_RESPONSE_MAX_CHARS,
    generate,
)
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
    assert result["generated_response"] == (
        "В программе будут лекции. [src:yonote_program]"
    )


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
