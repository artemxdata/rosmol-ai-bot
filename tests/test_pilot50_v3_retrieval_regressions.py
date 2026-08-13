from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.rerank import rerank
from src.graph.nodes.retrieve import retrieve
from src.graph.query_normalization import (
    INACTIVE_PLATFORM_APPLICATION_BUTTON,
    bounded_query_intent,
    expand_query_aliases,
)
from src.graph.question_utils import (
    build_query_proven_topic_plan,
    split_explicit_request_clauses,
)
from src.kb.forum_registry import canonicalize_forum_name
from src.models import Chunk, QueryAnalysis, Question
from src.rag.seed_retriever import SeedRetriever

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "data/knowledge_base_seed.json").read_text(encoding="utf-8"))


class _ForbiddenLLM:
    async def generate(self, **_kwargs: object) -> str:
        raise AssertionError("Pilot50 retrieval regression must use deterministic analysis")


class _ForbiddenReranker:
    def rerank(self, *_args: object, **_kwargs: object):
        raise AssertionError("trusted exact-topic selection must not invoke the ML reranker")


class _AsyncSeedRetriever:
    def __init__(self) -> None:
        self._retriever = SeedRetriever(SEED)
        self.metadata_filters: list[dict] = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        return self._retriever.retrieve(query, filters, top_k)

    async def retrieve_by_metadata(self, filters: dict, top_k: int):
        self.metadata_filters.append(dict(filters))
        chunks = []
        for record in self._retriever.records:
            if any(
                filters.get(key) is not None
                and (
                    record.get(key) not in filters[key]
                    if isinstance(filters[key], list)
                    else record.get(key) != filters[key]
                )
                for key in ("forum_normalized", "category", "topic", "source_type")
            ):
                continue
            chunks.extend(self._retriever.retrieve(record["text_clean"], filters, top_k=1))
            if len(chunks) >= top_k:
                break
        return chunks[:top_k]

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
        del scan_limit, min_score
        return self._retriever.retrieve(
            query,
            {**filters, "source_type": source_type},
            top_k,
        )


def test_query_proven_plan_is_one_complete_three_aspect_contract() -> None:
    query = (
        "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
        "и что значит статус «Одобрена»?"
    )

    plan = build_query_proven_topic_plan(
        QueryAnalysis(category="платформа_фгаис"),
        query,
    )

    assert plan.incomplete is False
    assert len(plan.clauses) == 3
    assert tuple(question.topic for question in plan.questions) == (
        "registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
        "poisk_i_navigaciya_po_meropriyatiyam",
        "statusy_zayavok",
    )


def test_query_proven_plan_never_returns_known_partial_aspects() -> None:
    query = (
        "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
        "и можно ли включить двухфакторную аутентификацию?"
    )

    plan = build_query_proven_topic_plan(
        QueryAnalysis(category="платформа_фгаис"),
        query,
    )

    assert plan.incomplete is True
    assert plan.questions == ()
    assert len(plan.clauses) == 3


def test_physical_grants_plan_keeps_scope_out_of_fact_binding_question() -> None:
    plan = build_query_proven_topic_plan(
        QueryAnalysis(category="гранты"),
        "Что такое гранты для физических лиц?",
    )

    assert plan.incomplete is False
    assert len(plan.questions) == 1
    assert plan.questions[0].text == "Каковы цель и участники конкурса?"
    assert plan.questions[0].forum_normalized == "Гранты для физических лиц"


def test_query_proven_shift_plan_keeps_age_conditioned_questions_generic() -> None:
    analysis = QueryAnalysis(
        category="форумы",
        forum_normalized="Машук",
        questions=[
            Question(
                text="Когда смена для участников 14–17 лет?",
                category="форумы",
                forum_normalized="Машук",
                topic="dates_by_age",
            ),
            Question(
                text="Когда смена для участников 18–35 лет?",
                category="форумы",
                forum_normalized="Машук",
                topic="dates_age_18_35",
            ),
        ],
    )

    plan = build_query_proven_topic_plan(
        analysis,
        "Когда проходит первая смена Машука для двух возрастных групп?",
    )

    assert plan.incomplete is True
    assert plan.questions == ()


