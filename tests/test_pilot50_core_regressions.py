from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.graph.nodes.rerank import _priority_source_candidate, rerank
from src.graph.nodes.retrieve import _has_multi_aspect_message, retrieve
from src.graph.query_normalization import (
    ACCOUNT_DATA_RECOVERY,
    FORUM_DISCOVERY,
    GENERIC_PLATFORM_REGISTRATION,
    GRANT_DIRECTIONS,
    PHYSICAL_GRANTS_OVERVIEW,
    PLATFORM_EVENT_NAVIGATION,
    bounded_query_intent,
)
from src.models import Chunk, QueryAnalysis, Question


class _NeverReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int):
        raise AssertionError("exact query entity and intent must select the published source")


class _EmptyReranker:
    def rerank(self, query: str, chunks: list[Chunk], top_k: int):
        assert chunks == []
        return []


class _RecordingRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, int]] = []

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        return []


class _ScopedRecallRetriever:
    def __init__(
        self,
        *,
        metadata_chunks: list[Chunk],
        semantic_chunks: list[Chunk],
    ) -> None:
        self.metadata_chunks = metadata_chunks
        self.semantic_chunks = semantic_chunks
        self.metadata_calls: list[tuple[dict, int]] = []
        self.calls: list[tuple[str, dict, int]] = []

    async def retrieve_by_metadata(self, filters: dict, top_k: int):
        self.metadata_calls.append((filters, top_k))
        return self.metadata_chunks

    async def retrieve(self, query: str, filters: dict, top_k: int):
        self.calls.append((query, filters, top_k))
        return self.semantic_chunks


@pytest.fixture(scope="module")
def published_yonote_seed() -> dict[str, dict]:
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "knowledge_base_seed.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        str(row["chunk_id"]): row
        for row in rows
        if row.get("status") == "published" and row.get("source_type") == "yonote"
    }


def _yonote_chunk(
    chunk_id: str,
    text: str,
    *,
    category: str,
    topic: str,
    forum: str | None = None,
    intent_name: str | None = None,
    score: float = 0.1,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        metadata={
            "source_type": "yonote",
            "category": category,
            "topic": topic,
            "forum_normalized": forum,
            "source_category": forum,
            "intent_name": intent_name or topic,
        },
        score=score,
    )


def _published_seed_chunk(
    seed: dict[str, dict],
    chunk_id: str,
) -> Chunk:
    row = seed[chunk_id]
    return Chunk(
        chunk_id=chunk_id,
        text=str(row["text_clean"]),
        metadata=dict(row),
        score=1.0,
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
            GENERIC_PLATFORM_REGISTRATION,
        ),
        ("Где во ФГАИС найти доступные мероприятия?", PLATFORM_EVENT_NAVIGATION),
        (
            "Потерял доступ к почте от профиля ФГАИС, как вернуть данные аккаунта?",
            ACCOUNT_DATA_RECOVERY,
        ),
        ("Какие направления есть в конкурсах Росмолодёжь.Гранты?", GRANT_DIRECTIONS),
        ("Что такое гранты для физических лиц?", PHYSICAL_GRANTS_OVERVIEW),
        ("Какие форумы сейчас есть и где их найти?", FORUM_DISCOVERY),
    ],
)
def test_bounded_query_intents_have_one_shared_classifier(
    query: str,
    expected: str,
) -> None:
    assert bounded_query_intent(query) == expected


@pytest.mark.parametrize(
    ("query", "forum"),
    [
        (
            "Как зарегистрироваться во ФГАИС и где найти доступные мероприятия?",
            None,
        ),
        (
            "Что такое гранты для физлиц и какие направления есть?",
            None,
        ),
        (
            "Что такое гранты для физических лиц и какие направления есть?",
            "Гранты для физических лиц",
        ),
    ],
)
def test_multi_intent_query_is_not_narrowed_to_one_bounded_source(
    query: str,
    forum: str | None,
) -> None:
    assert bounded_query_intent(query, forum_normalized=forum) is None


