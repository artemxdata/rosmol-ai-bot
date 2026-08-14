from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.graph.answer_plan as answer_plan_module
import src.graph.nodes.generate as generate_module
from scripts.check_fact_card_oracle import ForbiddenLLM
from scripts.check_fact_pipeline_offline import AsyncSeedRetriever, LexicalReranker
from src.graph.answer_plan import plan_answer
from src.graph.nodes.analyze import analyze_query
from src.graph.nodes.generate import generate
from src.graph.nodes.guard import apply_response_guards
from src.graph.nodes.rerank import rerank
from src.graph.nodes.retrieve import retrieve
from src.kb.fact_cards import compose_fact_cards
from src.kb.fact_extractor import KnowledgeAspect, infer_source_aspects
from src.kb.forum_registry import forums_are_equivalent
from src.llm.routing import estimate_routing_hint
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
BODY_FALLBACK_ASPECTS = frozenset(
    {
        KnowledgeAspect.ACCESSIBILITY,
        KnowledgeAspect.ACCOMMODATION,
        KnowledgeAspect.DOCUMENTS,
        KnowledgeAspect.ELIGIBILITY,
        KnowledgeAspect.FOOD,
        KnowledgeAspect.PROGRAM,
        KnowledgeAspect.TRANSFER,
        KnowledgeAspect.TRAVEL,
    }
)


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


def _body_fallback_probe_cases() -> list[tuple[str, KnowledgeAspect, set[str]]]:
    cases: list[tuple[str, KnowledgeAspect, set[str]]] = []
    for aspect in BODY_FALLBACK_ASPECTS:
        template = QUERY_TEMPLATES[aspect]
        for forum in FORUMS:
            chunks = _chunks_for_forum(forum)
            body_only_ids = {
                chunk.chunk_id
                for chunk in chunks
                if aspect in infer_source_aspects(chunk.metadata, chunk.text)
                and aspect not in infer_source_aspects(chunk.metadata, "")
            }
            if not body_only_ids:
                continue
            query = template.format(forum=forum)
            draft = compose_fact_cards(
                query,
                chunks,
                category="форумы",
                forum_normalized=forum,
            )
            if draft is None or not body_only_ids.intersection(draft.cited_sources):
                continue
            cases.append((query, aspect, body_only_ids))
            break
    return cases


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


@pytest.mark.asyncio
async def test_body_discovered_forum_facts_survive_the_full_offline_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _body_fallback_probe_cases()
    assert {aspect for _query, aspect, _ids in cases} == BODY_FALLBACK_ASPECTS

    plan_builds = 0
    original_plan_builder = answer_plan_module.build_query_proven_topic_plan

    def count_plan_builds(*args, **kwargs):
        nonlocal plan_builds
        plan_builds += 1
        return original_plan_builder(*args, **kwargs)

    def reject_private_plan_rebuild(*_args, **_kwargs):
        raise AssertionError("generation must consume the frozen answer plan")

    monkeypatch.setattr(
        answer_plan_module,
        "build_query_proven_topic_plan",
        count_plan_builds,
    )
    monkeypatch.setattr(
        generate_module,
        "build_query_proven_topic_plan",
        reject_private_plan_rebuild,
    )

    seed_by_id = {str(row["chunk_id"]): row for row in SEED}
    retriever = AsyncSeedRetriever(SEED)
    reranker = LexicalReranker()
    for query, aspect, body_only_ids in cases:
        llm = ForbiddenLLM()
        analyzed = await analyze_query(
            {
                "message": query,
                "message_masked": query,
                "routing_hint": estimate_routing_hint(query).model_dump(),
                "llm_client": llm,
            }
        )
        state = {
            "message": query,
            "message_masked": query,
            "contextual_message": analyzed.get("contextual_message", query),
            "analysis": analyzed["analysis"],
            "llm_client": llm,
            "retriever": retriever,
            "reranker": reranker,
        }
        state.update(plan_answer(state))
        state.update(await retrieve(state))
        state.update(await rerank(state))
        calls_before_generation = llm.calls
        generated = await generate(state)
        guarded = await apply_response_guards({**state, **generated})
        result = {**generated, **guarded}
        cited = [str(chunk_id) for chunk_id in result.get("cited_sources") or []]

        assert llm.calls == calls_before_generation, aspect.value
        assert result.get("generator_model") == "source_chunk", aspect.value
        assert body_only_ids.intersection(cited), aspect.value
        assert cited, aspect.value
        assert all(
            seed_by_id[chunk_id].get("source_type") == "yonote"
            and seed_by_id[chunk_id].get("source") == "yonote_api"
            and seed_by_id[chunk_id].get("version") == "yonote-api-v1"
            and seed_by_id[chunk_id].get("status") == "published"
            for chunk_id in cited
        )
    assert plan_builds == len(cases)
