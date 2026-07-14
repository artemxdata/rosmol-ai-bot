import json
from datetime import datetime

import pytest

from src.graph.nodes.guard import apply_response_guards
from src.graph.nodes.respond import respond
from src.graph.nodes.verify import verify
from src.kb.temporal import (
    MOSCOW_TZ,
    expired_registration_response,
    extract_registration_deadline,
    is_registration_query,
)
from src.models import Complexity, QueryAnalysis, Question, ScoredChunk


def _rostov_chunk(text: str) -> ScoredChunk:
    return ScoredChunk(
        chunk_id="rostov_registration",
        text=text,
        metadata={
            "forum_normalized": "Ростов",
            "topic": "podacha_zayavki_na_proekt",
            "source_type": "yonote",
        },
        score=1.0,
        reranker_score=0.9,
    )


def _analysis() -> QueryAnalysis:
    return QueryAnalysis(
        forum="Ростов",
        forum_normalized="Ростов",
        topics=["podacha_zayavki_na_proekt"],
        category="форумы",
        complexity=Complexity.SIMPLE,
    )


def test_extract_registration_deadline_with_moscow_time() -> None:
    deadline = extract_registration_deadline(
        "Регистрация во ФГАИС доступна до 06.07.2026 23:59 мск."
    )

    assert deadline is not None
    assert deadline.closes_at == datetime(2026, 7, 6, 23, 59, tzinfo=MOSCOW_TZ)
    assert deadline.explicit_time is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Окончание регистрации (по московскому времени): 22.04.2026 23:59",
            datetime(2026, 4, 22, 23, 59, tzinfo=MOSCOW_TZ),
        ),
        (
            "Подача заявок проходит через ФГАИС по ссылке "
            "https://myrosmol.ru/events/example до 11.05.2026 23:59 мск.",
            datetime(2026, 5, 11, 23, 59, tzinfo=MOSCOW_TZ),
        ),
        (
            "Регистрация. На сайте ФГАИС по ссылке: "
            "https://myrosmol.ru/events/example до 15.07.2026 23:59 мск.",
            datetime(2026, 7, 15, 23, 59, tzinfo=MOSCOW_TZ),
        ),
    ],
)
def test_extract_registration_deadline_accepts_yonote_formats(
    text: str,
    expected: datetime,
) -> None:
    deadline = extract_registration_deadline(text)

    assert deadline is not None
    assert deadline.closes_at == expected
    assert deadline.explicit_time is True


def test_extract_registration_deadline_does_not_treat_event_date_as_deadline() -> None:
    assert (
        extract_registration_deadline(
            "Форум пройдёт с 6 по 10 сентября 2026 года в Ростовской области."
        )
        is None
    )


@pytest.mark.parametrize(
    "query",
    [
        "Хочу попасть на форум Ростов, что нужно сделать?",
        "Как подать заявку на Ростов?",
        "Где зарегистрироваться?",
    ],
)
def test_registration_queries_are_temporally_sensitive(query: str) -> None:
    assert is_registration_query(query, ["podacha_zayavki_na_proekt"])


def test_expired_registration_response_uses_latest_known_deadline() -> None:
    response = expired_registration_response(
        message="Хочу попасть на форум Ростов, что нужно сделать?",
        analysis=_analysis(),
        chunks=[
            _rostov_chunk("Окончание приёма заявок — 06.07.2026 г."),
            _rostov_chunk("Регистрация во ФГАИС доступна до 06.07.2026 23:59 мск."),
        ],
        now=datetime(2026, 7, 10, 12, 0, tzinfo=MOSCOW_TZ),
    )

    assert response is not None
    assert response.startswith("Регистрация на форум «Ростов» закрыта")
    assert "6 июля 2026 года в 23:59 (мск)" in response
    assert "Новую заявку сейчас подать нельзя" in response


def test_registration_is_not_closed_before_deadline() -> None:
    response = expired_registration_response(
        message="Как подать заявку на Ростов?",
        analysis=_analysis(),
        chunks=[_rostov_chunk("Регистрация открыта до 06.07.2026 23:59 мск.")],
        now=datetime(2026, 7, 6, 22, 0, tzinfo=MOSCOW_TZ),
    )

    assert response is None


