from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.kb.forum_registry import canonicalize_forum_name, detect_forum_from_text
from src.kb.source_extractors import apply_source_corrections, clean_bot_text

ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = ROOT / "data" / "knowledge_base_seed.json"
CORRECTIONS_PATH = ROOT / "data" / "kb_source_corrections.json"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "chunk_id",
    [
        "xlsx_category_r0023_podacha_zayavki_na_proekt",
        "xlsx_category_r0045_podacha_zayavki_na_proekt",
        "xlsx_category_r0098_podacha_zayavki_na_proekt",
        "xlsx_category_r0184_pamyatka_uchastnika_foruma",
        "xlsx_category_r0260_pamyatka_uchastnika_foruma",
        "xlsx_category_r0273_rezultaty_rm",
        "xlsx_category_r0348_rezervnyy_spisok",
        "xlsx_category_r0350_otkaz_ot_uchastiya",
        "xlsx_category_r0351_pochemu_otklonili_zayavku",
        "xlsx_category_r0354_pamyatka_uchastnika_foruma",
        "xlsx_category_r0369_podacha_zayavki_na_proekt",
        "xlsx_category_r0371_pochemu_otklonili_zayavku",
        "xlsx_category_r0374_pamyatka_uchastnika_foruma",
        "xlsx_category_r0393_usloviya_prozhivaniya",
        "xlsx_category_r0398_podacha_zayavki_na_proekt",
        "xlsx_category_r0418_podacha_zayavki_na_proekt",
        "xlsx_category_r0419_otkaz_ot_uchastiya",
        "xlsx_category_r0420_pochemu_otklonili_zayavku",
        "xlsx_category_r0423_pamyatka_uchastnika_foruma",
        "xlsx_category_r0496_vnesti_izmeneniya_v_zayavku",
        "xlsx_category_r0499_pamyatka_uchastnika_foruma",
        "xlsx_category_r0527_vnesti_izmeneniya_v_zayavku",
        "xlsx_category_r0530_pamyatka_uchastnika_foruma",
        "xlsx_category_r0556_vnesti_izmeneniya_v_zayavku",
        "xlsx_category_r0559_pamyatka_uchastnika_foruma",
        "xlsx_category_r0587_vnesti_izmeneniya_v_zayavku",
        "xlsx_category_r0590_pamyatka_uchastnika_foruma",
    ],
)
def test_cross_event_legacy_chunks_are_archived(chunk_id: str) -> None:
    records = {record["chunk_id"]: record for record in _load_json(SEED_PATH)}

    assert records[chunk_id]["status"] == "archived"
    assert records[chunk_id]["quality_review_reason"] == "cross_event_text_conflict"


def test_dobrino_grant_chunk_has_corrected_event_metadata() -> None:
    records = {record["chunk_id"]: record for record in _load_json(SEED_PATH)}
    record = records["xlsx_category_r0570_usloviya_i_sroki_uchastiya_granty"]

    assert record["forum"] == "Добрино"
    assert record["forum_normalized"] == "Добрино"
    assert record["source_category"] == "Добрино"
    assert record["quality_review_reason"] == "corrected_cross_event_metadata"


def test_known_invalid_links_are_corrected_in_published_seed() -> None:
    records = {record["chunk_id"]: record for record in _load_json(SEED_PATH)}
    template = records["xlsx_category_r0008_zapolnit_shablon_proekta"]
    application = records["xlsx_category_r0275_podacha_zayavki_na_proekt"]
    volunteer = records["xlsx_category_r0329_podacha_zayavki_na_proekt"]
    youth_day_place = records["xlsx_category_r0614_mesto_i_ploschadka_provedeniya"]

    assert all("](http" not in link for link in template["links"])
    assert "](http" not in template["text_clean"]
    assert application["links"] == ["https://events.myrosmol.ru/"]
    assert "events.myrosmol.rru" not in application["text_clean"]
    assert volunteer["links"] == ["https://dobro.ru/event/11672722"]
    assert "|Стать" not in volunteer["text_clean"]
    assert youth_day_place["links"] == ["https://max.ru/youthday_bot"]
    assert "bot'Место" not in youth_day_place["text_clean"]


def test_curated_aliases_join_source_labels_without_generic_grant_aliases() -> None:
    assert canonicalize_forum_name("Росмолодёжь.Гранты 1 сезон") == "Гранты 1 сезон"
    assert canonicalize_forum_name("молодых учёных «Полюс»") == "Полюс"
    assert detect_forum_from_text("Как получить грант?") is None
    assert detect_forum_from_text("Расскажи о грантах") is None


def test_source_correction_manifest_preconditions_are_strict() -> None:
    records = [
        {
            "chunk_id": "legacy_1",
            "text_clean": "Ответ про форум «Другой»",
            "forum": "Текущий",
            "forum_normalized": "Текущий",
            "status": "published",
        }
    ]
    registry = [{"name": "Текущий", "normalized": "Текущий", "aliases": ["Текущий"]}]
    corrections = {
        "record_overrides": {
            "legacy_1": {
                "expected": {
                    "forum_normalized": "Текущий",
                    "text_contains": "форум «Другой»",
                },
                "set": {"status": "archived"},
            }
        }
    }

    apply_source_corrections(records, registry, corrections)
    assert records[0]["status"] == "archived"

    records[0]["text_clean"] = "Источник уже исправлен"
    with pytest.raises(ValueError, match="precondition changed"):
        apply_source_corrections(records, registry, corrections)


def test_source_text_correction_rebuilds_derived_metadata() -> None:
    records = [
        {
            "chunk_id": "legacy_1",
            "text_clean": "Ссылка:https://example.org/file.pdf.Отбор",
            "links": ["https://example.org/file.pdf.Отбор"],
            "emails": [],
            "phones": [],
            "dates": [],
            "char_count": 1,
        }
    ]
    corrections = {
        "record_overrides": {
            "legacy_1": {
                "expected": {"text_contains": "pdf.Отбор"},
                "set": {
                    "text_clean": "Ссылка: https://example.org/file.pdf. Отбор",
                },
            }
        }
    }

    apply_source_corrections(records, [], corrections)

    assert records[0]["links"] == ["https://example.org/file.pdf"]
    assert records[0]["char_count"] == len(records[0]["text_clean"])


def test_source_cleaner_repairs_known_url_export_artifacts() -> None:
    malformed = (
        "[https://example.org/file.pdf](https://example.org/file.pdf.) "
        "https://events.myrosmol.rru/"
    )

    assert clean_bot_text(malformed) == (
        "https://example.org/file.pdf https://events.myrosmol.ru/"
    )


def test_source_cleaner_renders_vk_style_link_without_gluing_following_text() -> None:
    assert clean_bot_text(
        "Ссылка 👉[https://dobro.ru/event/123|Стать волонтёром]Ждём тебя"
    ) == "Ссылка 👉Стать волонтёром: https://dobro.ru/event/123 Ждём тебя"


def test_every_tracked_source_override_is_reflected_in_current_seed() -> None:
    records = {record["chunk_id"]: record for record in _load_json(SEED_PATH)}
    corrections = _load_json(CORRECTIONS_PATH)

    for chunk_id, override in corrections["record_overrides"].items():
        record = records[chunk_id]
        for field, expected in override["set"].items():
            assert record[field] == expected, f"{chunk_id}: {field}"