@pytest.mark.parametrize(
    ("query", "expected_keys"),
    [
        (
            "Патриот Экспо: кто именно может участвовать и когда крайний срок заявки?",
            {"application_deadline", "event_eligibility"},
        ),
        (
            "Машук выпускников: когда старт и финиш у первой смены и "
            "когда заканчивается вторая?",
            {"shift_1_dates", "shift_2_dates"},
        ),
    ],
)
def test_query_proven_plan_keeps_coordinated_temporal_questions_distinct(
    query: str,
    expected_keys: set[str],
) -> None:
    forum = "Патриот Экспо" if query.startswith("Патриот") else "Машук"
    plan = build_query_proven_topic_plan(
        QueryAnalysis(category="форумы", forum_normalized=forum),
        query,
    )

    assert plan.incomplete is False
    assert {aspect.key for aspect in plan.source_aspects} == expected_keys
    assert len(plan.questions) == len(expected_keys)


def test_conditional_followup_remains_one_explicit_request_clause() -> None:
    analysis = QueryAnalysis(category="форумы", forum_normalized="Амур")

    assert split_explicit_request_clauses(
        analysis,
        "А что делать, если я подтвердил участие, но теперь не могу поехать?",
    ) == ["что делать, если я подтвердил участие, теперь не могу поехать"]


def test_technical_symptom_stays_with_generic_action_request() -> None:
    analysis = QueryAnalysis(category="техподдержка")

    assert split_explicit_request_clauses(
        analysis,
        "Какого хуя не грузится ФГАИС, что мне делать?",
    ) == ["какого хуя не грузится фгаис. что мне делать"]


@pytest.mark.asyncio
async def test_retrieve_consumes_the_same_complete_query_proven_plan() -> None:
    query = (
        "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
        "и что значит статус «Одобрена»?"
    )
    retriever = _AsyncSeedRetriever()

    result = await retrieve(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": "Раньше обсуждали форум. " + query,
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "retriever": retriever,
        }
    )

    expected_topics = {
        "registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
        "poisk_i_navigaciya_po_meropriyatiyam",
        "statusy_zayavok",
    }
    assert expected_topics <= {
        str((chunk.metadata or {}).get("topic") or "")
        for chunk in result["retrieved_chunks"]
    }
    assert expected_topics <= {
        str(filters.get("topic") or "")
        for filters in retriever.metadata_filters
    }


@pytest.mark.asyncio
async def test_query_proven_alternative_topic_is_shared_with_rerank() -> None:
    query = "По «Ладоге» до какого числа можно подать заявку?"
    source = _seed_chunk("yonote_api_irwwd4t2v8_s0006_forum", score=0.01)

    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Ладога",
            ),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": [source],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert [chunk.chunk_id for chunk in result["reranked_chunks"]] == [
        source.chunk_id
    ]


@pytest.mark.asyncio
async def test_query_proven_fast_path_rejects_non_published_source() -> None:
    query = "По «Ладоге» до какого числа можно подать заявку?"
    source = _seed_chunk("yonote_api_irwwd4t2v8_s0006_forum", score=0.01)
    source.metadata["status"] = "draft"

    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Ладога",
            ),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": [source],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["escalation_reason"] == "rerank_failed"


@pytest.mark.asyncio
async def test_query_proven_shift_pattern_rejects_wrong_ordinal_in_rerank() -> None:
    query = "Назови период второй смены форума «Машук»."
    first = _seed_chunk(
        "yonote_api_pmbmqm6lug_s0002_1_smena_8_15_avgusta",
        score=0.01,
    )

    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Машук",
            ),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": [first],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["escalation_reason"] == "rerank_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_chunk_id"),
    [
        (
            "Какого хрена кнопка “Подать заявку” во ФГАИС неактивна?",
            "yonote_api_u7b5sscrri_s0012_registraciya_na_municipalnoe_meropriyatie",
        ),
        (
            "Блин, куда подать заявку на “Ладогу” и до какого срока?",
            "yonote_api_irwwd4t2v8_s0006_forum",
        ),
    ],
)
async def test_pilot50_v3_retrieval_recalls_exact_published_qrel(
    query: str,
    expected_chunk_id: str,
) -> None:
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": _ForbiddenLLM(),
        }
    )
    if "Ладогу" in query:
        assert analyzed["analysis"].needs_clarification is False
        assert analyzed["analysis"].forum_normalized == "Ладога"
    retriever = _AsyncSeedRetriever()
    result = await retrieve(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "retriever": retriever,
        }
    )

    assert expected_chunk_id in {
        chunk.chunk_id for chunk in result["retrieved_chunks"]
    }, {
        "analysis": analyzed["analysis"].model_dump(mode="json"),
        "metadata_filter": result["metadata_filter"],
        "retrieved": [chunk.chunk_id for chunk in result["retrieved_chunks"]],
    }