@pytest.mark.parametrize(
    ("query", "forum"),
    [
        ("Как зарегистрироваться на форум Амур во ФГАИС?", None),
        ("Как зарегистрироваться на мероприятие во ФГАИС?", None),
        ("Как зарегистрироваться на событие во ФГАИС?", None),
        ("Как зарегистрироваться во ФГАИС для участия?", "Амур"),
    ],
)
def test_event_bound_registration_is_not_generic_platform_registration(
    query: str,
    forum: str | None,
) -> None:
    assert bounded_query_intent(query, forum_normalized=forum) is None


@pytest.mark.parametrize(
    "query",
    [
        "Где найти документы форума Машук?",
        "Где жить на форуме Машук?",
    ],
)
def test_forum_specific_fact_is_not_generic_forum_discovery(query: str) -> None:
    assert bounded_query_intent(query, forum_normalized="Машук") is None


@pytest.mark.parametrize(
    ("query", "forum"),
    [
        ("Какие направления грантов есть на Машуке?", "Машук"),
        ("Что такое гранты для физических лиц на Машуке?", "Машук"),
        ("Какие форумы сейчас есть и где их найти?", "Машук"),
    ],
)
def test_named_event_scope_is_not_replaced_by_generic_intent(
    query: str,
    forum: str,
) -> None:
    assert bounded_query_intent(query, forum_normalized=forum) is None


def test_generic_grant_product_scopes_keep_bounded_intents() -> None:
    assert (
        bounded_query_intent(
            "Какие направления есть в конкурсах Росмолодёжь.Гранты?",
            forum_normalized="Росмолодёжь.Гранты",
        )
        == GRANT_DIRECTIONS
    )
    assert (
        bounded_query_intent(
            "Что такое гранты для физических лиц?",
            forum_normalized="Гранты для физических лиц",
        )
        == PHYSICAL_GRANTS_OVERVIEW
    )


@pytest.mark.parametrize(
    "query",
    [
        (
            "Без канцелярита: где зарегистрироваться во ФГАИС и как потом "
            "найти мероприятие по региону?"
        ),
        (
            "Почта от старого профиля ФГАИС потеряна: как перенести данные и "
            "заодно что значит статус «Одобрена»?"
        ),
        (
            "По «Ладоге» сразу три вещи: до какого числа заявка, кто платит "
            "за проживание с едой и могут ли компенсировать дорогу?"
        ),
        "Премия «Патриот»: кто вообще может участвовать и когда крайний срок подачи?",
        "В двух словах, что за «Территория смыслов», когда она идёт и какие там смены?",
        (
            "По опубликованной инструкции первого сезона: что такое номинация, "
            "сколько их стандартно и какие основные шаги подачи?"
        ),
        (
            "Не смешивай этапы: сколько проверяют проект грантового соглашения "
            "и сколько — уже итоговый отчёт?"
        ),
        (
            "На Добро.РФ хочу с нуля: как создать кабинет, а потом отфильтровать "
            "мероприятие и подать заявку волонтёром?"
        ),
        "По «Машуку» без догадок: когда объявят результаты отбора и когда дадут программу?",
        "Сверь календарь «Машука»: какие даты у первой и второй смен и когда разъезд каждой?",
        (
            "У «Территории смыслов» назови общий период форума и отдельно даты "
            "смены «Правда» — не перепутай."
        ),
    ],
)
def test_pilot50_v2_compound_queries_require_shared_scoped_recall(query: str) -> None:
    assert _has_multi_aspect_message(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "Когда будут результаты отбора?",
        "Как зарегистрироваться во ФГАИС?",
        "Какие смены будут на форуме?",
    ],
)
def test_single_aspect_queries_do_not_trigger_compound_shared_recall(query: str) -> None:
    assert _has_multi_aspect_message(query) is False


