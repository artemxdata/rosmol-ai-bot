from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import pytest

from src.graph.nodes.generate import (
    _bounded_published_source_result,
    _generate_with_llm_or_source_fallback,
    _llm_claims_have_bound_source_facts,
    generate,
)
from src.logging.tracer import Tracer
from src.models import Complexity, QueryAnalysis, Question, ScoredChunk
from src.response_contract import ResponseProfileName

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "data/knowledge_base_seed.json").read_text(encoding="utf-8"))
SEED_BY_ID = {record["chunk_id"]: record for record in SEED}


class ForbiddenLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs: object) -> str:
        self.calls += 1
        raise AssertionError("bounded published-source answer must not call an LLM")


class ReturningLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    async def generate(self, **_kwargs: object) -> str:
        self.calls += 1
        return self.response


def _chunk(chunk_id: str, score: float = 0.98) -> ScoredChunk:
    record = SEED_BY_ID[chunk_id]
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


def _state(
    *,
    query: str,
    chunks: list[ScoredChunk],
    questions: list[Question],
    category: str,
    forum: str | None = None,
    profile: ResponseProfileName = ResponseProfileName.GENERIC,
    complexity: Complexity = Complexity.COMPLEX,
) -> tuple[dict[str, object], ForbiddenLLM]:
    llm = ForbiddenLLM()
    return (
        {
            "message_masked": query,
            "analysis": QueryAnalysis(
                complexity=complexity,
                category=category,
                forum_normalized=forum,
                response_profile=profile,
                questions=questions,
            ),
            "reranked_chunks": chunks,
            "max_confidence": 0.98,
            "llm_client": llm,
        },
        llm,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chunk_id", "question", "category", "forum", "profile", "expected"),
    [
        (
            "До какого числа можно подать заявку на Национальную премию «Патриот»?",
            "yonote_api_tnorqqrmvg_s0002_registraciya",
            Question(
                text="До какого числа можно подать заявку?",
                category="форумы",
                forum_normalized="Национальная премия «Патриот»",
                topic="registraciya",
            ),
            "форумы",
            "Национальная премия «Патриот»",
            ResponseProfileName.APPLICATION,
            ("12.09.2026",),
        ),
        (
            "Кто может участвовать в Национальной премии «Патриот»?",
            "yonote_api_tnorqqrmvg_s0003_uchastniki",
            Question(
                text="Кто может участвовать?",
                category="форумы",
                forum_normalized="Национальная премия «Патриот»",
                topic="uchastniki",
            ),
            "форумы",
            "Национальная премия «Патриот»",
            ResponseProfileName.ELIGIBILITY,
            (
                "гражданин Российской Федерации",
                "18 до 35 лет",
                "юридическое лицо",
                "иностранного государства",
                "18 до 55 лет",
            ),
        ),
        (
            "Я потерял доступ к почте от профиля ФГАИС. Как восстановить доступ к данным?",
            "yonote_api_u7b5sscrri_s0006_obedinenie_akkauntov",
            Question(
                text="Как перенести данные после потери доступа к почте профиля ФГАИС?",
                category="платформа_фгаис",
                topic="obedinenie_akkauntov",
            ),
            "платформа_фгаис",
            None,
            ResponseProfileName.TECHNICAL,
            ("создать аккаунт", "Госуслуг", "ID этого аккаунта", "support@myrosmol.ru"),
        ),
        (
            "Когда будут известны результаты отбора на форум «Машук»?",
            "yonote_api_pmbmqm6lug_s0009_rezultaty",
            Question(
                text="Когда будут известны результаты отбора?",
                category="форумы",
                forum_normalized="Машук",
                topic="rezultaty_rm",
            ),
            "форумы",
            "Машук",
            ResponseProfileName.SELECTION_STATUS,
            ("14 календарных дней", "до даты начала"),
        ),
        (
            "Назови период проведения первой смены форума «Машук».",
            "yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",
            Question(
                text="Назови период проведения первой смены форума «Машук».",
                category="форумы",
                forum_normalized="Машук",
                topic="daty_nachala_meropriyatiya",
            ),
            "форумы",
            "Машук",
            ResponseProfileName.DATES,
            ("8 августа", "15 августа"),
        ),
    ],
)
async def test_pilot50_single_published_source_uses_bounded_extractive_fast_path(
    query: str,
    chunk_id: str,
    question: Question,
    category: str,
    forum: str | None,
    profile: ResponseProfileName,
    expected: tuple[str, ...],
) -> None:
    state, llm = _state(
        query=query,
        chunks=[_chunk(chunk_id)],
        questions=[question],
        category=category,
        forum=forum,
        profile=profile,
    )

    result = await generate(state)

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == [chunk_id]
    assert all(fragment in result["generated_response"] for fragment in expected)


