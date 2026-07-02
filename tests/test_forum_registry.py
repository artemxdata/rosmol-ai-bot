from __future__ import annotations

from src.kb.forum_registry import detect_forum_from_text, detect_forums_from_text


def test_detect_forum_from_text_uses_registry_aliases() -> None:
    assert (
        detect_forum_from_text("Хочу попасть на форум Российский Север")
        == "Российский Север"
    )


def test_detect_forum_from_text_ignores_partial_word_matches() -> None:
    assert detect_forum_from_text("Шумный город без названия форума") is None


def test_detect_forum_from_text_handles_ivolga_genitive_case() -> None:
    assert detect_forum_from_text("Какая программа форума Иволги?") == "Иволга"


def test_detect_forums_from_text_returns_all_registry_matches_once() -> None:
    forums = detect_forums_from_text(
        "Чем отличаются Машук и Территория смыслов по проживанию?"
    )

    assert "Машук" in forums
    assert "Территория смыслов" in forums
    assert len(forums) == len(set(forums))


def test_detect_forums_prefers_long_alias_over_nested_short_alias() -> None:
    assert detect_forums_from_text("Истоки Школа Вышлите положение") == ["Истоки Школа"]


def test_detect_forums_keeps_short_alias_when_mentioned_separately() -> None:
    assert detect_forums_from_text("Чем отличаются Истоки Школа и Истоки?") == [
        "Истоки Школа",
        "Истоки",
    ]