@pytest.fixture
def _rerank_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.rerank.get_settings",
        lambda: SimpleNamespace(
            ml_unload_after_use=False,
            ml_unload_embedder_after_use=False,
            ml_unload_reranker_after_use=False,
            reranker_threshold_low=0.4,
            reranker_threshold_high=0.7,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "analysis", "expected", "correct", "distractor"),
    [
        (
            "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
            QueryAnalysis(category="форумы"),
            "fgais_registration",
            _yonote_chunk(
                "fgais_registration",
                "Регистрация проходит по ссылке https://myrosmol.ru/auth/register",
                category="платформа_фгаис",
                topic="registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
            ),
            _yonote_chunk(
                "event_registration",
                "Регистрация на конкретное мероприятие",
                category="форумы",
                topic="registraciya",
                forum="Ладога",
                score=0.99,
            ),
        ),
        (
            "Где во ФГАИС найти доступные мероприятия?",
            QueryAnalysis(category="форумы"),
            "fgais_event_navigation",
            _yonote_chunk(
                "fgais_event_navigation",
                "Поиск и навигация по мероприятиям через фильтры личного кабинета",
                category="платформа_фгаис",
                topic="poisk_i_navigaciya_po_meropriyatiyam",
            ),
            _yonote_chunk(
                "platform_description",
                "Общее описание платформы",
                category="платформа_фгаис",
                topic="opisanie",
                score=0.99,
            ),
        ),
        (
            "Какие направления есть в конкурсах Росмолодёжь.Гранты?",
            QueryAnalysis(category="гранты"),
            "grant_nominations",
            _yonote_chunk(
                "grant_nominations",
                "Номинации грантовых конкурсов — это тематики проектов",
                category="гранты",
                topic="nominacii_grantovyh_konkursov",
                forum="Росмолодёжь.Гранты",
            ),
            _yonote_chunk(
                "grant_contacts",
                "Контакты отделов Росмолодёжь.Гранты",
                category="гранты",
                topic="kontakty_otdelov_rosmolodezh_granty",
                forum="Гранты для физических лиц",
                score=0.99,
            ),
        ),
        (
            "Что такое гранты для физических лиц?",
            QueryAnalysis(category="гранты"),
            "physical_grants_overview",
            _yonote_chunk(
                "physical_grants_overview",
                "Общая информация о конкурсе грантов для физических лиц",
                category="гранты",
                topic="obschaya_informaciya",
                forum="Гранты для физических лиц",
            ),
            _yonote_chunk(
                "grant_contacts",
                "Контакты отделов Росмолодёжь.Гранты",
                category="гранты",
                topic="kontakty_otdelov_rosmolodezh_granty",
                forum="Гранты для физических лиц",
                score=0.99,
            ),
        ),
        (
            "Я потерял доступ к почте от профиля ФГАИС. Как восстановить доступ к данным?",
            QueryAnalysis(category="техподдержка", is_technical=True),
            "account_merge",
            _yonote_chunk(
                "account_merge",
                "Объединение аккаунтов и перенос данных из старого кабинета",
                category="платформа_фгаис",
                topic="obedinenie_akkauntov",
            ),
            _yonote_chunk(
                "password_reset",
                "Восстановление пароля",
                category="платформа_фгаис",
                topic="vosstanovit_parol",
                score=0.99,
            ),
        ),
        (
            "Чё по форумам вообще сейчас есть?",
            QueryAnalysis(category="общее"),
            "forum_catalog",
            _yonote_chunk(
                "forum_catalog",
                "Ссылка https://events.myrosmol.ru/ на каталог мероприятий Форумной дирекции",
                category="общее",
                topic="ssylka_https_events_myrosmol_ru",
            ),
            _yonote_chunk(
                "specific_forum",
                "Описание одного форума",
                category="форумы",
                topic="opisanie",
                forum="Ладога",
                score=0.99,
            ),
        ),
    ],
)
async def test_pilot50_query_scope_selects_exact_published_source(
    _rerank_settings: None,
    query: str,
    analysis: QueryAnalysis,
    expected: str,
    correct: Chunk,
    distractor: Chunk,
) -> None:
    result = await rerank(
        {
            "message_masked": query,
            "analysis": analysis,
            "retrieved_chunks": [distractor, correct],
            "reranker": _NeverReranker(),
        }
    )

    assert result.get("should_escalate") is not True
    assert result["reranked_chunks"][0].chunk_id == expected
    assert result["max_confidence"] >= 0.7


