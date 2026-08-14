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


def test_process_words_do_not_create_forum_shift_or_children_aspects() -> None:
    status = plan_query_aspects(
        "Когда сменится статус заявки после отбора?"
    )
    details = plan_query_aspects("Где посмотреть детали статуса заявки?")

    assert status == frozenset({KnowledgeAspect.RESULTS})
    assert KnowledgeAspect.SHIFTS not in status
    assert KnowledgeAspect.STATUS in details
    assert KnowledgeAspect.CHILDREN not in details


def test_shift_is_scope_for_results_but_a_fact_for_shift_catalog() -> None:
    scoped = plan_query_aspects(
        "Когда будут результаты отбора на первую смену форума?"
    )
    catalog = plan_query_aspects("Какие смены есть у форума?")

    assert scoped == frozenset({KnowledgeAspect.RESULTS})
    assert KnowledgeAspect.SHIFTS in catalog


def test_result_or_invitation_owns_non_definition_status_request() -> None:
    results = plan_query_aspects("Когда после отбора изменится статус заявки?")
    invitation = plan_query_aspects("Когда придёт письмо-вызов и изменится статус?")
    definition = plan_query_aspects(
        "Когда будут результаты и что означает статус «Резерв»?"
    )

    assert KnowledgeAspect.STATUS not in results
    assert KnowledgeAspect.STATUS not in invitation
    assert KnowledgeAspect.STATUS in definition


def test_temporal_process_qualifiers_do_not_create_unrelated_fact_slots() -> None:
    after_selection = plan_query_aspects(
        "После окончания отбора где появится программа и когда будет трансфер?"
    )
    invitation = plan_query_aspects(
        "Я отправил заявку: когда ждать письмо с приглашением?"
    )
    trip_signal = plan_query_aspects(
        "Когда после заявки уже можно собираться в дорогу?"
    )

    assert after_selection == frozenset(
        {KnowledgeAspect.PROGRAM, KnowledgeAspect.TRANSFER}
    )
    assert invitation == frozenset({KnowledgeAspect.INVITATION})
    assert trip_signal == frozenset({KnowledgeAspect.INVITATION})


def test_selection_process_and_outcome_have_different_fact_slots() -> None:
    stages = plan_query_aspects("Из каких этапов состоит отбор на форум?")
    outcome = plan_query_aspects("Когда будет результат по моей заявке?")

    assert stages == frozenset({KnowledgeAspect.REGISTRATION})
    assert outcome == frozenset({KnowledgeAspect.RESULTS})


def test_completed_application_uses_status_while_named_shift_stays_scope() -> None:
    planned = plan_query_aspects(
        "Я уже подал заявку на смену форума — что происходит дальше?"
    )

    assert planned == frozenset({KnowledgeAspect.STATUS})


def test_grant_agreement_is_scope_for_report_deadline() -> None:
    planned = plan_query_aspects(
        "Какой срок отчётности указан в грантовом соглашении?"
    )

    assert planned == frozenset({KnowledgeAspect.GRANT_REPORT})


def test_grant_application_process_has_explicit_status_and_agreement_slots() -> None:
    review = plan_query_aspects(
        "Сколько проверяют заявку после грантового конкурса?"
    )
    agreement = plan_query_aspects(
        "Где найти приказ и договор для грантового соглашения?"
    )

    assert review == frozenset({KnowledgeAspect.STATUS})
    assert agreement == frozenset(
        {KnowledgeAspect.GRANT_AGREEMENT, KnowledgeAspect.RESULTS}
    )


def test_published_result_and_invitation_sentences_expose_their_fact_slots() -> None:
    grant_dates = _record("yonote_api_fyxcuinesz_s0001_sroki_i_daty")
    invitation = _record("yonote_api_aucookucja_s0020_rezultaty_otbora")

    assert KnowledgeAspect.RESULTS in infer_source_aspects(
        grant_dates,
        grant_dates["text_clean"],
    )
    assert KnowledgeAspect.INVITATION in infer_source_aspects(
        invitation,
        invitation["text_clean"],
    )


def test_non_housing_use_of_placed_does_not_request_accommodation() -> None:
    assert plan_query_aspects(
        "Сообщение размещено в личном кабинете."
    ) == frozenset()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Сколько времени проверяют грантовый отчёт?",
            KnowledgeAspect.GRANT_REPORT,
        ),
        (
            "Когда откроется вкладка «Отчёт» и сколько дней есть на сдачу?",
            KnowledgeAspect.GRANT_REPORT,
        ),
        (
            "Как заключить грантовое соглашение?",
            KnowledgeAspect.GRANT_AGREEMENT,
        ),
        (
            "Где публикуют приказ о победителях грантового конкурса?",
            KnowledgeAspect.RESULTS,
        ),
    ],
)
def test_grant_processes_have_explicit_query_aspects(
    query: str,
    expected: KnowledgeAspect,
) -> None:
    planned = plan_query_aspects(query)

    assert expected in planned
    assert KnowledgeAspect.SHIFTS not in planned
    assert KnowledgeAspect.CHILDREN not in planned


