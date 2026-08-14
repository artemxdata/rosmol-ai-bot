from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.graph.nodes.generate import _bounded_published_source_result
from src.graph.nodes.retrieve import _retrieve_attempt
from src.kb.aspect_catalog import topic_candidates_for_request
from src.kb.fact_cards import compose_fact_cards
from src.kb.fact_extractor import (
    KnowledgeAspect,
    aspects_are_compatible,
    extract_source_fact_excerpts,
    infer_query_aspects,
    infer_source_aspects,
    plan_query_aspects,
)
from src.models import QueryAnalysis, Question, ScoredChunk
from src.response_contract import ResponseProfileName

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "data" / "knowledge_base_seed.json").read_text(encoding="utf-8"))
SEED_BY_ID = {record["chunk_id"]: record for record in SEED}


def _record(chunk_id: str) -> dict:
    return SEED_BY_ID[chunk_id]


def _chunk(chunk_id: str) -> ScoredChunk:
    record = _record(chunk_id)
    return ScoredChunk(
        chunk_id=chunk_id,
        text=record["text_clean"],
        metadata={
            key: value
            for key, value in record.items()
            if key not in {"text_clean", "text_raw"}
        },
        score=0.98,
        reranker_score=0.98,
    )


def test_query_aspects_do_not_confuse_word_substrings() -> None:
    application = infer_query_aspects("Как подать заявку на форум?")
    navigation = infer_query_aspects("Где найти доступные мероприятия?")
    program = infer_query_aspects("Когда будет программа форума?")

    assert KnowledgeAspect.REGISTRATION in application
    assert KnowledgeAspect.DATES not in application
    assert navigation == frozenset({KnowledgeAspect.NAVIGATION})
    assert KnowledgeAspect.PROGRAM in program
    assert KnowledgeAspect.CHILDREN not in program


def test_as_of_registration_clause_creates_date_and_registration_slots() -> None:
    planned = plan_query_aspects(
        "По состоянию на 14 августа 2026 года приём уже закрыт?"
    )

    assert planned == frozenset(
        {KnowledgeAspect.DATES, KnowledgeAspect.REGISTRATION}
    )


def test_exact_forum_topic_does_not_pollute_program_as_registration() -> None:
    record = _record("yonote_api_pmbmqm6lug_s0013_programma_foruma")

    aspects = infer_source_aspects(record, record["text_clean"])

    assert KnowledgeAspect.PROGRAM in aspects
    assert KnowledgeAspect.REGISTRATION not in aspects


def test_aspect_bridge_rejects_other_fact_and_shift_ordinal() -> None:
    results = _record("yonote_api_pmbmqm6lug_s0009_rezultaty")
    first_shift = _record("yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta")

    assert not aspects_are_compatible(
        "Когда будет программа форума?",
        results,
        results["text_clean"],
    )
    assert not aspects_are_compatible(
        "Когда проходит вторая смена?",
        first_shift,
        first_shift["text_clean"],
    )


def test_catalog_resolves_current_topics_without_forum_specific_aliases() -> None:
    platform_topics = topic_candidates_for_request(
        "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
        category="платформа_фгаис",
    )
    mashuk_topics = topic_candidates_for_request(
        "Когда проходит вторая смена форума Машук?",
        category="форумы",
        forum_normalized="Машук",
    )

    assert (
        "registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis"
        in platform_topics
    )
    assert "2_smena_15_22_avgusta" in mashuk_topics
    assert "1_smena_8_15_avgusta" not in mashuk_topics


def test_catalog_links_named_shift_to_its_following_date_card() -> None:
    topics = topic_candidates_for_request(
        "Когда проходила смена «Правда» форума «Территория смыслов»?",
        category="форумы",
        forum_normalized="Территория смыслов",
    )

    assert topics[0] == "daty_26_30_iyulya_2026_goda"


class _CatalogMetadataRetriever:
    def __init__(self) -> None:
        self.filters: list[dict] = []

    async def retrieve_by_metadata(self, filters: dict, top_k: int) -> list[ScoredChunk]:
        self.filters.append(filters)
        topics = filters.get("topic")
        topic_values = topics if isinstance(topics, list) else [topics]
        expected = "registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis"
        if expected not in topic_values:
            return []
        return [_chunk(f"yonote_api_u7b5sscrri_s0002_{expected}")]


@pytest.mark.asyncio
async def test_metadata_retrieval_uses_catalog_after_stale_analyzer_topic() -> None:
    retriever = _CatalogMetadataRetriever()

    chunks, used_metadata = await _retrieve_attempt(
        retriever,
        "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
        {
            "category": "платформа_фгаис",
            "source_type": "yonote",
            "topic": "kak_zaregistrirovatsya_na_fgais",
        },
        top_k=10,
        current_message="Как зарегистрироваться во ФГАИС «Молодёжь России»?",
    )

    assert used_metadata is True
    assert [chunk.chunk_id for chunk in chunks] == [
        "yonote_api_u7b5sscrri_"
        "s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis"
    ]
    assert len(retriever.filters) == 2