@pytest.mark.asyncio
async def test_retrieve_overrides_misclassified_generic_fgais_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.retrieve.get_settings",
        lambda: SimpleNamespace(retrieval_strict_forum_stop_min_chunks=2),
    )
    retriever = _RecordingRetriever()
    result = await retrieve(
        {
            "message_masked": "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
            "analysis": QueryAnalysis(category="форумы"),
            "retriever": retriever,
        }
    )

    assert retriever.calls
    first_query, first_filters, _top_k = retriever.calls[0]
    assert first_filters == {"category": "платформа_фгаис", "source_type": "yonote"}
    assert "auth register" in first_query
    assert all("forum_normalized" not in filters for _query, filters, _top_k in retriever.calls)
    assert result["metadata_filter"]["category"] == "платформа_фгаис"
    assert result["metadata_filter"]["forum_normalized"] is None


@pytest.mark.asyncio
async def test_bounded_fgais_intent_keeps_scoped_semantic_recall_after_metadata_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.retrieve.get_settings",
        lambda: SimpleNamespace(retrieval_strict_forum_stop_min_chunks=2),
    )
    expected = _yonote_chunk(
        "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
        "Регистрация проходит по ссылке https://myrosmol.ru/auth/register",
        category="платформа_фгаис",
        topic="registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
    )
    metadata_neighbor = _yonote_chunk(
        "metadata_registration_neighbor",
        "Регистрация на отдельное мероприятие.",
        category="платформа_фгаис",
        topic="kak_zaregistrirovatsya_na_fgais",
    )
    retriever = _ScopedRecallRetriever(
        metadata_chunks=[metadata_neighbor],
        semantic_chunks=[expected],
    )

    result = await retrieve(
        {
            "message_masked": "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
            "analysis": QueryAnalysis(
                category="платформа_фгаис",
                questions=[
                    Question(
                        text="Как зарегистрироваться во ФГАИС?",
                        topic="kak_zaregistrirovatsya_na_fgais",
                        category="платформа_фгаис",
                    )
                ],
            ),
            "retriever": retriever,
        }
    )

    assert retriever.metadata_calls
    assert retriever.calls == [
        (
            "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
            {"category": "платформа_фгаис", "source_type": "yonote"},
            30,
        )
    ]
    assert {chunk.chunk_id for chunk in result["retrieved_chunks"]} == {
        metadata_neighbor.chunk_id,
        expected.chunk_id,
    }