@pytest.mark.parametrize(
    ("chunk_id", "expected"),
    [
        (
            "yonote_api_g4yfzssrsd_s0043_4_tehnicheskaya_proverka",
            KnowledgeAspect.STATUS,
        ),
        (
            "yonote_api_g4yfzssrsd_s0053_poryadok_zaklyucheniya_soglasheniya",
            KnowledgeAspect.GRANT_AGREEMENT,
        ),
        (
            "yonote_api_g4yfzssrsd_s0070_sroki_otchetnosti",
            KnowledgeAspect.GRANT_REPORT,
        ),
    ],
)
def test_grant_process_sources_have_explicit_aspects(
    chunk_id: str,
    expected: KnowledgeAspect,
) -> None:
    record = _record(chunk_id)

    assert expected in infer_source_aspects(record, record["text_clean"])


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


@pytest.mark.parametrize(
    ("source_text", "expected"),
    [
        (
            "Проезд до места проведения оплачивается участником самостоятельно.",
            KnowledgeAspect.TRAVEL,
        ),
        (
            "Проживание и питание участников обеспечивает принимающая сторона.",
            KnowledgeAspect.ACCOMMODATION,
        ),
        ("Проживание и питание участников обеспечивает принимающая сторона.", KnowledgeAspect.FOOD),
        ("Для участников с ОВЗ предусмотрена доступная среда.", KnowledgeAspect.ACCESSIBILITY),
        ("Результаты отбора будут опубликованы в личном кабинете.", KnowledgeAspect.RESULTS),
        (
            "Чтобы отказаться от участия, отзови заявку в личном кабинете.",
            KnowledgeAspect.CANCELLATION,
        ),
    ],
)
def test_source_aspects_use_explicit_facts_inside_generic_faq_chunks(
    source_text: str,
    expected: KnowledgeAspect,
) -> None:
    aspects = infer_source_aspects({"topic": "faq"}, source_text)

    assert expected in aspects


def test_source_body_aspects_ignore_generic_domain_words() -> None:
    aspects = infer_source_aspects(
        {"topic": "faq"},
        "Организатор опубликует доступную программу для участников.",
    )

    assert KnowledgeAspect.ACCOMMODATION not in aspects
    assert KnowledgeAspect.FOOD not in aspects
    assert KnowledgeAspect.ACCESSIBILITY not in aspects
    assert KnowledgeAspect.ELIGIBILITY not in aspects


def test_fact_cards_answer_travel_from_a_generic_published_faq_chunk() -> None:
    chunk = ScoredChunk(
        chunk_id="yonote_api_generic_faq_s0001",
        text="Проезд до места проведения оплачивается участником самостоятельно.",
        metadata={
            "source_type": "yonote",
            "source": "yonote_api",
            "version": "yonote-api-v1",
            "status": "published",
            "category": "форумы",
            "forum_normalized": "Амур",
            "topic": "faq",
        },
        score=0.98,
        reranker_score=0.98,
    )

    draft = compose_fact_cards(
        "Кто оплачивает проезд на форум Амур?",
        [chunk],
        category="форумы",
        forum_normalized="Амур",
    )

    assert draft is not None
    assert draft.cited_sources == ("yonote_api_generic_faq_s0001",)
    assert "оплачивается участником самостоятельно" in draft.response


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
    ("query", "expected", "forbidden"),
    [
        (
            "Когда откроется вкладка «Отчёт»?",
            "в первый рабочий день после окончания срока реализации проекта",
            "60 рабочих дней",
        ),
        (
            "Сколько рабочих дней есть победителю 2026 года на сдачу отчётности?",
            "20 рабочих дней",
            "60 рабочих дней",
        ),
        (
            "Сколько времени проверяют грантовый отчёт?",
            "до 30 рабочих дней",
            "60 рабочих дней",
        ),
        (
            "Сколько дней дают победителю 2026 года на доработку отчёта?",
            "30 рабочих дней",
            "45 рабочих дней",
        ),
    ],
)
def test_grant_report_renderer_selects_the_requested_timeline(
    query: str,
    expected: str,
    forbidden: str,
) -> None:
    draft = compose_fact_cards(
        query,
        [_chunk("yonote_api_g4yfzssrsd_s0070_sroki_otchetnosti")],
        category="гранты",
        forum_normalized="Гранты для физических лиц",
        response_limit=900,
    )

    assert draft is not None
    assert expected in draft.response
    assert forbidden not in draft.response


def test_grant_agreement_renderer_uses_only_published_process_steps() -> None:
    draft = compose_fact_cards(
        "Как заключить грантовое соглашение?",
        [
            _chunk(
                "yonote_api_g4yfzssrsd_"
                "s0053_poryadok_zaklyucheniya_soglasheniya"
            )
        ],
        category="гранты",
        forum_normalized="Гранты для физических лиц",
        response_limit=900,
    )

    assert draft is not None
    assert "доступны три вкладки" in draft.response
    assert "сумма расходов" in draft.response
    assert "сроки реализации проекта" in draft.response


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