def test_foreign_registration_is_not_closed_by_domestic_deadline() -> None:
    response = expired_registration_response(
        message="Как иностранному участнику зарегистрироваться на Ростов?",
        analysis=_analysis(),
        chunks=[_rostov_chunk("Регистрация во ФГАИС доступна до 06.07.2026 23:59 мск.")],
        now=datetime(2026, 7, 10, 12, 0, tzinfo=MOSCOW_TZ),
    )

    assert response is None


def test_legacy_deadline_without_matching_forum_name_is_not_a_hard_cutoff() -> None:
    chunk = _rostov_chunk("Окончание приёма заявок — 06.07.2026 г.")
    chunk.metadata["source_type"] = "xlsx"

    response = expired_registration_response(
        message="Как подать заявку на Ростов?",
        analysis=_analysis(),
        chunks=[chunk],
        now=datetime(2026, 7, 10, 12, 0, tzinfo=MOSCOW_TZ),
    )

    assert response is None


def test_seed_yonote_deadline_applies_when_retrieval_only_returned_legacy_chunk(
    tmp_path,
) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    seed_path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "yonote_rostov_registration",
                    "forum_normalized": "Ростов",
                    "source_type": "yonote",
                    "text_clean": (
                        "Регистрация во ФГАИС доступна до "
                        "06.07.2026 23:59 мск."
                    ),
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    legacy = _rostov_chunk("Окончание приёма заявок — 06.07.2026 г.")
    legacy.metadata["source_type"] = "xlsx"

    response = expired_registration_response(
        message="Хочу попасть на форум Ростов, что нужно сделать?",
        analysis=_analysis(),
        chunks=[legacy],
        now=datetime(2026, 7, 10, 12, 0, tzinfo=MOSCOW_TZ),
        seed_path=seed_path,
    )

    assert response is not None
    assert "6 июля 2026 года в 23:59 (мск)" in response


@pytest.mark.asyncio
async def test_respond_replaces_stale_registration_call_to_action() -> None:
    state = {
        "message": "Хочу попасть на форум Ростов, что нужно сделать?",
        "message_masked": "Хочу попасть на форум Ростов, что нужно сделать?",
        "analysis": _analysis(),
        "reranked_chunks": [
            _rostov_chunk(
                "Хочешь попасть на форум Ростов? Окончание приёма заявок — "
                "06.07.2026 г. Регистрируйся прямо сейчас!"
            )
        ],
        "max_confidence": 0.9,
        "generated_response": "Регистрируйся прямо сейчас!",
    }

    guarded = await apply_response_guards(state)
    verified = await verify({**state, **guarded})
    result = await respond({**state, **guarded, **verified})

    assert result["final_response"].startswith("Регистрация на форум «Ростов» закрыта")
    assert "Регистрируйся прямо сейчас" not in result["final_response"]
    assert len(guarded["cited_sources"]) == 1
    assert guarded["cited_sources"][0] in {
        chunk.chunk_id for chunk in guarded["reranked_chunks"]
    }
    assert verified["verification"].has_hallucination is False


@pytest.mark.asyncio
async def test_respond_does_not_change_non_registration_answer() -> None:
    result = await respond(
        {
            "message": "Где проходит форум Ростов?",
            "analysis": _analysis(),
            "reranked_chunks": [
                _rostov_chunk("Окончание приёма заявок — 06.07.2026 г.")
            ],
            "generated_response": "Форум пройдёт в Ростовской области.",
        }
    )

    assert result["final_response"] == "Форум пройдёт в Ростовской области."


@pytest.mark.asyncio
async def test_response_guards_skip_ambiguous_multi_forum_request() -> None:
    analysis = QueryAnalysis(
        forum_normalized="Машук",
        category="форумы",
        extracted_params={"detected_forums": ["Ростов", "Машук"]},
    )

    guarded = await apply_response_guards(
        {
            "message_masked": "Как иностранцу зарегистрироваться на Ростов и Машук?",
            "analysis": analysis,
            "reranked_chunks": [],
        }
    )

    assert guarded == {}