@pytest.mark.asyncio
async def test_grant_directions_uses_query_proven_metadata_lookup() -> None:
    query = "Какие направления есть в конкурсах Росмолодёжь.Гранты?"
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": _ForbiddenLLM(),
        }
    )
    retriever = _AsyncSeedRetriever()

    result = await retrieve(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "retriever": retriever,
        }
    )

    assert {
        chunk.chunk_id for chunk in result["retrieved_chunks"]
    } >= {"yonote_api_rbibn8s2s7_s0008_nominacii_grantovyh_konkursov"}
    assert any(
        filters.get("topic") == "nominacii_grantovyh_konkursov"
        and filters.get("category") == "гранты"
        and filters.get("source_type") == "yonote"
        for filters in retriever.metadata_filters
    )


@pytest.mark.asyncio
async def test_multi_aspect_grant_request_retrieves_every_proven_topic() -> None:
    query = (
        "По грантам первого сезона: что такое номинация, "
        "сколько их стандартно и какие основные шаги подачи?"
    )
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": _ForbiddenLLM(),
        }
    )
    retriever = _AsyncSeedRetriever()

    result = await retrieve(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": analyzed["analysis"],
            "retriever": retriever,
        }
    )

    assert result["metadata_filter"]["category"] == "гранты"
    assert result["metadata_filter"].get("topic") is None
    expected_topics = {
        "nominacii_grantovyh_konkursov",
        "poshagovyy_algoritm",
    }
    assert expected_topics <= {
        str(filters.get("topic") or "")
        for filters in retriever.metadata_filters
    }
    assert expected_topics <= {
        str((chunk.metadata or {}).get("topic") or "")
        for chunk in result["retrieved_chunks"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Какие направления грантов и как заполнить заявку?",
        "Какие направления грантов и где скачать шаблон заявки?",
    ],
)
async def test_compound_grant_directions_keep_category_wide_recall(
    query: str,
) -> None:
    retriever = _AsyncSeedRetriever()
    result = await retrieve(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(category="гранты"),
            "retriever": retriever,
        }
    )

    assert result["metadata_filter"].get("topic") is None
    assert not any(
        filters.get("topic") == "nominacii_grantovyh_konkursov"
        for filters in retriever.metadata_filters
    )


@pytest.mark.asyncio
async def test_inactive_button_direction_uses_current_request_not_history() -> None:
    current = "Как подать заявку во ФГАИС?"
    contextual = (
        "Раньше кнопка «Подать заявку» во ФГАИС была неактивна. "
        "Сейчас: Как подать заявку во ФГАИС?"
    )
    retriever = _AsyncSeedRetriever()
    result = await retrieve(
        {
            "message": current,
            "message_masked": current,
            "contextual_message": contextual,
            "analysis": QueryAnalysis(
                category="платформа_фгаис",
                questions=[
                    Question(
                        text=current,
                        topic="podacha_zayavki_na_proekt",
                        category="платформа_фгаис",
                    )
                ],
            ),
            "retriever": retriever,
        }
    )

    inactive_topic = "registraciya_na_municipalnoe_meropriyatie"
    assert inactive_topic not in {
        (chunk.metadata or {}).get("topic")
        for chunk in result["retrieved_chunks"]
    }
    assert not any(
        filters.get("topic") == inactive_topic
        for filters in retriever.metadata_filters
    )


