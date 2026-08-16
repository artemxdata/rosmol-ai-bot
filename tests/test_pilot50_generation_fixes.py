from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from time import perf_counter

import pytest

import src.graph.nodes.generate as generate_node
from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.generate import (
    _bounded_published_source_result,
    _generate_with_llm_or_source_fallback,
    _llm_claims_have_bound_source_facts,
    _request_bound_published_source_result,
    generate,
)
from src.graph.nodes.guard import apply_response_guards
from src.graph.nodes.respond import normalize_final_response
from src.graph.nodes.verify import verify
from src.kb.temporal import MOSCOW_TZ
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
    final_response = normalize_final_response(result["generated_response"])
    assert all(fragment in final_response for fragment in expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chunk_id", "expected"),
    [
        (
            "Где во ФГАИС найти доступные мероприятия?",
            "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ("разделение мероприятий", "Фильтры универсальные", "подраздела"),
        ),
        (
            "Что означают статусы заявки во ФГАИС?",
            "yonote_api_u7b5sscrri_s0016_statusy_zayavok",
            (
                "На рассмотрении",
                "Одобрена",
                "Подтверждено участие",
                "Резерв",
            ),
        ),
        (
            "Как найти волонтёрское мероприятие и подать заявку на Добро.РФ?",
            "yonote_api_jw4tdtr1pc_s0008_volonterskaya_pomosch",
            ("фильтров поиска", "подачи заявки", "заполнить анкету"),
        ),
        (
            "Что такое гранты для физических лиц?",
            "yonote_api_g4yfzssrsd_s0001_obschaya_informaciya",
            ("Цель конкурса", "Участники конкурса"),
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "yonote_api_g4yfzssrsd_s0001_obschaya_informaciya",
            ("верификацию учетной записи", "Мои проекты", "Мои мероприятия"),
        ),
    ],
    ids=(
        "event-navigation",
        "generic-status-glossary",
        "dobro-location-filter",
        "physical-grants-overview",
        "personal-grant-application",
    ),
)
async def test_observed_typical_queries_use_grounded_bounded_source_path(
    query: str,
    chunk_id: str,
    expected: tuple[str, ...],
) -> None:
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )

    result = await generate(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "reranked_chunks": [_chunk(chunk_id)],
            "max_confidence": 0.98,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == [chunk_id]
    assert all(fragment in result["generated_response"] for fragment in expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chunk_ids", "expected", "absent"),
    [
        (
            "Кто и сколько времени проверяет проект грантового соглашения?",
            (
                "yonote_api_g4yfzssrsd_s0056_proverka_proekta_grantovogo_soglasheniya",
            ),
            ("проверяет куратор", "до 30 дней"),
            (),
        ),
        (
            "Я потерял доступ к почте от профиля ФГАИС. "
            "Как восстановить доступ к данным?",
            ("yonote_api_u7b5sscrri_s0006_obedinenie_akkauntov",),
            ("создать аккаунт", "новый аккаунт", "перенесут"),
            ("активная заявка на грантовый конкурс",),
        ),
        (
            "По «Ладоге» сразу три вещи: до какого числа заявка, кто платит "
            "за проживание с едой и могут ли компенсировать дорогу?",
            (
                "yonote_api_irwwd4t2v8_s0006_forum",
                "yonote_api_irwwd4t2v8_s0008_pitanie_i_prozhivanie",
                "yonote_api_irwwd4t2v8_s0012_kompensaciya",
            ),
            (
                "30 июня 2026",
                "за счет организаторов",
                "могут быть компенсированы",
            ),
            (),
        ),
        (
            "Премия «Патриот»: кто вообще может участвовать и когда крайний "
            "срок подачи?",
            (
                "yonote_api_tnorqqrmvg_s0002_registraciya",
                "yonote_api_tnorqqrmvg_s0003_uchastniki",
            ),
            ("12.09.2026", "18 до 35 лет", "18 до 55 лет"),
            (),
        ),
        (
            "Не смешивай этапы: сколько проверяют проект грантового соглашения "
            "и сколько — уже итоговый отчёт?",
            (
                "yonote_api_g4yfzssrsd_s0056_proverka_proekta_grantovogo_soglasheniya",
                "yonote_api_g4yfzssrsd_s0079_proverka_otcheta",
            ),
            ("до 30 дней", "до 30 рабочих дней"),
            (),
        ),
        (
            "Сверь календарь «Машука»: какие даты у первой и второй смен и "
            "когда разъезд каждой?",
            (
                "yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",
                "yonote_api_pmbmqm6lug_s0003_2_smena_15_22_avgusta",
            ),
            ("8 августа", "15 августа", "22 августа", "разъезд", "отъезд"),
            (),
        ),
        (
            "Блин, как получить билет на День молодёжи после регистрации через МАХ?",
            (
                "yonote_api_nwr3m74g03_s0003_"
                "sposob_1_cherez_chat_bot_v_mah_https_max_ru_youthday_bot",
            ),
            ("код билета", "диалоге", "почту"),
            ("фамилию", "телефон"),
        ),
        (
            "Назови период проведения первой смены форума «Машук».",
            ("yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",),
            ("8 августа", "15 августа"),
            (),
        ),
    ],
    ids=(
        "agreement-review-owner-and-duration",
        "account-recovery-without-unrequested-grant-condition",
        "ladoga-three-aspect-request",
        "patriot-eligibility-and-deadline",
        "agreement-and-report-review-timelines",
        "mashuk-two-shift-calendar",
        "youth-day-ticket-after-max-registration",
        "mashuk-first-shift-period",
    ),
)
async def test_stage_v2_supported_queries_use_request_bound_published_sources(
    query: str,
    chunk_ids: tuple[str, ...],
    expected: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )
    generation_calls_before = llm.calls
    tracer = Tracer()

    result = await generate(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "reranked_chunks": [
                _chunk(chunk_id, 0.99 - index / 100)
                for index, chunk_id in enumerate(chunk_ids)
            ],
            "max_confidence": 0.99,
            "llm_client": llm,
            "trace": tracer,
        }
    )

    assert llm.calls == generation_calls_before
    assert result.get("should_escalate") is not True
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == list(chunk_ids)
    final_response = normalize_final_response(result["generated_response"])
    assert all(fragment in final_response for fragment in expected)
    assert all(fragment not in final_response for fragment in absent)
    selection = next(
        event for event in reversed(tracer.events) if event.node == "generate_selection"
    )
    assert selection.metadata["selected_source_ids"] == list(chunk_ids)
    assert selection.metadata["cited_source_ids"] == list(chunk_ids)