@pytest.mark.asyncio
async def test_multi_aspect_topic_hits_still_get_one_shared_scoped_semantic_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.retrieve.get_settings",
        lambda: SimpleNamespace(retrieval_strict_forum_stop_min_chunks=2),
    )
    overview = _yonote_chunk(
        "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
        "Форум проходит с 20 июля по 6 августа.",
        category="форумы",
        topic="o_meropriyatii",
        forum="Территория смыслов",
    )
    shifts = _yonote_chunk(
        "yonote_api_zrvcb9k240_s0004_tematicheskie_smeny_foruma",
        "Смены: Единство, Правда и Родина.",
        category="форумы",
        topic="tematicheskie_smeny_foruma",
        forum="Территория смыслов",
    )
    metadata_neighbor = _yonote_chunk(
        "metadata_topic_neighbor",
        "Соседний опубликованный раздел форума.",
        category="форумы",
        topic="sut_foruma_i_napravleniya",
        forum="Территория смыслов",
    )
    retriever = _ScopedRecallRetriever(
        metadata_chunks=[metadata_neighbor],
        semantic_chunks=[overview, shifts],
    )
    query = "Что за «Территория смыслов», когда она идёт и какие там смены?"

    result = await retrieve(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(
                category="форумы",
                forum_normalized="Территория смыслов",
                questions=[
                    Question(
                        text="Что это за форум и когда он проходит?",
                        topic="o_meropriyatii",
                        category="форумы",
                        forum_normalized="Территория смыслов",
                    ),
                    Question(
                        text="Какие тематические смены будут?",
                        topic="tematicheskie_smeny_foruma",
                        category="форумы",
                        forum_normalized="Территория смыслов",
                    ),
                ],
            ),
            "retriever": retriever,
        }
    )

    assert len(retriever.metadata_calls) == 2
    assert retriever.calls == [
        (
            query,
            {
                "forum_normalized": "Территория смыслов",
                "category": "форумы",
                "source_type": "yonote",
            },
            30,
        )
    ]
    assert {overview.chunk_id, shifts.chunk_id}.issubset(
        {chunk.chunk_id for chunk in result["retrieved_chunks"]}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "analysis", "expected_ids", "expected_scope"),
    [
        (
            (
                "Без канцелярита: где зарегистрироваться во ФГАИС и как потом "
                "найти мероприятие по региону?"
            ),
            QueryAnalysis(
                category="платформа_фгаис",
                questions=[
                    Question(
                        text="Где зарегистрироваться во ФГАИС?",
                        topic="kak_zaregistrirovatsya_na_fgais",
                        category="платформа_фгаис",
                    ),
                    Question(
                        text="Как найти мероприятие по региону?",
                        topic="poisk_i_navigaciya_po_meropriyatiyam",
                        category="платформа_фгаис",
                    ),
                ],
            ),
            (
                "yonote_api_u7b5sscrri_s0002_registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                "yonote_api_u7b5sscrri_s0010_poisk_i_navigaciya_po_meropriyatiyam",
            ),
            {"category": "платформа_фгаис", "source_type": "yonote"},
        ),
        (
            (
                "По «Ладоге» сразу три вещи: до какого числа заявка, кто платит "
                "за проживание с едой и могут ли компенсировать дорогу?"
            ),
            QueryAnalysis(
                category="форумы",
                forum_normalized="Ладога",
                questions=[
                    Question(
                        text="До какого числа подать заявку?",
                        topic="podacha_zayavki_na_proekt",
                        category="форумы",
                        forum_normalized="Ладога",
                    ),
                    Question(
                        text="Кто платит за проживание и питание?",
                        topic="usloviya_prozhivaniya",
                        category="форумы",
                        forum_normalized="Ладога",
                    ),
                    Question(
                        text="Компенсируют ли дорогу?",
                        topic="oplata_proezda",
                        category="форумы",
                        forum_normalized="Ладога",
                    ),
                ],
            ),
            (
                "yonote_api_irwwd4t2v8_s0006_forum",
                "yonote_api_irwwd4t2v8_s0008_pitanie_i_prozhivanie",
                "yonote_api_irwwd4t2v8_s0012_kompensaciya",
            ),
            {
                "forum_normalized": "Ладога",
                "category": "форумы",
                "source_type": "yonote",
            },
        ),
        (
            "В двух словах, что за «Территория смыслов», когда она идёт и какие там смены?",
            QueryAnalysis(
                category="форумы",
                forum_normalized="Территория смыслов",
                questions=[
                    Question(
                        text="Что это за форум и когда он проходит?",
                        topic="o_meropriyatii",
                        category="форумы",
                        forum_normalized="Территория смыслов",
                    ),
                    Question(
                        text="Какие тематические смены будут?",
                        topic="tematicheskie_smeny_foruma",
                        category="форумы",
                        forum_normalized="Территория смыслов",
                    ),
                ],
            ),
            (
                "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
                "yonote_api_zrvcb9k240_s0004_tematicheskie_smeny_foruma",
            ),
            {
                "forum_normalized": "Территория смыслов",
                "category": "форумы",
                "source_type": "yonote",
            },
        ),
        (
            (
                "На Добро.РФ хочу с нуля: как создать кабинет, а потом отфильтровать "
                "мероприятие и подать заявку волонтёром?"
            ),
            QueryAnalysis(
                category="форумы",
                forum_normalized="Добро.РФ",
                questions=[
                    Question(
                        text="Как создать кабинет?",
                        topic="registraciya_s_pomoschyu_sozdaniya_kabineta",
                        category="форумы",
                        forum_normalized="Добро.РФ",
                    ),
                    Question(
                        text="Как найти мероприятие и подать заявку волонтёром?",
                        topic="volonterskaya_pomosch",
                        category="форумы",
                        forum_normalized="Добро.РФ",
                    ),
                ],
            ),
            (
                "yonote_api_jw4tdtr1pc_s0005_registraciya_s_pomoschyu_sozdaniya_kabineta",
                "yonote_api_jw4tdtr1pc_s0008_volonterskaya_pomosch",
            ),
            {
                "forum_normalized": "Добро.РФ",
                "category": "форумы",
                "source_type": "yonote",
            },
        ),
        (
            (
                "У «Территории смыслов» назови общий период форума и отдельно даты "
                "смены «Правда» — не перепутай."
            ),
            QueryAnalysis(
                category="форумы",
                forum_normalized="Территория смыслов",
                questions=[
                    Question(
                        text="Какой общий период форума?",
                        topic="o_meropriyatii",
                        category="форумы",
                        forum_normalized="Территория смыслов",
                    ),
                    Question(
                        text="Какие даты у смены Правда?",
                        topic="daty_26_30_iyulya_2026_goda",
                        category="форумы",
                        forum_normalized="Территория смыслов",
                    ),
                ],
            ),
            (
                "yonote_api_zrvcb9k240_s0001_o_meropriyatii",
                "yonote_api_zrvcb9k240_s0009_daty_26_30_iyulya_2026_goda",
            ),
            {
                "forum_normalized": "Территория смыслов",
                "category": "форумы",
                "source_type": "yonote",
            },
        ),
    ],
)
async def test_source_binding_cases_recall_every_exact_published_yonote_qrel(
    monkeypatch: pytest.MonkeyPatch,
    published_yonote_seed: dict[str, dict],
    query: str,
    analysis: QueryAnalysis,
    expected_ids: tuple[str, ...],
    expected_scope: dict[str, str],
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.retrieve.get_settings",
        lambda: SimpleNamespace(retrieval_strict_forum_stop_min_chunks=2),
    )
    expected_chunks = [
        _published_seed_chunk(published_yonote_seed, chunk_id)
        for chunk_id in expected_ids
    ]
    retriever = _ScopedRecallRetriever(
        metadata_chunks=[expected_chunks[0]],
        semantic_chunks=expected_chunks,
    )

    result = await retrieve(
        {
            "message_masked": query,
            "analysis": analysis,
            "retriever": retriever,
        }
    )

    assert retriever.calls == [(query, expected_scope, 30)]
    assert set(expected_ids).issubset(
        {chunk.chunk_id for chunk in result["retrieved_chunks"]}
    )
    assert all(
        published_yonote_seed[chunk_id]["status"] == "published"
        and published_yonote_seed[chunk_id]["source_type"] == "yonote"
        for chunk_id in expected_ids
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Как зарегистрироваться на форум Амур во ФГАИС?",
        "Как зарегистрироваться во ФГАИС для участия?",
    ],
)
async def test_retrieve_keeps_named_event_scope_for_event_registration(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    monkeypatch.setattr(
        "src.graph.nodes.retrieve.get_settings",
        lambda: SimpleNamespace(retrieval_strict_forum_stop_min_chunks=2),
    )
    retriever = _RecordingRetriever()
    await retrieve(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(category="форумы", forum_normalized="Амур"),
            "retriever": retriever,
        }
    )

    assert retriever.calls
    _query, first_filters, _top_k = retriever.calls[0]
    assert first_filters["category"] == "форумы"
    assert first_filters["forum_normalized"] == "Амур"
    assert all("auth register" not in query for query, _filters, _top_k in retriever.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "category", "forum"),
    [
        (
            "Какого хуя не грузится ФГАИС?",
            "техподдержка",
            None,
        ),
        (
            "Блин, где мой грёбаный билет на День молодёжи?",
            "форумы",
            "День молодёжи",
        ),
        (
            "Блин, не могу подать заявку на Амур, что делать?",
            "форумы",
            "Амур",
        ),
    ],
)
async def test_pilot50_absent_published_source_remains_fail_closed(
    _rerank_settings: None,
    query: str,
    category: str,
    forum: str | None,
) -> None:
    result = await rerank(
        {
            "message_masked": query,
            "analysis": QueryAnalysis(category=category, forum_normalized=forum),
            "retrieved_chunks": [],
            "reranker": _NeverReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_relevant_chunks"


@pytest.mark.asyncio
async def test_bounded_intent_metadata_drift_fails_closed(
    _rerank_settings: None,
) -> None:
    result = await rerank(
        {
            "message_masked": "Как зарегистрироваться во ФГАИС «Молодёжь России»?",
            "analysis": QueryAnalysis(category="форумы"),
            "retrieved_chunks": [
                _yonote_chunk(
                    "registration_with_drifted_category",
                    "Регистрация проходит по ссылке https://myrosmol.ru/auth/register",
                    category="общее",
                    topic="registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
                    score=0.99,
                )
            ],
            "reranker": _EmptyReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_relevant_chunks"


@pytest.mark.asyncio
async def test_bounded_intent_missing_exact_target_does_not_use_same_category_distractor(
    _rerank_settings: None,
) -> None:
    result = await rerank(
        {
            "message_masked": "Где во ФГАИС найти доступные мероприятия?",
            "analysis": QueryAnalysis(category="платформа_фгаис"),
            "retrieved_chunks": [
                _yonote_chunk(
                    "unrelated_platform_source",
                    "Как восстановить пароль от личного кабинета.",
                    category="платформа_фгаис",
                    topic="vosstanovit_parol",
                    score=0.99,
                )
            ],
            "reranker": _EmptyReranker(),
        }
    )

    assert result["reranked_chunks"] == []
    assert result["should_escalate"] is True
    assert result["escalation_reason"] == "no_relevant_chunks"


def test_named_event_registration_keeps_named_priority_source() -> None:
    query = "Как зарегистрироваться на Машук во ФГАИС?"
    named = _yonote_chunk(
        "mashuk_registration",
        "Регистрация на форум Машук проходит через ФГАИС.",
        category="форумы",
        topic="podacha_zayavki_na_proekt",
        forum="Машук",
    )
    generic = _yonote_chunk(
        "generic_registration",
        "Регистрация аккаунта проходит по ссылке https://myrosmol.ru/auth/register.",
        category="платформа_фгаис",
        topic="registraciya_prohodit_po_ssylke_https_myrosmol_ru_auth_regis",
        score=0.99,
    )

    selected = _priority_source_candidate(
        query,
        [generic, named],
        forum_normalized="Машук",
    )

    assert selected is not None
    assert selected.chunk_id == "mashuk_registration"


@pytest.mark.asyncio
async def test_generic_grant_directions_reject_specific_microgrant_entity(
    _rerank_settings: None,
) -> None:
    generic = _yonote_chunk(
        "generic_grant_nominations",
        "Номинации грантовых конкурсов — это тематики проектов.",
        category="гранты",
        topic="nominacii_grantovyh_konkursov",
        forum="Росмолодёжь.Гранты",
    )
    microgrant = _yonote_chunk(
        "microgrant_nominations",
        "Номинации конкурса микрогрантов.",
        category="гранты",
        topic="nominacii_grantovyh_konkursov",
        forum="Росмолодёжь.Гранты: Микрогранты",
        score=0.99,
    )

    result = await rerank(
        {
            "message_masked": "Какие направления есть в конкурсах Росмолодёжь.Гранты?",
            "analysis": QueryAnalysis(category="гранты"),
            "retrieved_chunks": [microgrant, generic],
            "reranker": _NeverReranker(),
        }
    )

    assert result.get("should_escalate") is not True
    assert result["reranked_chunks"][0].chunk_id == "generic_grant_nominations"