def _seed_chunk(chunk_id: str, *, score: float = 1.0) -> Chunk:
    record = next(item for item in SEED if item["chunk_id"] == chunk_id)
    metadata = {key: value for key, value in record.items() if key != "text_clean"}
    metadata["forum_normalized"] = canonicalize_forum_name(
        metadata.get("forum_normalized")
    )
    return Chunk(
        chunk_id=chunk_id,
        text=record["text_clean"],
        metadata=metadata,
        score=score,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "category", "forum", "expected_chunk_ids"),
    [
        (
            "Без канцелярита: где зарегистрироваться во ФГАИС и как потом "
            "найти мероприятие по региону?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС и найти мероприятие по региону?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Для работы во ФГАИС: зарегистрироваться и найти мероприятие по региону?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Почта от старого профиля ФГАИС потеряна: как перенести данные и "
            "заодно что значит статус «Одобрена»?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0006_obedinenie_akkauntov",
                "yonote_api_u7b5sscrri_s0016_statusy_zayavok",
            ),
        ),
        (
            "В двух словах, что за «Территория смыслов», когда она идёт и какие там смены?",
            "форумы",
            "Территория смыслов",
            (
                "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
                "yonote_api_zrvcb9k240_s0004_tematicheskie_smeny_foruma",
            ),
        ),
        (
            "По опубликованной инструкции первого сезона: что такое номинация, сколько их "
            "стандартно и какие основные шаги подачи?",
            "гранты",
            None,
            (
                "yonote_api_rbibn8s2s7_s0008_nominacii_grantovyh_konkursov",
                "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm",
            ),
        ),
        (
            "На Добро.РФ хочу с нуля: как создать кабинет, а потом отфильтровать мероприятие "
            "и подать заявку волонтёром?",
            "форумы",
            "Добро.РФ",
            (
                "yonote_api_jw4tdtr1pc_s0005_registraciya_s_pomoschyu_sozdaniya_kabineta",
                "yonote_api_jw4tdtr1pc_s0008_volonterskaya_pomosch",
            ),
        ),
        (
            "У «Территории смыслов» назови общий период форума и отдельно "
            "даты смены «Правда» — не перепутай.",
            "форумы",
            "Территория смыслов",
            (
                "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
                "yonote_api_zrvcb9k240_s0009_daty_26_30_iyulya_2026_goda",
            ),
        ),
        (
            "Хочу поучаствовать в гранте: как подать заявку?",
            "гранты",
            None,
            ("yonote_api_g4yfzssrsd_s0001_obschaya_informaciya",),
        ),
        (
            "Хочу поучаствовать в гранте: подать заявку с описанием идеи "
            "и календарным планом?",
            "гранты",
            None,
            ("yonote_api_g4yfzssrsd_s0001_obschaya_informaciya",),
        ),
    ],
)
async def test_query_proven_aspects_survive_rerank(
    query: str,
    category: str,
    forum: str | None,
    expected_chunk_ids: tuple[str, ...],
) -> None:
    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(
                category=category,
                forum_normalized=forum,
            ),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": [
                _seed_chunk(chunk_id, score=0.01)
                for chunk_id in reversed(expected_chunk_ids)
            ],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert tuple(
        chunk.chunk_id for chunk in result["reranked_chunks"]
    ) == expected_chunk_ids


@pytest.mark.asyncio
async def test_query_proven_aspects_compose_a_novel_three_aspect_request() -> None:
    query = (
        "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
        "и что значит статус «Одобрена»?"
    )
    expected_chunk_ids = (
        "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
        "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
        "yonote_api_u7b5sscrri_s0016_statusy_zayavok",
    )

    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": [
                _seed_chunk(chunk_id, score=0.01)
                for chunk_id in reversed(expected_chunk_ids)
            ],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert tuple(
        chunk.chunk_id for chunk in result["reranked_chunks"]
    ) == expected_chunk_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["missing", "duplicate", "unsupported"])