@pytest.mark.parametrize(
    ("chunk_id", "query", "expected"),
    [
        (
            "yonote_api_u7b5sscrri_s0007_udalenie_akkaunta",
            "Может ли техподдержка удалить мой аккаунт ФГАИС за меня?",
            ("только сам пользователь", "не имеем права удалять аккаунты"),
        ),
        (
            "yonote_api_rbibn8s2s7_s0008_nominacii_grantovyh_konkursov",
            "Что такое номинация и сколько стандартных номинаций?",
            ("Номинация - это тематика проекта", "18 стандартных номинаций"),
        ),
        (
            "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
            "Что такое форум «Территория смыслов»?",
            ("главная общественно-политическая площадка",),
        ),
        (
            "yonote_api_zhjxnhwbyi_s0002_registraciya",
            "Как попасть на форум ШУМ и подать заявку?",
            ("Регистрация для граждан РФ", "https://myrosmol.ru/events/"),
        ),
    ],
)
def test_generic_fact_extractor_uses_published_source_without_event_rules(
    chunk_id: str,
    query: str,
    expected: tuple[str, ...],
) -> None:
    record = _record(chunk_id)

    excerpts = extract_source_fact_excerpts(
        record["text_clean"],
        query,
        record,
    )
    answer = " ".join(excerpts)

    assert excerpts
    assert all(fragment in answer for fragment in expected)


def test_bounded_generator_uses_aspect_bridge_for_renamed_platform_heading() -> None:
    query = "Как зарегистрироваться во ФГАИС «Молодёжь России»?"
    question = Question(
        text=query,
        category="платформа_фгаис",
        topic="kak_zaregistrirovatsya_na_fgais",
    )
    analysis = QueryAnalysis(
        category="платформа_фгаис",
        questions=[question],
        response_profile=ResponseProfileName.APPLICATION,
    )

    result = _bounded_published_source_result(
        analysis=analysis,
        questions=[question],
        source_chunks=[
            _chunk(
                "yonote_api_u7b5sscrri_"
                "s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis"
            )
        ],
        response_limit=450,
        request_text=query,
    )

    assert result is not None
    assert result["generator_model"] == "source_chunk"
    assert "https://myrosmol.ru/auth/register" in result["generated_response"]


def test_unknown_topic_does_not_get_a_deterministic_fact_answer() -> None:
    assert (
        extract_source_fact_excerpts(
            "Неподтверждённая дополнительная тема.",
            "Какие есть подробности?",
            {"topic": "unsupported_topic"},
        )
        == []
    )


def test_fact_cards_choose_the_requested_registration_subflow() -> None:
    shum = compose_fact_cards(
        "Хочу попасть на форум ШУМ — что нужно сделать?",
        [
            _chunk("yonote_api_zhjxnhwbyi_s0014_registraciya"),
            _chunk("yonote_api_zhjxnhwbyi_s0002_registraciya"),
        ],
        category="форумы",
        forum_normalized="ШУМ",
    )
    dobro = compose_fact_cards(
        "Как зарегистрироваться на Добро.РФ через новый кабинет?",
        [
            _chunk("yonote_api_jw4tdtr1pc_s0008_volonterskaya_pomosch"),
            _chunk(
                "yonote_api_jw4tdtr1pc_"
                "s0005_registraciya_s_pomoschyu_sozdaniya_kabineta"
            ),
        ],
        category="форумы",
        forum_normalized="Добро.РФ",
    )

    assert shum is not None
    assert shum.cited_sources == ("yonote_api_zhjxnhwbyi_s0002_registraciya",)
    assert dobro is not None
    assert dobro.cited_sources == (
        "yonote_api_jw4tdtr1pc_"
        "s0005_registraciya_s_pomoschyu_sozdaniya_kabineta",
    )


def test_fact_cards_choose_registration_deadline_over_later_workflow_date() -> None:
    draft = compose_fact_cards(
        "Можно ли было подать заявку на форум «Ладога» по состоянию на "
        "14 августа 2026 года и до какой даты принимали заявки?",
        [
            _chunk("yonote_api_irwwd4t2v8_s0010_etapy_provedeniya"),
            _chunk("yonote_api_irwwd4t2v8_s0006_forum"),
            _chunk(
                "yonote_api_irwwd4t2v8_"
                "s0004_predstaviteley_obrazovatelnyh_uchrezhdeniy_roditelskih_soobs"
            ),
        ],
        category="форумы",
        forum_normalized="Ладога",
    )

    assert draft is not None
    assert draft.cited_sources == ("yonote_api_irwwd4t2v8_s0006_forum",)
    assert "30 июня 2026 года" in draft.response


def test_fact_cards_fail_closed_for_indistinguishable_duplicate_sources() -> None:
    source = _chunk("yonote_api_u7b5sscrri_s0016_statusy_zayavok")
    duplicate = source.model_copy(update={"chunk_id": "duplicate_status_source"})

    draft = compose_fact_cards(
        "Что значит статус «Одобрена»?",
        [source, duplicate],
        category="платформа_фгаис",
    )

    assert draft is None


def test_fact_cards_render_eligibility_bounds_as_ranges_not_bare_ages() -> None:
    draft = compose_fact_cards(
        "Премия «Патриот»: кто вообще может участвовать?",
        [_chunk("yonote_api_tnorqqrmvg_s0003_uchastniki")],
        category="форумы",
        forum_normalized="Премия «Патриот»",
    )

    assert draft is not None
    assert "Гражданин: минимальный возраст — 18 лет" in draft.response
    assert "Гражданин: максимальный возраст — 35 лет" in draft.response
    assert "Представители: минимальный возраст — 18 лет" in draft.response
    assert "Представители: максимальный возраст — 55 лет" in draft.response
