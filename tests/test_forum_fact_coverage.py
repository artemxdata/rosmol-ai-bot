from __future__ import annotations

import json
from pathlib import Path

from src.kb.fact_cards import compose_fact_cards
from src.kb.fact_extractor import KnowledgeAspect
from src.kb.forum_registry import forums_are_equivalent
from src.models import ScoredChunk

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads(
    (ROOT / "data" / "knowledge_base_seed.json").read_text(encoding="utf-8")
)
REGISTRY = json.loads(
    (ROOT / "data" / "forums_registry.json").read_text(encoding="utf-8")
)
FORUMS = tuple(
    str(item.get("normalized") or item.get("name") or "").strip()
    for item in REGISTRY
    if isinstance(item, dict)
    and str(item.get("normalized") or item.get("name") or "").strip()
)

QUERY_TEMPLATES = {
    KnowledgeAspect.DATES: "Когда проходит форум «{forum}»?",
    KnowledgeAspect.REGISTRATION: "Как подать заявку на форум «{forum}»?",
    KnowledgeAspect.LOCATION: "Где проходит форум «{forum}»?",
    KnowledgeAspect.PROGRAM: "Какая программа форума «{forum}»?",
    KnowledgeAspect.ELIGIBILITY: "Кто может участвовать в форуме «{forum}»?",
    KnowledgeAspect.TRAVEL: "Кто оплачивает проезд на форум «{forum}»?",
    KnowledgeAspect.TRANSFER: "Будет ли трансфер на форум «{forum}»?",
    KnowledgeAspect.ACCOMMODATION: "Как организовано проживание на форуме «{forum}»?",
    KnowledgeAspect.FOOD: "Как организовано питание на форуме «{forum}»?",
    KnowledgeAspect.DOCUMENTS: "Какие документы нужны на форум «{forum}»?",
    KnowledgeAspect.ACCESSIBILITY: "Есть ли условия для участников с ОВЗ на форуме «{forum}»?",
}
MINIMUM_DIRECT_COVERAGE = {
    "accessibility": 7,
    "accommodation": 20,
    "dates": 24,
    "documents": 13,
    "eligibility": 18,
    "food": 20,
    "location": 23,
    "program": 18,
    "registration": 24,
    "transfer": 18,
    "travel": 16,
}


def _chunks_for_forum(forum: str) -> list[ScoredChunk]:
    return [
        ScoredChunk(
            chunk_id=str(row["chunk_id"]),
            text=str(row.get("text_clean") or ""),
            metadata={
                key: value
                for key, value in row.items()
                if key not in {"text_clean", "text_raw"}
            },
            score=0.9,
            reranker_score=0.9,
        )
        for row in SEED
        if isinstance(row, dict)
        and str(row.get("status") or "").casefold() == "published"
        and str(row.get("source_type") or "").casefold() == "yonote"
        and forums_are_equivalent(row.get("forum_normalized"), forum)
    ]


def _direct_answer_coverage() -> dict[str, int]:
    coverage = {aspect.value: 0 for aspect in QUERY_TEMPLATES}
    for forum in FORUMS:
        chunks = _chunks_for_forum(forum)
        for aspect, template in QUERY_TEMPLATES.items():
            query = template.format(forum=forum)
            draft = compose_fact_cards(
                query,
                chunks,
                category="форумы",
                forum_normalized=forum,
            )
            if draft is not None and aspect in draft.requested_aspects:
                coverage[aspect.value] += 1
    return coverage


def test_published_forum_facts_keep_broad_direct_answer_coverage() -> None:
    coverage = _direct_answer_coverage()

    assert {
        aspect: coverage[aspect] >= minimum
        for aspect, minimum in MINIMUM_DIRECT_COVERAGE.items()
    } == dict.fromkeys(MINIMUM_DIRECT_COVERAGE, True), json.dumps(
        coverage,
        ensure_ascii=True,
        sort_keys=True,
    )
