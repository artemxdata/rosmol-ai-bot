from __future__ import annotations

from src.rag.seed_retriever import SeedRetriever, tokenize


def test_tokenize_normalizes_case_and_yo() -> None:
    assert tokenize("Форум Ёлка forum-2026") == ["форум", "елка", "forum", "2026"]


def test_seed_retriever_returns_filtered_published_chunks() -> None:
    retriever = SeedRetriever(
        [
            {
                "chunk_id": "travel",
                "status": "published",
                "text_clean": "Проезд до форума оплачивает участник.",
                "forum_normalized": "Машук",
                "category": "форумы",
                "intent_name": "Оплата проезда",
            },
            {
                "chunk_id": "archived",
                "status": "archived",
                "text_clean": "Проезд оплачивает организатор.",
                "forum_normalized": "Машук",
                "category": "форумы",
            },
            {
                "chunk_id": "grant",
                "status": "published",
                "text_clean": "Грантовые средства возвращаются по письму.",
                "category": "гранты",
            },
        ]
    )

    chunks = retriever.retrieve(
        "кто оплачивает проезд",
        {"forum_normalized": "Машук", "category": "форумы"},
        top_k=5,
    )

    assert [chunk.chunk_id for chunk in chunks] == ["travel"]


def test_seed_retriever_filters_published_chunks_by_source_type() -> None:
    retriever = SeedRetriever(
        [
            {
                "chunk_id": "yonote_registration",
                "status": "published",
                "source_type": "yonote",
                "text_clean": "Регистрация на форум открыта на платформе.",
                "category": "форумы",
            },
            {
                "chunk_id": "xlsx_registration",
                "status": "published",
                "source_type": "xlsx",
                "text_clean": "Регистрация на форум открыта на платформе.",
                "category": "форумы",
            },
        ]
    )

    chunks = retriever.retrieve(
        "регистрация на форум",
        {"category": "форумы", "source_type": "yonote"},
        top_k=5,
    )

    assert [chunk.chunk_id for chunk in chunks] == ["yonote_registration"]


def test_seed_retriever_matches_and_canonicalizes_yonote_forum_variant() -> None:
    retriever = SeedRetriever(
        [
            {
                "chunk_id": "ostrova_program",
                "status": "published",
                "text_clean": "Программа форума ОстроVа будет опубликована перед началом.",
                "forum": "ОстроVа",
                "forum_normalized": "ОстроVа",
                "category": "форумы",
            }
        ]
    )

    chunks = retriever.retrieve(
        "программа форума",
        {"forum_normalized": "Островa", "category": "форумы"},
        top_k=1,
    )

    assert [chunk.chunk_id for chunk in chunks] == ["ostrova_program"]
    assert chunks[0].metadata["forum_normalized"] == "Островa"
    assert chunks[0].metadata["forum_source_value"] == "ОстроVа"


def test_seed_retriever_boosts_intent_examples() -> None:
    retriever = SeedRetriever(
        [
            {
                "chunk_id": "transfer",
                "status": "published",
                "text_clean": "Шаттлы будут ходить от метро.",
                "category": "форумы",
                "intent_examples": ["как добраться на трансфере"],
            },
            {
                "chunk_id": "generic",
                "status": "published",
                "text_clean": "Организационная информация придёт на почту.",
                "category": "форумы",
            },
        ]
    )

    chunks = retriever.retrieve("как добраться на трансфере", {"category": "форумы"}, top_k=1)

    assert chunks[0].chunk_id == "transfer"


def test_seed_retriever_boosts_exact_intent_metadata() -> None:
    retriever = SeedRetriever(
        [
            {
                "chunk_id": "children_registration",
                "status": "published",
                "text_clean": (
                    "Р—Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°С‚СЊ СѓС‡Р°СЃС‚РЅРёРєР° "
                    "РјРѕР¶РЅРѕ РЅР° СЃР°Р№С‚Рµ."
                ),
                "category": "С„РѕСЂСѓРјС‹",
                "intent_name": "Р РµРіРёСЃС‚СЂР°С†РёСЏ РґРµС‚РµР№",
            },
            {
                "chunk_id": "event_registration",
                "status": "published",
                "text_clean": "Р—Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°С‚СЊСЃСЏ РјРѕР¶РЅРѕ РЅР° СЃР°Р№С‚Рµ.",
                "category": "С„РѕСЂСѓРјС‹",
                "intent_name": "Р РµРіРёСЃС‚СЂР°С†РёСЏ РЅР° РјРµСЂРѕРїСЂРёСЏС‚РёРµ",
            },
        ]
    )

    chunks = retriever.retrieve(
        "Р РµРіРёСЃС‚СЂР°С†РёСЏ РЅР° РјРµСЂРѕРїСЂРёСЏС‚РёРµ",
        {"category": "С„РѕСЂСѓРјС‹"},
        top_k=1,
    )

    assert chunks[0].chunk_id == "event_registration"
