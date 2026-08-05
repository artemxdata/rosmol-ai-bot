from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from eval.ask_cases import seed_smoke_query, select_balanced_records
from scripts.analyze_ticket_dataset import private_id_hash
from src.config import get_settings
from src.rag.embedder import Embedder
from src.rag.retriever import Retriever
from src.rag.seed_retriever import SeedRetriever

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DATA_ROOT = (PROJECT_ROOT / "data" / "private").resolve()
PRIVATE_CANDIDATE_AUDIT_SCHEMA_VERSION = "private-yonote-candidate-audit-v1"


def compute_recall_at_k(results: list[dict[str, Any]]) -> float | None:
    return compute_recall(results)


def compute_recall(results: list[dict[str, Any]], cutoff: int | None = None) -> float | None:
    scored = [item for item in results if item["expected_chunk_ids"]]
    if not scored:
        return None
    hits = sum(
        bool(set(_retrieved_for_cutoff(item, cutoff)) & set(item["expected_chunk_ids"]))
        for item in scored
    )
    return hits / len(scored)


def rank_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in results if item["expected_chunk_ids"]]
    ranks = [item["expected_rank"] for item in scored if item.get("expected_rank") is not None]
    reciprocal_ranks = [1 / int(rank) for rank in ranks]
    rank_counts = Counter(int(rank) for rank in ranks)
    return {
        "hits": len(ranks),
        "misses": len(scored) - len(ranks),
        "mrr": _average(reciprocal_ranks),
        "avg_expected_rank": _average([float(rank) for rank in ranks]),
        "expected_rank_histogram": {
            str(rank): rank_counts[rank] for rank in sorted(rank_counts)
        },
    }


def _normalize_case(raw: dict[str, Any]) -> dict[str, Any]:
    query = raw.get("query") or raw.get("question") or raw.get("text")
    if not query:
        raise ValueError("golden case must contain query, question, or text")

    expected = (
        raw.get("expected_chunk_ids")
        or raw.get("expected_chunks")
        or raw.get("relevant_chunk_ids")
        or []
    )
    if isinstance(expected, str):
        expected = [expected]

    filters = dict(raw.get("filters") or {})
    for key in ("forum_normalized", "category", "topic"):
        if raw.get(key) and key not in filters:
            filters[key] = raw[key]

    return {
        "id": raw.get("id") or raw.get("case_id") or query,
        "query": str(query),
        "filters": filters,
        "expected_chunk_ids": [str(chunk_id) for chunk_id in expected],
    }


