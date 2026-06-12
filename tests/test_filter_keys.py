from __future__ import annotations

from src.rag.filter_keys import (
    build_filter_key_payload,
    category_filter_key,
    stable_text_filter_key,
)


def test_category_filter_key_maps_known_categories_to_ascii() -> None:
    assert category_filter_key("гранты") == "grants"
    assert category_filter_key("Форумы") == "forums"
    assert category_filter_key("технические проблемы") == "tech_support"


def test_stable_text_filter_key_is_ascii_and_deterministic() -> None:
    first = stable_text_filter_key("Машук")
    second = stable_text_filter_key("машук")

    assert first == second
    assert first.startswith("h_")
    assert first.isascii()


def test_build_filter_key_payload_adds_derived_keys() -> None:
    payload = build_filter_key_payload(
        {
            "category": "гранты",
            "forum_normalized": "Машук",
            "topic": "оплата_проезда",
        }
    )

    assert payload["category_key"] == "grants"
    assert payload["forum_key"] == stable_text_filter_key("Машук")
    assert payload["topic_key"] == stable_text_filter_key("оплата_проезда")