@pytest.mark.asyncio
async def test_request_bound_aspects_support_novel_ladoga_recombination() -> None:
    query = (
        "По «Ладоге»: до какого числа заявка и могут ли компенсировать дорогу?"
    )
    chunk_ids = (
        "yonote_api_irwwd4t2v8_s0006_forum",
        "yonote_api_irwwd4t2v8_s0012_kompensaciya",
    )
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )

    result = await generate(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "reranked_chunks": [_chunk(chunk_id) for chunk_id in chunk_ids],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["cited_sources"] == list(chunk_ids)
    assert "30 июня 2026" in result["generated_response"]
    assert "могут быть компенсированы" in result["generated_response"]
    assert "Проживание и питание" not in result["generated_response"]


@pytest.mark.asyncio
async def test_expired_ladoga_deadline_is_closed_without_dropping_other_aspects() -> None:
    query = (
        "По «Ладоге» сразу три вещи: до какого числа заявка, кто платит "
        "за проживание с едой и могут ли компенсировать дорогу?"
    )
    chunk_ids = (
        "yonote_api_irwwd4t2v8_s0006_forum",
        "yonote_api_irwwd4t2v8_s0008_pitanie_i_prozhivanie",
        "yonote_api_irwwd4t2v8_s0012_kompensaciya",
    )
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )
    state = {
        "message": query,
        "message_masked": query,
        "contextual_message": analyzed["contextual_message"],
        "analysis": analyzed["analysis"],
        "reranked_chunks": [_chunk(chunk_id) for chunk_id in chunk_ids],
        "max_confidence": 0.99,
        "llm_client": llm,
    }

    generated = await generate(state)
    guarded = await apply_response_guards({**state, **generated})
    verified = await verify({**state, **generated, **guarded})

    assert guarded["response_guard"] == "registration_closed_multi_aspect"
    assert "Регистрация на форум «Ладога» закрыта" in guarded["generated_response"]
    assert "Новую заявку сейчас подать нельзя" in guarded["generated_response"]
    assert "Подать заявку на участие в форуме можно" not in guarded["generated_response"]
    assert "Проживание и питание за счет организаторов" in guarded["generated_response"]
    assert "могут быть компенсированы" in guarded["generated_response"]
    assert guarded["cited_sources"] == list(chunk_ids)
    assert verified.get("should_escalate") is not True
    assert verified["verification"].has_hallucination is False