def test_grant_application_selected_source_has_bounded_grounded_answer() -> None:
    chunk_id = "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm"
    question = Question(
        text="Как подать заявку на первый сезон Росмолодёжь.Гранты?",
        category="гранты",
        forum_normalized="Росмолодёжь.Гранты 1 сезон",
        topic="poshagovyy_algoritm",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="гранты",
        forum_normalized="Росмолодёжь.Гранты 1 сезон",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )

    result = _bounded_published_source_result(
        analysis=analysis,
        questions=[question],
        source_chunks=[_chunk(chunk_id)],
        response_limit=900,
    )

    assert result is not None
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == [chunk_id]
    assert "Госуслуг" in result["generated_response"]
    assert "ФГАИС" in result["generated_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chunk_ids", "questions", "category", "forum", "profile", "expected"),
    [
        (
            "Почта от старого профиля ФГАИС потеряна: как перенести данные "
            "и заодно что значит статус «Одобрена»?",
            (
                "yonote_api_u7b5sscrri_s0006_obedinenie_akkauntov",
                "yonote_api_u7b5sscrri_s0016_statusy_zayavok",
            ),
            [
                Question(
                    text="Как перенести данные после потери доступа к почте профиля ФГАИС?",
                    category="платформа_фгаис",
                    topic="obedinenie_akkauntov",
                ),
                Question(
                    text="Что значит статус «Одобрена»?",
                    category="платформа_фгаис",
                    topic="statusy_zayavok",
                ),
            ],
            "платформа_фгаис",
            None,
            ResponseProfileName.TECHNICAL,
            (
                "создать аккаунт",
                "Госуслуг",
                "ID этого аккаунта",
                "support@myrosmol.ru",
                "организаторы одобрили участие",
            ),
        ),
        (
            "Не смешивай этапы: сколько проверяют проект грантового соглашения "
            "и сколько — уже итоговый отчёт?",
            (
                "yonote_api_g4yfzssrsd_s0056_proverka_proekta_grantovogo_soglasheniya",
                "yonote_api_g4yfzssrsd_s0079_proverka_otcheta",
            ),
            [
                Question(
                    text="Сколько проверяют проект грантового соглашения?",
                    category="гранты",
                    topic="proverka_proekta_grantovogo_soglasheniya",
                ),
                Question(
                    text="Сколько проверяют итоговый отчёт?",
                    category="гранты",
                    topic="proverka_otcheta",
                ),
            ],
            "гранты",
            None,
            ResponseProfileName.GRANTS,
            ("до 30 дней", "до 30 рабочих дней"),
        ),
        (
            "По «Машуку» без догадок: когда объявят результаты отбора и когда дадут программу?",
            (
                "yonote_api_pmbmqm6lug_s0009_rezultaty",
                "yonote_api_pmbmqm6lug_s0013_programma_foruma",
            ),
            [
                Question(
                    text="Когда объявят результаты отбора?",
                    category="форумы",
                    forum_normalized="Машук",
                    topic="rezultaty_rm",
                ),
                Question(
                    text="Когда дадут программу?",
                    category="форумы",
                    forum_normalized="Машук",
                    topic="programma_foruma",
                ),
            ],
            "форумы",
            "Машук",
            ResponseProfileName.SELECTION_STATUS,
            ("14 календарных дней", "за сутки до начала"),
        ),
        (
            "Сверь календарь «Машука»: какие даты у первой и второй смен и когда разъезд каждой?",
            (
                "yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",
                "yonote_api_pmbmqm6lug_s0003_2_smena_15_22_avgusta",
            ),
            [
                Question(
                    text="Какие даты у первой смены и когда разъезд?",
                    category="форумы",
                    forum_normalized="Машук",
                    topic="1_smena_8_15_avgusta",
                ),
                Question(
                    text="Какие даты у второй смены и когда отъезд?",
                    category="форумы",
                    forum_normalized="Машук",
                    topic="2_smena_15_22_avgusta",
                ),
            ],
            "форумы",
            "Машук",
            ResponseProfileName.DATES,
            ("8 августа", "15 августа", "22 августа", "разъезд", "отъезд"),
        ),
    ],
)
async def test_pilot50_multi_published_sources_use_one_exact_claim_per_source(
    query: str,
    chunk_ids: tuple[str, ...],
    questions: list[Question],
    category: str,
    forum: str | None,
    profile: ResponseProfileName,
    expected: tuple[str, ...],
) -> None:
    state, llm = _state(
        query=query,
        chunks=[_chunk(chunk_id, 0.99 - index / 100) for index, chunk_id in enumerate(chunk_ids)],
        questions=questions,
        category=category,
        forum=forum,
        profile=profile,
    )
    tracer = Tracer()
    state["trace"] = tracer

    result = await generate(state)

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["generator_model"] == "source_chunk"
    assert set(result["cited_sources"]) == set(chunk_ids)
    assert all(fragment in result["generated_response"] for fragment in expected)
    assert result["generated_response"].count("[src:") >= len(chunk_ids)
    selection = next(
        event for event in reversed(tracer.events) if event.node == "generate_selection"
    )
    assert selection.metadata["selected_source_ids"] == list(chunk_ids)
    assert selection.metadata["cited_source_ids"] == list(chunk_ids)


