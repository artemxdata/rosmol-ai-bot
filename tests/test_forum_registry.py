from __future__ import annotations

from src.kb.forum_registry import (
    canonicalize_forum_name,
    detect_forum_from_text,
    detect_forums_from_text,
    forum_filter_values,
)


def test_detect_forum_from_text_uses_registry_aliases() -> None:
    assert (
        detect_forum_from_text("Хочу попасть на форум Российский Север")
        == "Российский Север"
    )


def test_detect_forum_from_text_ignores_partial_word_matches() -> None:
    assert detect_forum_from_text("Шумный город без названия форума") is None


def test_detect_forum_from_text_handles_ivolga_genitive_case() -> None:
    assert detect_forum_from_text("Какая программа форума Иволги?") == "Иволга"


def test_detect_forum_from_text_handles_volga_accusative_case() -> None:
    assert detect_forum_from_text("Расскажи про Волгу") == "Волга"


def test_detect_forum_from_text_handles_ladoga_prepositional_case() -> None:
    assert (
        detect_forum_from_text("По «Ладоге» сразу три вопроса")
        == "Ладога"
    )


def test_detect_forum_from_text_handles_mashuk_genitive_case() -> None:
    assert detect_forum_from_text("Сверь даты смен Машука") == "Машук"


def test_detect_forum_from_text_handles_new_yonote_entities() -> None:
    assert detect_forum_from_text("Как зарегистрироваться на Добро.РФ?") == "Добро.РФ"
    assert (
        detect_forum_from_text("Кто может участвовать в Национальной премии Патриот?")
        == "Национальная премия «Патриот»"
    )


def test_detect_forum_from_text_loads_distinct_seed_entities_but_not_navigation() -> None:
    assert detect_forum_from_text("Расскажи про Наука. КМОЦ") == "Наука. КМОЦ"
    assert detect_forum_from_text("Какие платформы используются?") is None


def test_forum_source_variants_share_canonical_name_and_filter_values() -> None:
    assert canonicalize_forum_name("ОстроVа") == "Островa"
    assert canonicalize_forum_name("ШУМ") == "Шум"
    assert canonicalize_forum_name("иВолга") == "Иволга"
    assert "ОстроVа" in forum_filter_values("Островa")


def test_detect_forum_from_text_handles_tavrida_art_accusative_case() -> None:
    assert detect_forum_from_text("Как попасть на Тавриду.Арт?") == "Таврида"


def test_detect_forum_from_text_handles_youth_day_genitive_case() -> None:
    assert detect_forum_from_text("Где программа Дня молодёжи?") == "День молодёжи"


def test_detect_forum_from_text_handles_latin_e_diaeresis_in_youth_day() -> None:
    assert detect_forum_from_text("Не пришёл билет на день молодëжи") == "День молодёжи"


def test_detect_forum_from_text_maps_youth_day_campaign_names() -> None:
    assert detect_forum_from_text("Где выступает DJ Smash?") == "День молодёжи"
    assert detect_forum_from_text("Когда на сцене The Hatters?") == "День молодёжи"
    assert detect_forum_from_text("Программа Молфеста") == "День молодёжи"
    assert detect_forum_from_text("Где фотоотчёт с молфеста74?") == "День молодёжи"


def test_detect_forum_from_text_handles_mixed_alphabet_ostrova() -> None:
    assert detect_forum_from_text("Я прошла первый этап форума ОстроVа") == "Островa"


def test_detect_forum_from_text_handles_territory_of_meanings_genitive_case() -> None:
    assert (
        detect_forum_from_text("Как организован трансфер Территории смыслов?")
        == "Территория смыслов"
    )


def test_detect_forum_from_text_maps_named_shifts_to_parent_events() -> None:
    assert detect_forum_from_text("Когда проходит смена ФинЗОЖ?") == "ТИМ Бирюса"
    assert detect_forum_from_text("Я участник смены Детство") == "Истоки Школа"


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