@pytest.mark.asyncio
async def test_future_patriot_deadline_remains_open_through_guard_and_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            current = cls(2026, 8, 12, 12, 0, tzinfo=MOSCOW_TZ)
            return current if tz is None else current.astimezone(tz)

    monkeypatch.setattr("src.kb.temporal.datetime", FrozenDatetime)
    query = (
        "Премия «Патриот»: кто вообще может участвовать и когда крайний "
        "срок подачи?"
    )
    chunk_ids = (
        "yonote_api_tnorqqrmvg_s0002_registraciya",
        "yonote_api_tnorqqrmvg_s0003_uchastniki",
    )
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )
    state = {
        "message": query,
        "message_masked": query,
        "contextual_message": analyzed["contextual_message"],
        "analysis": analyzed["analysis"],
        "reranked_chunks": [_chunk(chunk_id) for chunk_id in chunk_ids],
        "max_confidence": 0.99,
        "llm_client": llm,
    }

    generated = await generate(state)
    guarded = await apply_response_guards({**state, **generated})
    verified = await verify({**state, **generated, **guarded})

    assert guarded == {}
    assert "12.09.2026" in generated["generated_response"]
    assert generated["cited_sources"] == list(chunk_ids)
    assert verified.get("should_escalate") is not True
    assert verified["verification"].has_hallucination is False


@pytest.mark.asyncio
async def test_guard_does_not_borrow_deadline_from_an_uncited_registration_flow() -> None:
    query = "Хочу попасть на форум ШУМ — что нужно сделать?"
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )
    state = {
        "message": query,
        "message_masked": query,
        "contextual_message": analyzed["contextual_message"],
        "analysis": analyzed["analysis"],
        "reranked_chunks": [
            _chunk("yonote_api_zhjxnhwbyi_s0002_registraciya"),
            _chunk("yonote_api_zhjxnhwbyi_s0012_grantovyy_konkurs"),
        ],
        "max_confidence": 0.99,
        "llm_client": llm,
    }

    generated = await generate(state)
    guarded = await apply_response_guards({**state, **generated})

    assert generated["cited_sources"] == [
        "yonote_api_zhjxnhwbyi_s0002_registraciya"
    ]
    assert guarded == {}
    assert "регистрация на фгаис" in generated["generated_response"].casefold()
    assert "13 июля" not in generated["generated_response"]


