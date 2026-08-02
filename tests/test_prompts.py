from __future__ import annotations

from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import Chunk, Question


def test_generator_prompt_compacts_source_metadata() -> None:
    prompt = build_generator_user(
        questions=[Question(text="Кто оплачивает проезд?")],
        chunks=[
            Chunk(
                chunk_id="travel",
                text="Проезд оплачивает направляющая сторона.",
                metadata={
                    "forum_normalized": "Амур",
                    "category": "форумы",
                    "topic": "oplata_proezda",
                    "source_type": "xlsx",
                    "intent_name": "Оплата проезда",
                    "intent_examples": [
                        "кто платит за дорогу",
                        "возместят ли билет",
                    ],
                    "debug_payload": {"unused": True},
                },
            )
        ],
        session=None,
    )

    assert "forum_normalized" in prompt
    assert "oplata_proezda" in prompt
    assert "Оплата проезда" in prompt
    assert "Проезд оплачивает направляющая сторона." in prompt
    assert "intent_examples" not in prompt
    assert "кто платит за дорогу" not in prompt
    assert "debug_payload" not in prompt


def test_generator_prompt_preserves_conditional_metadata_and_citation_contract() -> None:
    prompt = build_generator_user(
        questions=[Question(text="Какие даты смены для разных возрастов?")],
        chunks=[
            Chunk(
                chunk_id="yonote_conditional_dates",
                text=(
                    "Для участников 14–17 лет смена проходит 8–22 августа, "
                    "для участников 18–35 лет — 8–15 августа."
                ),
                metadata={
                    "source_type": "yonote",
                    "has_conditional_logic": True,
                    "conditions_summary": [
                        "14–17 лет: 8–22 августа",
                        "18–35 лет: 8–15 августа",
                    ],
                    "dates_mentioned": ["8–22 августа", "8–15 августа"],
                    "source_heading_path": ["Машук", "Первая смена"],
                },
            )
        ],
        session=None,
        retry_reason="llm_source_citation_failed",
    )

    assert "has_conditional_logic" in prompt
    assert "conditions_summary" in prompt
    assert "dates_mentioned" in prompt
    assert "source_heading_path" in prompt
    assert "ПОВТОРНАЯ ПОПЫТКА" in prompt
    assert "[src:yonote_conditional_dates]" in prompt
    assert "transport удалит их" in RESPONSE_GENERATOR_SYSTEM
    assert "Не раскрывай служебные метки." not in RESPONSE_GENERATOR_SYSTEM
