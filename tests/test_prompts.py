from __future__ import annotations

from src.llm.prompts import build_generator_user
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