@pytest.mark.asyncio
async def test_request_bound_answer_reflects_changed_published_clause() -> None:
    query = "По «Ладоге» могут ли компенсировать дорогу?"
    source = _chunk("yonote_api_irwwd4t2v8_s0012_kompensaciya")
    source = source.model_copy(
        update={
            "text": source.text.replace(
                "могут быть компенсированы",
                "компенсируются полностью",
                1,
            )
        }
    )
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": None,
        }
    )

    result = _request_bound_published_source_result(
        state={
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
        },
        analysis=analyzed["analysis"],
        chunks=[source],
        max_confidence=0.99,
    )

    assert result is not None
    assert "компенсируются полностью" in result["generated_response"]
    assert "могут быть компенсированы" not in result["generated_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chunk_ids"),
    [
        (
            "По «Ладоге» до какого числа заявка и парковка?",
            (
                "yonote_api_irwwd4t2v8_s0006_forum",
                "yonote_api_irwwd4t2v8_s0008_pitanie_i_prozhivanie",
                "yonote_api_irwwd4t2v8_s0012_kompensaciya",
            ),
        ),
        (
            "Когда крайний срок подачи и программа премии «Патриот»?",
            (
                "yonote_api_tnorqqrmvg_s0002_registraciya",
                "yonote_api_tnorqqrmvg_s0003_uchastniki",
            ),
        ),
        (
            "Сколько проверяют проект грантового соглашения, сколько итоговый "
            "отчёт и где скачать документы?",
            (
                "yonote_api_g4yfzssrsd_s0056_proverka_proekta_grantovogo_soglasheniya",
                "yonote_api_g4yfzssrsd_s0079_proverka_otcheta",
            ),
        ),
        (
            "Сверь даты первой, второй и третьей смен «Машука».",
            (
                "yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",
                "yonote_api_pmbmqm6lug_s0003_2_smena_15_22_avgusta",
            ),
        ),
        (
            "Как зарегистрироваться на День молодёжи через МАХ?",
            (
                "yonote_api_nwr3m74g03_s0003_"
                "sposob_1_cherez_chat_bot_v_mah_https_max_ru_youthday_bot",
            ),
        ),
        (
            "Назови период второй смены форума «Машук».",
            ("yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",),
        ),
    ],
    ids=(
        "ladoga-unsupported-parking-aspect",
        "patriot-unsupported-program-aspect",
        "grant-unsupported-documents-aspect",
        "mashuk-unsupported-third-shift",
        "youth-day-registration-not-post-registration-ticket",
        "mashuk-wrong-shift",
    ),
)
async def test_request_bound_source_plans_fail_closed_outside_proven_scope(
    query: str,
    chunk_ids: tuple[str, ...],
) -> None:
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": None,
        }
    )
    state = {
        "message": query,
        "message_masked": query,
        "contextual_message": analyzed["contextual_message"],
        "analysis": analyzed["analysis"],
        "reranked_chunks": [_chunk(chunk_id) for chunk_id in chunk_ids],
        "max_confidence": 0.99,
    }

    assert (
        _request_bound_published_source_result(
            state=state,
            analysis=analyzed["analysis"],
            chunks=state["reranked_chunks"],
            max_confidence=0.99,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chunk_ids"),
    [
        (
            "По «Ладоге»: до какого числа заявка и будет ли доступен Wi-Fi?",
            ("yonote_api_irwwd4t2v8_s0006_forum",),
        ),
        (
            "По «Ладоге»: кто платит за проживание с едой, могут ли "
            "компенсировать дорогу и нужен ли загранпаспорт?",
            (
                "yonote_api_irwwd4t2v8_s0008_pitanie_i_prozhivanie",
                "yonote_api_irwwd4t2v8_s0012_kompensaciya",
            ),
        ),
        (
            "По «Ладоге»: до какого числа заявка и будет ли работать коворкинг?",
            ("yonote_api_irwwd4t2v8_s0006_forum",),
        ),
    ],
    ids=("wifi", "passport", "novel-unmapped-clause"),
)
async def test_unmapped_compound_clause_uses_generic_fail_closed_path(
    query: str,
    chunk_ids: tuple[str, ...],
) -> None:
    llm = ForbiddenLLM()
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {},
            "llm_client": llm,
        }
    )

    result = await generate(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "reranked_chunks": [_chunk(chunk_id) for chunk_id in chunk_ids],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert llm.calls == 2
    assert result["should_escalate"] is True
    assert result["generated_response"] == ""
    assert result["cited_sources"] == []


@pytest.mark.asyncio
async def test_generation_cannot_bypass_operator_decision() -> None:
    query = "Я прошла отбор, но приглашение на почту так и не пришло."
    llm = ForbiddenLLM()
    analysis = QueryAnalysis(
        category="форумы",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        should_escalate=True,
        escalation_reason="personal_status",
    )

    result = await generate(
        {
            "message": query,
            "message_masked": query,
            "analysis": analysis,
            "reranked_chunks": [
                _chunk("yonote_api_u7b5sscrri_s0014_podtverzhdenie_uchastiya_v_forume")
            ],
            "max_confidence": 0.99,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "personal_status"
    assert result["generated_response"] == ""
    assert result["cited_sources"] == []


@pytest.mark.asyncio
async def test_source_answerable_nonregistry_clause_uses_generic_composition() -> None:
    query = (
        "Почта от старого профиля ФГАИС потеряна: как перенести данные и "
        "заодно что значит статус «Одобрена»?"
    )
    chunk_ids = (
        "yonote_api_u7b5sscrri_s0006_obedinenie_akkauntov",
        "yonote_api_u7b5sscrri_s0016_statusy_zayavok",
    )
    questions = [
        Question(
            text="Как перенести данные после потери доступа к старой почте?",
            category="платформа_фгаис",
            topic="obedinenie_akkauntov",
        ),
        Question(
            text="Что значит статус Одобрена?",
            category="платформа_фгаис",
            topic="statusy_zayavok",
        ),
    ]
    state, llm = _state(
        query=query,
        chunks=[_chunk(chunk_id) for chunk_id in chunk_ids],
        questions=questions,
        category="платформа_фгаис",
        profile=ResponseProfileName.TECHNICAL,
    )
    analysis = state["analysis"]
    assert isinstance(analysis, QueryAnalysis)

    assert (
        _request_bound_published_source_result(
            state=state,
            analysis=analysis,
            chunks=state["reranked_chunks"],
            max_confidence=0.99,
        )
        is None
    )

    result = await generate(state)

    assert llm.calls == 0
    assert result["cited_sources"] == list(chunk_ids)
    assert "support@myrosmol.ru" in result["generated_response"]
    assert "Одобрена" in result["generated_response"]


def test_generic_status_summary_is_derived_from_changed_source_wording() -> None:
    chunk_id = "yonote_api_u7b5sscrri_s0016_statusy_zayavok"
    source = _chunk(chunk_id)
    source = source.model_copy(
        update={
            "text": source.text.replace(
                "Заявка отклонена администратором.",
                "Заявка возвращена администратором на уточнение.",
                1,
            )
        }
    )
    question = Question(
        text="Что означают статусы заявки во ФГАИС?",
        category="платформа_фгаис",
        topic="statusy_zayavok",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.SIMPLE,
        category="платформа_фгаис",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        questions=[question],
    )

    result = _bounded_published_source_result(
        analysis=analysis,
        questions=[question],
        source_chunks=[source],
        response_limit=450,
        request_text=question.text,
    )

    assert result is not None
    assert "возвращена администратором" in result["generated_response"]
    assert "отклонена администратором" not in result["generated_response"].casefold()


def test_generic_status_summary_fails_closed_when_glossary_shape_is_incomplete() -> None:
    chunk_id = "yonote_api_u7b5sscrri_s0016_statusy_zayavok"
    source = _chunk(chunk_id)
    source = source.model_copy(
        update={"text": source.text.replace("9. Резерв.", "9. Ожидание.", 1)}
    )
    question = Question(
        text="Что означают статусы заявки во ФГАИС?",
        category="платформа_фгаис",
        topic="statusy_zayavok",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.SIMPLE,
        category="платформа_фгаис",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=450,
            request_text=question.text,
        )
        is None
    )


def test_generic_status_summary_does_not_answer_unknown_singular_status() -> None:
    chunk_id = "yonote_api_u7b5sscrri_s0016_statusy_zayavok"
    question = Question(
        text="Что значит статус «Верифицирована»?",
        category="платформа_фгаис",
        topic="statusy_zayavok",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.SIMPLE,
        category="платформа_фгаис",
        response_profile=ResponseProfileName.SELECTION_STATUS,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[_chunk(chunk_id)],
            response_limit=450,
            request_text=question.text,
        )
        is None
    )


@pytest.mark.parametrize(
    ("query", "category", "profile", "forum", "metadata_update"),
    [
        (
            "Как организации подать заявку на грант?",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {},
        ),
        (
            "Я юрлицо, хочу подать заявку на грант.",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {},
        ),
        (
            "Я представляю компанию и хочу подать заявку на грант.",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {},
        ),
        (
            "Я не физлицо, хочу подать заявку на грант.",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {},
        ),
        (
            "Хочу подать заявку на второй сезон Росмолодёжь.Гранты.",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {},
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "гранты",
            ResponseProfileName.GENERIC,
            None,
            {},
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "форумы",
            ResponseProfileName.APPLICATION,
            None,
            {},
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "гранты",
            ResponseProfileName.APPLICATION,
            "Росмолодёжь.Гранты 1 сезон",
            {},
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {"forum_normalized": "Гранты для юридических лиц"},
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {"topic": "usloviya_i_sroki_uchastiya"},
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "гранты",
            ResponseProfileName.APPLICATION,
            None,
            {"category": "форумы"},
        ),
    ],
    ids=(
        "organization-request",
        "legal-entity-short-form",
        "company-request",
        "explicit-nonphysical-request",
        "named-season",
        "wrong-profile",
        "wrong-category",
        "named-forum",
        "wrong-source-scope",
        "wrong-source-topic",
        "wrong-source-category",
    ),
)
def test_personal_grant_application_alias_fails_closed_outside_exact_scope(
    query: str,
    category: str,
    profile: ResponseProfileName,
    forum: str | None,
    metadata_update: dict[str, str],
) -> None:
    question = Question(
        text="Как подать заявку?",
        category=category,
        forum_normalized=forum,
        topic="podacha_zayavki_na_proekt",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category=category,
        forum_normalized=forum,
        response_profile=profile,
        questions=[question],
    )
    source = _chunk("yonote_api_g4yfzssrsd_s0001_obschaya_informaciya")
    if metadata_update:
        source = source.model_copy(
            update={"metadata": {**source.metadata, **metadata_update}}
        )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
            request_text=query,
        )
        is None
    )


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
async def test_first_season_grant_application_deterministic_path_skips_llm() -> None:
    query = "Как подать заявку на первый сезон Росмолодёжь.Гранты?"
    chunk_id = "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm"
    llm = ForbiddenLLM()

    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": llm,
        }
    )
    analysis = analyzed["analysis"]

    assert analyzed["analyzer_mode"] == "deterministic"
    assert analysis.complexity == Complexity.COMPLEX
    assert analysis.category == "гранты"
    assert analysis.forum_normalized is None
    assert [question.topic for question in analysis.questions] == [
        "podacha_zayavki_na_proekt"
    ]

    result = await generate(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analysis,
            "reranked_chunks": [_chunk(chunk_id)],
            "max_confidence": 0.98,
            "llm_client": llm,
        }
    )

    assert llm.calls == 0
    assert result.get("should_escalate") is not True
    assert result["generator_model"] == "source_chunk"
    assert result["cited_sources"] == [chunk_id]
    assert "Госуслуг" in result["generated_response"]
    assert "ФГАИС" in result["generated_response"]