async def run_eval(
    golden_path: Path,
    output_path: Path,
    top_k: int,
    backend: str = "qdrant",
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    auto_smoke_cases: bool = False,
    max_smoke_cases: int = 100,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    golden_raw = await asyncio.to_thread(_read_json, golden_path)
    cases = [_normalize_case(item) for item in golden_raw]
    generated_smoke_cases = False
    seed_records: list[dict[str, Any]] | None = None
    if not cases and auto_smoke_cases:
        seed_records = await asyncio.to_thread(_read_json, kb_seed_path)
        cases = build_seed_smoke_cases(seed_records, max_cases=max_smoke_cases)
        generated_smoke_cases = True
    if not cases:
        metrics = {
            "recall_at_k": None,
            "recall_at_1": None,
            "recall_at_3": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "top_k": top_k,
            "backend": backend,
            "cases_total": 0,
            "cases_scored": 0,
            "generated_smoke_cases": False,
            "message": "golden_set is empty",
        }
        await asyncio.to_thread(_write_json, output_path, metrics)
        if markdown_path:
            await asyncio.to_thread(_write_markdown, markdown_path, metrics)
        return metrics

    results: list[dict[str, Any]] = []
    if backend == "lexical":
        if seed_records is None:
            seed_records = await asyncio.to_thread(_read_json, kb_seed_path)
        seed_retriever = SeedRetriever(seed_records)
        for case in cases:
            chunks = seed_retriever.retrieve(case["query"], case["filters"], top_k=top_k)
            results.append(_case_result(case, [chunk.chunk_id for chunk in chunks]))
    elif backend == "qdrant":
        settings = get_settings()
        qdrant = AsyncQdrantClient(url=settings.qdrant_url)
        retriever = Retriever(qdrant, Embedder())
        try:
            for case in cases:
                chunks = await retriever.retrieve(case["query"], case["filters"], top_k=top_k)
                results.append(_case_result(case, [chunk.chunk_id for chunk in chunks]))
        finally:
            await qdrant.close()
    else:
        raise ValueError("backend must be qdrant or lexical")

    recall = compute_recall_at_k(results)
    recall_cutoffs = {
        f"recall_at_{cutoff}": compute_recall(results, cutoff)
        for cutoff in (1, 3, 5, 10)
        if top_k >= cutoff
    }
    metrics = {
        "recall_at_k": recall,
        "recall_at_5": recall_cutoffs.get("recall_at_5"),
        "recall_at_10": recall_cutoffs.get("recall_at_10"),
        "top_k": top_k,
        "backend": backend,
        "cases_total": len(cases),
        "cases_scored": sum(bool(item["expected_chunk_ids"]) for item in results),
        "generated_smoke_cases": generated_smoke_cases,
        "results": results,
    }
    metrics.update(recall_cutoffs)
    metrics.update(rank_summary(results))
    await asyncio.to_thread(_write_json, output_path, metrics)
    if markdown_path:
        await asyncio.to_thread(_write_markdown, markdown_path, metrics)
    return metrics


def run_private_yonote_candidate_audit(
    ticket_jsonl_path: Path,
    output_path: Path,
    *,
    kb_seed_path: Path = Path("data/knowledge_base_seed.json"),
    top_k: int = 10,
    private_root: Path = PRIVATE_DATA_ROOT,
) -> dict[str, Any]:
    """Build user-only published-Yonote candidates without claiming qrels."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    source_path = _private_path(
        ticket_jsonl_path,
        private_root=private_root,
        label="private ticket input",
        must_exist=True,
    )
    destination = _private_path(
        output_path,
        private_root=private_root,
        label="private candidate output",
        must_exist=False,
    )
    if source_path.suffix.casefold() != ".jsonl":
        raise ValueError("private ticket input must be JSONL")
    if destination.suffix.casefold() != ".json":
        raise ValueError("private candidate output must be JSON")
    if destination.exists():
        raise FileExistsError("private candidate output already exists")

    ticket_records = _read_private_ticket_jsonl(source_path)
    seed_payload = _read_json(kb_seed_path)
    if not isinstance(seed_payload, list):
        raise ValueError("KB seed must be a JSON array")
    yonote_records = [
        record
        for record in seed_payload
        if isinstance(record, dict)
        and record.get("status") == "published"
        and record.get("source_type") == "yonote"
    ]
    retriever = SeedRetriever(yonote_records)
    source_filter = {"status": "published", "source_type": "yonote"}

    results: list[dict[str, Any]] = []
    user_turns_total = 0
    multi_turn_cases = 0
    cases_with_candidates = 0
    union_candidate_counts: list[int] = []
    for ticket in ticket_records:
        user_turns = ticket["user_turns"]
        user_turns_total += len(user_turns)
        multi_turn_cases += len(user_turns) >= 2
        cumulative_turns: list[str] = []
        turn_results: list[dict[str, Any]] = []
        union_by_id: dict[str, dict[str, Any]] = {}
        for turn_index, turn_text in enumerate(user_turns, start=1):
            cumulative_turns.append(turn_text)
            query = "\n".join(cumulative_turns)
            chunks = retriever.retrieve(query, source_filter, top_k=top_k)
            candidates: list[dict[str, Any]] = []
            for rank, chunk in enumerate(chunks, start=1):
                if (
                    chunk.metadata.get("status") != "published"
                    or chunk.metadata.get("source_type") != "yonote"
                ):
                    raise RuntimeError("candidate escaped the published Yonote source universe")
                score = round(float(chunk.score), 12)
                candidates.append(
                    {"chunk_id": chunk.chunk_id, "rank": rank, "score": score}
                )
                current = union_by_id.get(chunk.chunk_id)
                if current is None:
                    union_by_id[chunk.chunk_id] = {
                        "chunk_id": chunk.chunk_id,
                        "best_rank": rank,
                        "best_score": score,
                        "first_turn_index": turn_index,
                        "turn_hits": 1,
                    }
                else:
                    current["best_rank"] = min(int(current["best_rank"]), rank)
                    current["best_score"] = max(float(current["best_score"]), score)
                    current["turn_hits"] = int(current["turn_hits"]) + 1
            turn_results.append(
                {
                    "turn_index": turn_index,
                    "query_sha256": _text_sha256(query),
                    "candidates": candidates,
                }
            )

        union_candidates = sorted(
            union_by_id.values(),
            key=lambda item: (
                int(item["best_rank"]),
                -float(item["best_score"]),
                int(item["first_turn_index"]),
                str(item["chunk_id"]),
            ),
        )
        cases_with_candidates += bool(union_candidates)
        union_candidate_counts.append(len(union_candidates))
        results.append(
            {
                "ticket_id_hash": private_id_hash(ticket["ticket_id"]),
                "user_turns_count": len(user_turns),
                "turns": turn_results,
                "union_candidates": union_candidates,
            }
        )

    report = {
        "schema_version": PRIVATE_CANDIDATE_AUDIT_SCHEMA_VERSION,
        "candidate_semantics": "provisional_source_candidates_not_qrels",
        "qrels_status": "not_available",
        "metrics_status": "unscored",
        "query_contract": "ordered-user-turns-cumulative-v1",
        "dialogue_limit": "bot-turn interleaving unavailable; user-only context",
        "ignored_input_fields": [
            "bot_turns",
            "category",
            "channel",
            "closed_without_operator",
            "counted_in_conversion",
            "forum",
            "is_substantive",
            "topic",
            "was_escalated",
        ],
        "source_filter": source_filter,
        "source_file_sha256": _file_sha256(source_path),
        "kb_seed_sha256": _file_sha256(kb_seed_path),
        "eval_tool_sha256": _file_sha256(Path(__file__)),
        "seed_retriever_sha256": _file_sha256(
            PROJECT_ROOT / "src" / "rag" / "seed_retriever.py"
        ),
        "kb_seed_records_total": len(seed_payload),
        "published_yonote_records_total": len(yonote_records),
        "top_k": top_k,
        "cases_total": len(results),
        "user_turns_total": user_turns_total,
        "multi_turn_cases": multi_turn_cases,
        "cases_with_candidates": cases_with_candidates,
        "avg_union_candidates": (
            round(sum(union_candidate_counts) / len(union_candidate_counts), 6)
            if union_candidate_counts
            else None
        ),
        "results": results,
    }
    _write_json(destination, report)
    return report


def build_seed_smoke_cases(
    records: list[dict[str, Any]],
    max_cases: int = 100,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    selected = select_balanced_records(records, max_cases=max_cases, per_forum_limit=3)
    for record in selected:
        query = seed_smoke_query(record)
        filters: dict[str, Any] = {}
        if record.get("category"):
            filters["category"] = record["category"]
        if record.get("forum_normalized"):
            filters["forum_normalized"] = record["forum_normalized"]
        cases.append(
            {
                "id": f"seed_smoke::{record['chunk_id']}",
                "query": query,
                "filters": filters,
                "expected_chunk_ids": [str(record["chunk_id"])],
            }
        )
    return cases


def _case_result(case: dict[str, Any], retrieved_ids: list[str]) -> dict[str, Any]:
    expected_rank = _expected_rank(retrieved_ids, case["expected_chunk_ids"])
    return {
        "id": case["id"],
        "query": case["query"],
        "filters": case["filters"],
        "expected_chunk_ids": case["expected_chunk_ids"],
        "retrieved_chunk_ids": retrieved_ids,
        "expected_rank": expected_rank,
        "hit": bool(set(retrieved_ids) & set(case["expected_chunk_ids"])),
    }


def _expected_rank(retrieved_ids: list[str], expected_ids: list[str]) -> int | None:
    expected = set(expected_ids)
    for index, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in expected:
            return index
    return None


def _retrieved_for_cutoff(item: dict[str, Any], cutoff: int | None) -> list[str]:
    retrieved = item["retrieved_chunk_ids"]
    if cutoff is None:
        return retrieved
    return retrieved[:cutoff]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_private_ticket_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    ticket_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"private ticket line {line_number} must be an object")
            ticket_id = raw.get("ticket_id")
            if not isinstance(ticket_id, str) or not ticket_id.strip():
                raise ValueError(f"private ticket line {line_number} has no ticket_id")
            ticket_id = ticket_id.strip()
            if ticket_id in ticket_ids:
                raise ValueError(f"duplicate ticket_id at line {line_number}")
            ticket_ids.add(ticket_id)
            user_turns = raw.get("user_turns")
            if (
                not isinstance(user_turns, list)
                or not user_turns
                or any(
                    not isinstance(turn, str) or not turn.strip() for turn in user_turns
                )
            ):
                raise ValueError(
                    f"private ticket line {line_number} user_turns must contain "
                    "non-empty strings"
                )
            records.append(
                {
                    "ticket_id": ticket_id,
                    "user_turns": [turn.strip() for turn in user_turns],
                }
            )
    return records


def _private_path(
    path: Path,
    *,
    private_root: Path,
    label: str,
    must_exist: bool,
) -> Path:
    root = private_root.resolve(strict=True)
    candidate = path.resolve(strict=must_exist)
    if not candidate.is_relative_to(root):
        raise ValueError(f"{label} must stay under data/private")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(path: Path, metrics: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    top_k = metrics.get("top_k")
    recall = metrics.get("recall_at_k")
    lines = [
        "# Retrieval Eval Report",
        "",
        f"- Backend: `{metrics.get('backend')}`",
        f"- Cases: `{metrics.get('cases_total')}`",
        f"- Scored cases: `{metrics.get('cases_scored')}`",
        f"- Recall@{top_k}: `{_format_rate(recall)}`",
        f"- Recall@5: `{_format_rate(metrics.get('recall_at_5'))}`",
        f"- Recall@10: `{_format_rate(metrics.get('recall_at_10'))}`",
        f"- MRR: `{_format_float(metrics.get('mrr'))}`",
        f"- Avg expected rank: `{_format_float(metrics.get('avg_expected_rank'))}`",
        f"- Generated smoke cases: `{metrics.get('generated_smoke_cases')}`",
    ]

    misses = [item for item in metrics.get("results", []) if not item.get("hit")]
    if misses:
        lines.extend(["", "## Missed Cases", ""])
        for item in misses[:20]:
            expected = ", ".join(item.get("expected_chunk_ids") or [])
            retrieved = ", ".join(item.get("retrieved_chunk_ids") or [])
            lines.append(
                f"- `{item.get('id')}` expected=`{expected}` retrieved=`{retrieved}`"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _format_float(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.4f}"


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default="data/golden_set.json")
    parser.add_argument("--output", default="eval/metrics.json")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--backend", choices=["qdrant", "lexical"], default="qdrant")
    parser.add_argument("--kb-seed", default="data/knowledge_base_seed.json")
    parser.add_argument("--auto-smoke-cases", action="store_true")
    parser.add_argument("--max-smoke-cases", type=int, default=100)
    parser.add_argument(
        "--private-ticket-jsonl",
        default="",
        help=(
            "Build unscored user-only Yonote candidates from a private ticket JSONL; "
            "requires --backend lexical and an output under data/private"
        ),
    )
    args = parser.parse_args()

    if args.private_ticket_jsonl:
        if args.backend != "lexical":
            parser.error("--private-ticket-jsonl requires --backend lexical")
        if args.auto_smoke_cases or args.markdown:
            parser.error("private candidate mode does not support smoke cases or markdown")
        report = run_private_yonote_candidate_audit(
            Path(args.private_ticket_jsonl),
            Path(args.output),
            kb_seed_path=Path(args.kb_seed),
            top_k=args.top_k,
        )
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "results"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    metrics = asyncio.run(
        run_eval(
            Path(args.golden),
            Path(args.output),
            args.top_k,
            backend=args.backend,
            kb_seed_path=Path(args.kb_seed),
            auto_smoke_cases=args.auto_smoke_cases,
            max_smoke_cases=args.max_smoke_cases,
            markdown_path=Path(args.markdown) if args.markdown else None,
        )
    )
    print(
        json.dumps(
            {key: value for key, value in metrics.items() if key != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
