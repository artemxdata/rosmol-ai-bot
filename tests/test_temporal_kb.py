import json
from datetime import datetime

import pytest

from src.graph.nodes.respond import respond
from src.kb.temporal import (
    MOSCOW_TZ,
    expired_registration_response,
    extract_registration_deadline,
    is_registration_query,
)
from src.models import Complexity, QueryAnalysis, ScoredChunk


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
    result = await respond(
        {
            "message": "Хочу попасть на форум Ростов, что нужно сделать?",
            "message_masked": "Хочу попасть на форум Ростов, что нужно сделать?",
            "analysis": _analysis(),
            "reranked_chunks": [
                _rostov_chunk(
                    "Хочешь попасть на форум? Окончание приёма заявок — "
                    "06.07.2026 г. Регистрируйся прямо сейчас!"
                )
            ],
            "generated_response": "Регистрируйся прямо сейчас!",
        }
    )

    assert result["final_response"].startswith("Регистрация на форум «Ростов» закрыта")
    assert "Регистрируйся прямо сейчас" not in result["final_response"]


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
