import json

from src.kb.event_facts import (
    concise_event_place_date_response,
    foreign_registration_response,
)
from src.models import Complexity, QueryAnalysis, ScoredChunk


def test_concise_event_place_date_response_extracts_only_requested_facts() -> None:
    analysis = QueryAnalysis(
        forum="Ростов",
        forum_normalized="Ростов",
        category="форумы",
        complexity=Complexity.COMPLEX,
    )
    chunk = ScoredChunk(
        chunk_id="yonote_rostov_description",
        text=(
            "Описание. Длинный текст о тематике форума. "
            "Дата и место проведения: с 6 по 10 сентября 2026 года. "
            "Форум будет проходить в Ростовской области. "
            "Участники: граждане Российской Федерации от 14 до 35 лет."
        ),
        metadata={"forum_normalized": "Ростов", "source_type": "yonote"},
        score=1.0,
        reranker_score=0.9,
    )

    response = concise_event_place_date_response(
        message="Где и когда проходит форум Ростов?",
        analysis=analysis,
        chunks=[chunk],
    )

    assert response == (
        "Форум «Ростов» пройдёт с 6 по 10 сентября 2026 года "
        "в Ростовской области."
    )


def test_concise_event_place_date_response_ignores_other_questions() -> None:
    response = concise_event_place_date_response(
        message="Как подать заявку на Ростов?",
        analysis=QueryAnalysis(forum_normalized="Ростов"),
        chunks=[],
    )

    assert response is None


def test_foreign_registration_response_uses_separate_yonote_link(tmp_path) -> None:
    seed_path = tmp_path / "knowledge_base_seed.json"
    seed_path.write_text(
        json.dumps(
            [
                {
                    "forum_normalized": "Ростов",
                    "source_type": "yonote",
                    "text_clean": (
                        "Регистрация для иностранных участников доступна по ссылке: "
                        "https://wyffest.com/events/rostov-26"
                    ),
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = foreign_registration_response(
        message="Как иностранному участнику зарегистрироваться на Ростов?",
        analysis=QueryAnalysis(forum_normalized="Ростов"),
        chunks=[],
        seed_path=seed_path,
    )

    assert response == (
        "Для иностранных участников регистрация на форум «Ростов» доступна отдельно: "
        "https://wyffest.com/events/rostov-26"
    )