def test_patriot_two_source_answer_requires_exact_forum_bound_sources() -> None:
    chunk_ids = (
        "yonote_api_tnorqqrmvg_s0002_registraciya",
        "yonote_api_tnorqqrmvg_s0003_uchastniki",
    )
    questions = [
        Question(
            text="До какого числа можно подать заявку?",
            category="форумы",
            forum_normalized="Национальная премия «Патриот»",
            topic="registraciya",
        ),
        Question(
            text="Кто может участвовать?",
            category="форумы",
            forum_normalized="Национальная премия «Патриот»",
            topic="uchastniki",
        ),
    ]
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        response_profile=ResponseProfileName.ELIGIBILITY,
        questions=questions,
    )

    result = _bounded_published_source_result(
        analysis=analysis,
        questions=questions,
        source_chunks=[_chunk(chunk_id) for chunk_id in chunk_ids],
        response_limit=900,
    )

    assert result is not None
    assert result["cited_sources"] == list(chunk_ids)
    assert all(
        fragment in result["generated_response"]
        for fragment in ("12.09.2026", "18 до 35 лет", "18 до 55 лет")
    )


@pytest.mark.asyncio
async def test_dobro_two_source_answer_binds_and_cites_both_published_sources() -> None:
    chunk_ids = (
        "yonote_api_jw4tdtr1pc_s0005_registraciya_s_pomoschyu_sozdaniya_kabineta",
        "yonote_api_jw4tdtr1pc_s0008_volonterskaya_pomosch",
    )
    questions = [
        Question(
            text="Как создать кабинет на Добро.РФ?",
            category="форумы",
            forum_normalized="Добро.РФ",
            topic="registraciya_s_pomoschyu_sozdaniya_kabineta",
        ),
        Question(
            text="Как отфильтровать мероприятие и подать заявку волонтёром?",
            category="форумы",
            forum_normalized="Добро.РФ",
            topic="volonterskaya_pomosch",
        ),
    ]
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Добро.РФ",
        response_profile=ResponseProfileName.APPLICATION,
        questions=questions,
    )
    llm = ForbiddenLLM()
    tracer = Tracer()

    result = await _generate_with_llm_or_source_fallback(
        state={
            "message_masked": (
                "На Добро.РФ хочу с нуля: как создать кабинет, а потом "
                "отфильтровать мероприятие и подать заявку волонтёром?"
            ),
            "analysis": analysis,
            "reranked_chunks": [_chunk(chunk_id) for chunk_id in chunk_ids],
            "max_confidence": 0.98,
            "llm_client": llm,
            "trace": tracer,
        },
        analysis=analysis,
        questions=questions,
        source_chunks=[_chunk(chunk_id) for chunk_id in chunk_ids],
        started_at=perf_counter(),
    )

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == list(chunk_ids)
    assert all(
        fragment in result["generated_response"]
        for fragment in (
            "письмо",
            "подтвержд",
            "аккаунт будет создан",
            "фильтров поиска",
            "заявк",
        )
    )
    assert tracer.events[-1].metadata["mode"] == "bounded_published_source_chunk"


def test_yearless_published_date_claim_remains_bound_when_heading_contains_year() -> None:
    source = _chunk("yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta")

    assert _llm_claims_have_bound_source_facts(
        "8 августа — день заезда [src:yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta]",
        [source],
    )
    assert not _llm_claims_have_bound_source_facts(
        "8 августа 2025 года — день заезда "
        "[src:yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta]",
        [source],
    )