@pytest.mark.asyncio
async def test_response_guards_preserve_multi_aspect_single_forum_answer() -> None:
    analysis = QueryAnalysis(
        forum="Ростов",
        forum_normalized="Ростов",
        category="форумы",
        questions=[
            Question(
                text="Где и когда проходит мероприятие?",
                topic="daty_nachala_meropriyatiya",
                category="форумы",
                forum_normalized="Ростов",
            ),
            Question(
                text="Как подать заявку или зарегистрироваться?",
                topic="podacha_zayavki_na_proekt",
                category="форумы",
                forum_normalized="Ростов",
            ),
        ],
    )
    place_chunk = ScoredChunk(
        chunk_id="yonote_rostov_description",
        text=(
            "Дата и место проведения: с 6 по 10 сентября 2026 года. "
            "Форум будет проходить в Ростовской области."
        ),
        metadata={"forum_normalized": "Ростов", "source_type": "yonote"},
        score=1.0,
        reranker_score=0.9,
    )
    registration_chunk = _rostov_chunk("Регистрация проходит через ФГАИС.")

    guarded = await apply_response_guards(
        {
            "message_masked": (
                "Где и когда проходит форум Ростов и как зарегистрироваться?"
            ),
            "analysis": analysis,
            "reranked_chunks": [place_chunk, registration_chunk],
            "generated_response": "Полный ответ по месту, датам и регистрации.",
            "cited_sources": [place_chunk.chunk_id, registration_chunk.chunk_id],
        }
    )

    assert guarded == {}


@pytest.mark.asyncio
async def test_registration_guard_does_not_drop_place_date_when_extractor_misses() -> None:
    analysis = QueryAnalysis(
        forum="Ростов",
        forum_normalized="Ростов",
        category="форумы",
        questions=[
            Question(
                text="Где и когда проходит мероприятие?",
                topic="daty_nachala_meropriyatiya",
                category="форумы",
                forum_normalized="Ростов",
            ),
            Question(
                text="До какого числа можно зарегистрироваться?",
                topic="podacha_zayavki_na_proekt",
                category="форумы",
                forum_normalized="Ростов",
            ),
        ],
    )
    place_chunk = ScoredChunk(
        chunk_id="rostov_place_without_guard_format",
        text="Форум состоится 6 сентября 2026 года в Ростовской области.",
        metadata={"forum_normalized": "Ростов", "source_type": "yonote"},
        score=1.0,
        reranker_score=0.9,
    )
    registration_chunk = _rostov_chunk(
        "Регистрация во ФГАИС доступна до 06.07.2026 23:59 мск."
    )

    guarded = await apply_response_guards(
        {
            "message_masked": (
                "Где и когда проходит форум Ростов и до какого числа регистрация?"
            ),
            "analysis": analysis,
            "reranked_chunks": [place_chunk, registration_chunk],
            "generated_response": "Полный ответ по месту, датам и сроку регистрации.",
            "cited_sources": [place_chunk.chunk_id, registration_chunk.chunk_id],
        }
    )

    assert guarded == {}


@pytest.mark.asyncio
async def test_registration_deadline_questions_remain_one_guard_aspect() -> None:
    analysis = QueryAnalysis(
        forum="Ростов",
        forum_normalized="Ростов",
        category="форумы",
        questions=[
            Question(
                text="Какие даты и сроки?",
                topic="daty_nachala_meropriyatiya",
                category="форумы",
                forum_normalized="Ростов",
            ),
            Question(
                text="До какого числа можно зарегистрироваться?",
                topic="podacha_zayavki_na_proekt",
                category="форумы",
                forum_normalized="Ростов",
            ),
        ],
    )
    registration_chunk = _rostov_chunk(
        "Регистрация во ФГАИС доступна до 06.07.2026 23:59 мск."
    )

    guarded = await apply_response_guards(
        {
            "message_masked": "До какого числа регистрация на форум Ростов?",
            "analysis": analysis,
            "reranked_chunks": [registration_chunk],
            "generated_response": "Регистрация открыта.",
        }
    )

    assert guarded["response_guard"] == "registration_closed"


def test_seed_deadline_ignores_archived_record(tmp_path) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    seed_path.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "archived_deadline",
                    "forum_normalized": "Ростов",
                    "source_type": "yonote",
                    "status": "archived",
                    "text_clean": "Регистрация открыта до 06.07.2026 23:59 мск.",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = expired_registration_response(
        message="Как подать заявку на Ростов?",
        analysis=_analysis(),
        chunks=[],
        now=datetime(2026, 7, 10, 12, 0, tzinfo=MOSCOW_TZ),
        seed_path=seed_path,
    )

    assert response is None