@pytest.mark.parametrize(
    "metadata_update",
    [
        {"category": "форумы"},
        {"forum_normalized": "Росмолодёжь.Гранты 2 сезон"},
        {"source": "yonote_backup"},
    ],
    ids=("wrong-category", "wrong-season-forum", "wrong-source"),
)
def test_first_season_grant_application_alias_rejects_wrong_source_scope(
    metadata_update: dict[str, str],
) -> None:
    query = "Как подать заявку на первый сезон Росмолодёжь.Гранты?"
    question = Question(
        text="Как подать заявку?",
        category="гранты",
        topic="podacha_zayavki_na_proekt",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="гранты",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )
    source = _chunk("yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm")
    source = source.model_copy(
        update={"metadata": {**source.metadata, **metadata_update}}
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[source],
            response_limit=900,
            request_text=query,
        )
        is None
    )


@pytest.mark.parametrize(
    ("category", "profile"),
    [
        ("форумы", ResponseProfileName.APPLICATION),
        ("гранты", ResponseProfileName.GENERIC),
    ],
    ids=("non-grant", "non-application"),
)
def test_first_season_grant_application_alias_requires_grant_application_contract(
    category: str,
    profile: ResponseProfileName,
) -> None:
    query = "Как подать заявку на первый сезон Росмолодёжь.Гранты?"
    question = Question(
        text="Как подать заявку?",
        category=category,
        topic="podacha_zayavki_na_proekt",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category=category,
        response_profile=profile,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[_chunk("yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm")],
            response_limit=900,
            request_text=query,
        )
        is None
    )