async def test_query_proven_aspect_plan_falls_back_when_not_fully_proven(
    failure_mode: str,
) -> None:
    registration_id = (
        "yonote_api_u7b5sscrri_"
        "s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis"
    )
    navigation_id = "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam"
    status_id = "yonote_api_u7b5sscrri_s0016_statusy_zayavok"
    query = (
        "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
        "и что значит статус «Одобрена»?"
    )
    chunks = [
        _seed_chunk(registration_id, score=0.01),
        _seed_chunk(status_id, score=0.01),
    ]
    if failure_mode == "duplicate":
        chunks.insert(1, _seed_chunk(navigation_id, score=0.01))
        chunks.append(
            _seed_chunk(status_id, score=0.01).model_copy(
                update={"chunk_id": "duplicate_status_source"}
            )
        )
    elif failure_mode == "unsupported":
        query = (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
            "и как удалить аккаунт?"
        )
        chunks[1] = _seed_chunk(navigation_id, score=0.01)

    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": chunks,
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["escalation_reason"] == "rerank_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "category", "forum", "known_chunk_ids"),
    [
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
            "и можно ли привязать Госуслуги?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Что за Территория смыслов, какие смены и есть ли медпункт?",
            "форумы",
            "Территория смыслов",
            (
                "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
                "yonote_api_zrvcb9k240_s0004_tematicheskie_smeny_foruma",
            ),
        ),
        (
            "Какие номинации, шаги подачи и как потом изменить смету?",
            "гранты",
            None,
            (
                "yonote_api_rbibn8s2s7_s0008_nominacii_grantovyh_konkursov",
                "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
            "и можно ли включить двухфакторную аутентификацию?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "По грантам первого сезона: какие номинации, шаги подачи "
            "и можно ли добавить соавтора?",
            "гранты",
            None,
            (
                "yonote_api_rbibn8s2s7_s0008_nominacii_grantovyh_konkursov",
                "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
            "и будет ли вход по биометрии?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону "
            "и включить двухфакторную аутентификацию?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Что за Территория смыслов, какие смены и нужен ли отдельный пропуск?",
            "форумы",
            "Территория смыслов",
            (
                "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
                "yonote_api_zrvcb9k240_s0004_tematicheskie_smeny_foruma",
            ),
        ),
        (
            "Во ФГАИС объясни статус «Одобрена» регистрации, "
            "и как найти мероприятие по региону?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
                "yonote_api_u7b5sscrri_s0016_statusy_zayavok",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону, "
            "а Wi-Fi там есть?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Для работы во ФГАИС: зарегистрироваться, найти мероприятие по региону "
            "и паспорт нужен?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "По грантам первого сезона: назвать номинации, перечислить шаги подачи "
            "и соавтора добавить.",
            "гранты",
            None,
            (
                "yonote_api_rbibn8s2s7_s0008_nominacii_grantovyh_konkursov",
                "yonote_api_fyxcuinesz_s0006_poshagovyy_algoritm",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону, "
            "а Wi-Fi — это условие?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Где зарегистрироваться во ФГАИС, как найти мероприятие по региону, "
            "и связь бесплатна?",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
        (
            "Для работы во ФГАИС: зарегистрироваться, найти мероприятие по региону "
            "и подключи календарь.",
            "платформа_фгаис",
            None,
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
        ),
    ],
)
async def test_mixed_known_and_novel_aspects_fall_back_to_ml(
    query: str,
    category: str,
    forum: str | None,
    known_chunk_ids: tuple[str, ...],
) -> None:
    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": query,
            "analysis": QueryAnalysis(category=category, forum_normalized=forum),
            "analyzer_mode": "deterministic",
            "retrieved_chunks": [
                _seed_chunk(chunk_id, score=0.01)
                for chunk_id in known_chunk_ids
            ],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["escalation_reason"] == "rerank_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_chunk_id", "distractor_chunk_id"),
    [
        (
            "Хочу попасть на форум ШУМ — что нужно сделать?",
            "yonote_api_zhjxnhwbyi_s0002_registraciya",
            "yonote_api_zhjxnhwbyi_s0014_registraciya",
        ),
        (
            "Как попасть на Тавриду.Арт и подать заявку?",
            "yonote_api_cljqo2rlvk_s0003_registraciya",
            "yonote_api_cljqo2rlvk_s0028_registraciya",
        ),
    ],
)
async def test_pilot50_v3_exact_event_registration_ignores_unrequested_subflow(
    query: str,
    expected_chunk_id: str,
    distractor_chunk_id: str,
) -> None:
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": _ForbiddenLLM(),
        }
    )

    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "analyzer_mode": analyzed["analyzer_mode"],
            "retrieved_chunks": [
                _seed_chunk(distractor_chunk_id),
                _seed_chunk(expected_chunk_id),
            ],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == expected_chunk_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_query", "contextual_query", "expected_chunk_id"),
    [
        (
            "Хочу попасть на форум ШУМ — что нужно сделать?",
            "Раньше я говорил, что хочу быть волонтёром на ШУМе.",
            "yonote_api_zhjxnhwbyi_s0002_registraciya",
        ),
        (
            "Как зарегистрироваться волонтёром на форум ШУМ?",
            "Раньше я спрашивал, как обычному участнику попасть на ШУМ.",
            "yonote_api_zhjxnhwbyi_s0014_registraciya",
        ),
    ],
)
async def test_current_registration_subflow_wins_over_context_history(
    current_query: str,
    contextual_query: str,
    expected_chunk_id: str,
) -> None:
    analyzed = await analyze_query(
        {
            "message": current_query,
            "message_masked": current_query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": _ForbiddenLLM(),
        }
    )
    result = await rerank(
        {
            "message": current_query,
            "message_masked": current_query,
            "contextual_message": contextual_query,
            "analysis": analyzed["analysis"],
            "analyzer_mode": analyzed["analyzer_mode"],
            "retrieved_chunks": [
                _seed_chunk("yonote_api_zhjxnhwbyi_s0014_registraciya"),
                _seed_chunk("yonote_api_zhjxnhwbyi_s0002_registraciya"),
            ],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == expected_chunk_id


@pytest.mark.parametrize(
    "query",
    [
        "Хочу попасть на форум ШУМ — что нужно сделать?",
        "Как попасть на Тавриду.Арт и подать заявку?",
    ],
)
def test_query_expansion_does_not_inject_registration_subflow_markers(
    query: str,
) -> None:
    expanded = expand_query_aliases(query).casefold().replace("ё", "е")

    assert "волонт" not in expanded
    assert "зрител" not in expanded


@pytest.mark.parametrize(
    ("query", "forum", "expected"),
    [
        (
            "Какого хрена кнопка Подать заявку во ФГАИС неактивна?",
            None,
            INACTIVE_PLATFORM_APPLICATION_BUTTON,
        ),
        ("Как подать заявку во ФГАИС?", None, None),
        ("Почему кнопка обратной связи во ФГАИС не работает?", None, None),
        (
            "Почему кнопка Подать заявку во ФГАИС неактивна на форуме Машук?",
            "Машук",
            None,
        ),
    ],
)
def test_inactive_application_button_scope_is_query_proven_only(
    query: str,
    forum: str | None,
    expected: str | None,
) -> None:
    assert bounded_query_intent(query, forum_normalized=forum) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_chunk_id", "generic_chunk_id"),
    [
        (
            "Как зарегистрироваться волонтёром на форум ШУМ?",
            "yonote_api_zhjxnhwbyi_s0014_registraciya",
            "yonote_api_zhjxnhwbyi_s0002_registraciya",
        ),
        (
            "Как зарегистрироваться зрителем на Таврида.Арт?",
            "yonote_api_cljqo2rlvk_s0028_registraciya",
            "yonote_api_cljqo2rlvk_s0003_registraciya",
        ),
    ],
)
async def test_explicit_registration_subflow_remains_selectable(
    query: str,
    expected_chunk_id: str,
    generic_chunk_id: str,
) -> None:
    analyzed = await analyze_query(
        {
            "message": query,
            "message_masked": query,
            "routing_hint": {"complexity": "complex"},
            "llm_client": _ForbiddenLLM(),
        }
    )
    result = await rerank(
        {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed["contextual_message"],
            "analysis": analyzed["analysis"],
            "analyzer_mode": analyzed["analyzer_mode"],
            "retrieved_chunks": [
                _seed_chunk(generic_chunk_id),
                _seed_chunk(expected_chunk_id),
            ],
            "reranker": _ForbiddenReranker(),
        }
    )

    assert result["reranked_chunks"][0].chunk_id == expected_chunk_id