@pytest.mark.parametrize(
    ("metadata_key", "invalid_value"),
    [
        ("source_type", "answer_bank"),
        ("source", "yonote_backup"),
        ("version", "yonote-api-v0"),
        ("status", "draft"),
    ],
)
def test_bounded_source_fast_path_rejects_metadata_drift(
    metadata_key: str,
    invalid_value: str,
) -> None:
    source = _chunk("yonote_api_tnorqqrmvg_s0002_registraciya")
    source = source.model_copy(
        update={"metadata": {**source.metadata, metadata_key: invalid_value}}
    )
    question = Question(
        text="До какого числа можно подать заявку?",
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        topic="registraciya",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_rejects_same_topic_from_wrong_forum() -> None:
    source = _chunk("yonote_api_zrvcb9k240_s0002_registraciya")
    question = Question(
        text="До какого числа можно подать заявку?",
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        topic="registraciya",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_rejects_same_topic_from_wrong_category() -> None:
    source = _chunk("yonote_api_tnorqqrmvg_s0002_registraciya")
    source = source.model_copy(
        update={"metadata": {**source.metadata, "category": "гранты"}}
    )
    question = Question(
        text="До какого числа можно подать заявку?",
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        topic="registraciya",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_keeps_uncovered_effective_question() -> None:
    source = _chunk("yonote_api_pmbmqm6lug_s0009_rezultaty")
    result_question = Question(
        text="Когда объявят результаты отбора?",
        category="форумы",
        forum_normalized="Машук",
        topic="rezultaty_rm",
    )
    program_question = Question(
        text="Когда дадут программу?",
        category="форумы",
        forum_normalized="Машук",
        topic="programma_foruma",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Машук",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        questions=[result_question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[result_question, program_question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_rejects_wrong_shift_ordinal() -> None:
    source = _chunk("yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta")
    question = Question(
        text="Когда проходит вторая смена форума «Машук»?",
        category="форумы",
        forum_normalized="Машук",
        topic="daty_nachala_meropriyatiya",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Машук",
        response_profile=ResponseProfileName.DATES,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_rejects_uncovered_extra_aspect() -> None:
    source = _chunk("yonote_api_pmbmqm6lug_s0009_rezultaty")
    questions = [
        Question(
            text="Когда объявят результаты отбора?",
            category="форумы",
            forum_normalized="Машук",
            topic="rezultaty_rm",
        ),
        Question(
            text="Когда дадут программу?",
            category="форумы",
            forum_normalized="Машук",
            topic="programma_foruma",
        ),
    ]
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Машук",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        questions=questions,
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=questions,
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_rejects_multiple_user_links() -> None:
    source = _chunk("yonote_api_tnorqqrmvg_s0002_registraciya")
    source = source.model_copy(
        update={
            "text": source.text.replace(
                "до 12.09.2026",
                "и https://example.invalid до 12.09.2026",
                1,
            )
        }
    )
    question = Question(
        text="Какой крайний срок подачи заявки?",
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        topic="registraciya",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Национальная премия «Патриот»",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


def test_bounded_source_fast_path_rejects_overlength_excerpt() -> None:
    source = _chunk("yonote_api_pmbmqm6lug_s0009_rezultaty")
    source = source.model_copy(
        update={
            "text": (
                "Результаты\n\nРезультаты отбора будут известны за 14 календарных "
                "дней до даты начала смены, "
                + "подтверждённые организаторами " * 50
                + "."
            )
        }
    )
    question = Question(
        text="Когда объявят результаты отбора?",
        category="форумы",
        forum_normalized="Машук",
        topic="rezultaty_rm",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="форумы",
        forum_normalized="Машук",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
        )
        is None
    )


@pytest.mark.asyncio
async def test_unsupported_published_topic_keeps_guarded_llm_fallback() -> None:
    source = ScoredChunk(
        chunk_id="yonote_api_test_unsupported_topic",
        text="Подтверждённый опубликованный факт.",
        metadata={
            "source_type": "yonote",
            "source": "yonote_api",
            "version": "yonote-api-v1",
            "status": "published",
            "category": "общее",
            "topic": "unsupported_topic",
        },
        score=0.98,
        reranker_score=0.98,
    )
    llm = ReturningLLM(
        "Подтверждённый опубликованный факт. "
        "[src:yonote_api_test_unsupported_topic]"
    )
    query = "Какой факт подтверждён?"
    question = Question(
        text=query,
        category="общее",
        topic="unsupported_topic",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="общее",
        response_profile=ResponseProfileName.GENERIC,
        questions=[question],
    )

    result = await _generate_with_llm_or_source_fallback(
        state={
            "message_masked": query,
            "analysis": analysis,
            "reranked_chunks": [source],
            "max_confidence": 0.98,
            "llm_client": llm,
        },
        analysis=analysis,
        questions=[question],
        source_chunks=[source],
        started_at=perf_counter(),
    )

    assert llm.calls == 1
    assert result.get("should_escalate") is not True
    assert result["generator_model"] != "source_chunk"
