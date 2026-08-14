from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.llm.prompts import RESPONSE_GENERATOR_SYSTEM, build_generator_user
from src.models import Chunk, Question


def test_quality_prompt_bundle_has_explicit_version() -> None:
    assert Settings(_env_file=None).prompt_version == "pilot50-quality-v2"


def test_prompt_version_cannot_exceed_trace_schema_limit() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, prompt_version="x" * 21)


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


def test_generator_retry_prompt_explains_fact_binding_failure() -> None:
    prompt = build_generator_user(
        questions=[Question(text="Кто оплачивает проезд для наставников?")],
        chunks=[
            Chunk(
                chunk_id="travel",
                text="Проезд наставника оплачивает направляющая сторона.",
                metadata={"source_type": "yonote"},
            )
        ],
        session=None,
        retry_reason="llm_source_fact_binding_failed",
    )

    assert "Не меняй плательщика" in prompt
    assert "роль участника" in prompt
    assert "Возраст, смену, дату и срок" in prompt


def test_generator_retry_prompt_explains_profile_failure() -> None:
    prompt = build_generator_user(
        questions=[Question(text="Будет ли питание?")],
        chunks=[
            Chunk(
                chunk_id="food",
                text="Для участников предусмотрено питание.",
                metadata={"source_type": "yonote"},
            )
        ],
        session=None,
        retry_reason="llm_response_profile_failed",
    )

    assert "только аспекты, которые прямо запрошены" in prompt
    assert "Каждый запрошенный аспект сохрани" in prompt
