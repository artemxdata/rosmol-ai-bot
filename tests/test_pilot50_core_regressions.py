from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.graph.nodes.rerank import _priority_source_candidate, rerank
from src.graph.nodes.retrieve import retrieve
from src.graph.query_normalization import (
    ACCOUNT_DATA_RECOVERY,
    FORUM_DISCOVERY,
    GENERIC_PLATFORM_REGISTRATION,
    GRANT_DIRECTIONS,
    PHYSICAL_GRANTS_OVERVIEW,
    PLATFORM_EVENT_NAVIGATION,
    bounded_query_intent,
)
from src.models import Chunk, QueryAnalysis


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