@pytest.mark.parametrize(
    "query",
    [
        "Как подать заявку на второй сезон Росмолодёжь.Гранты?",
        "Как подать заявку не на первый сезон Росмолодёжь.Гранты?",
        "Как подать заявку на первый или второй сезон Росмолодёжь.Гранты?",
        "Как подать заявку на первый сезон Росмолодёжь.Гранты или на второй?",
        (
            "Как подать заявку на первый сезон Росмолодёжь.Гранты, "
            "если проект относится ко 2-му сезону?"
        ),
    ],
    ids=(
        "second-only",
        "negated-first",
        "first-or-second",
        "first-season-or-implied-second",
        "first-and-numeric-second",
    ),
)
def test_first_season_grant_application_alias_requires_unambiguous_first_season(
    query: str,
) -> None:
    question = Question(
        text="Как подать заявку?",
        category="гранты",
        topic="podacha_zayavki_na_proekt",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="гранты",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )

    assert (
        _bounded_published_source_result(
            analysis=analysis,
            questions=[question],
            source_chunks=[_chunk("yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm")],
            response_limit=900,
            request_text=query,
        )
        is None
    )


@pytest.mark.asyncio
async def test_first_season_fast_path_uses_current_message_not_contextual_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_message = "Как подать заявку на второй сезон Росмолодёжь.Гранты?"
    contextual_message = (
        "Пользователь: Как подать заявку на первый сезон Росмолодёжь.Гранты?\n"
        f"Пользователь: {current_message}"
    )
    question = Question(
        text="Как подать заявку?",
        category="гранты",
        topic="podacha_zayavki_na_proekt",
    )
    analysis = QueryAnalysis(
        complexity=Complexity.COMPLEX,
        category="гранты",
        response_profile=ResponseProfileName.APPLICATION,
        questions=[question],
    )
    observed_request_texts: list[str] = []

    def capture_bounded_request(**kwargs: object) -> dict[str, object]:
        observed_request_texts.append(str(kwargs.get("request_text") or ""))
        return {
            "generated_response": "Проверочный ответ.",
            "generator_model": "source_chunk",
            "cited_sources": ["yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm"],
        }

    monkeypatch.setattr(
        generate_node,
        "_bounded_published_source_result",
        capture_bounded_request,
    )

    result = await _generate_with_llm_or_source_fallback(
        state={
            "message": current_message,
            "message_masked": current_message,
            "contextual_message": contextual_message,
            "analysis": analysis,
            "reranked_chunks": [
                _chunk("yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm")
            ],
            "max_confidence": 0.98,
            "llm_client": ForbiddenLLM(),
        },
        analysis=analysis,
        questions=[question],
        source_chunks=[_chunk("yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm")],
        started_at=perf_counter(),
    )

    assert result["generator_model"] == "source_chunk"
    assert observed_request_texts == [current_message]


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
