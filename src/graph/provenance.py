from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.models import Chunk, ScoredChunk

PROVENANCE_SCHEMA_VERSION = "question-pipeline-provenance-v2"

MAX_PROVENANCE_QUESTIONS = 12
MAX_PROVENANCE_ATTEMPTS = 6
MAX_PROVENANCE_CANDIDATES = 48
MAX_PROVENANCE_CHUNK_IDS = 64
MAX_PROVENANCE_FILTER_ATTEMPTS = 24
MAX_PROVENANCE_SOURCE_IDS = 64

_SAFE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_FILTER_KEYS = ("source_type", "category", "topic", "forum_normalized")
_ALLOWLISTED_SOURCE_TYPES = frozenset({"yonote"})


def question_id(index: int) -> str:
    """Return a stable request-local question identifier without retaining its text."""

    if index < 0:
        raise ValueError("question index must be non-negative")
    return f"q{index + 1}"


def safe_filter(filters: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep only non-sensitive KB scope fingerprints in trace provenance.

    ``category``, ``topic`` and ``forum_normalized`` may originate in model output,
    so their raw values must never be copied into telemetry.  Source type is retained
    only when it is a contract-controlled allowlisted value; every other value is
    represented by the same fixed-size fingerprint used by :func:`filter_scope`.
    """

    if not isinstance(filters, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in _FILTER_KEYS:
        value = _normalized_filter_value(filters.get(key))
        if not value:
            continue
        normalized_source_type = value.casefold()
        if key == "source_type" and normalized_source_type in _ALLOWLISTED_SOURCE_TYPES:
            result[key] = normalized_source_type
        else:
            result[key] = f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
    return result


def filter_scope(
    candidate: Mapping[str, Any] | None,
    strict: Mapping[str, Any] | None,
) -> str:
    """Classify a retrieval attempt without copying query text into telemetry."""

    current = safe_filter(candidate)
    original = safe_filter(strict)
    if current == original:
        return "strict"
    if original.get("topic") and not current.get("topic"):
        if original.get("forum_normalized") == current.get("forum_normalized"):
            return "relaxed_topic"
    if original.get("category") and not current.get("category"):
        if original.get("forum_normalized") == current.get("forum_normalized"):
            return "relaxed_category"
    if original.get("forum_normalized") and not current.get("forum_normalized"):
        return "relaxed_forum"
    return "relaxed_global"


def chunk_ids(chunks: Iterable[Chunk]) -> list[str]:
    rows, _ = chunk_id_batch(chunks)
    return rows


def chunk_id_batch(
    chunks: Iterable[Chunk],
    *,
    limit: int = MAX_PROVENANCE_CHUNK_IDS,
) -> tuple[list[str], dict[str, int]]:
    """Return bounded ordered chunk IDs and explicit truncation counters."""

    if limit < 0:
        raise ValueError("provenance chunk ID limit must be non-negative")
    rows: list[str] = []
    seen: set[str] = set()
    total = 0
    for chunk in chunks:
        chunk_id = _telemetry_chunk_id(getattr(chunk, "chunk_id", None))
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        total += 1
        if len(rows) < limit:
            rows.append(chunk_id)
    return rows, truncation_counts(total=total, recorded=len(rows), label="chunk_ids")


def chunk_candidates(chunks: Iterable[Chunk], *, method: str) -> list[dict[str, Any]]:
    rows, _ = chunk_candidate_batch(((chunks, method),))
    return rows


def chunk_candidate_batch(
    sources: Sequence[tuple[Iterable[Chunk], str]],
    *,
    limit: int = MAX_PROVENANCE_CANDIDATES,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return a bounded candidate list without retaining query or chunk text."""

    if limit < 0:
        raise ValueError("provenance candidate limit must be non-negative")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    allowed_methods = {"metadata", "hybrid", "keyword", "shared_hybrid", "shared_keyword"}
    for chunks, method in sources:
        if method not in allowed_methods:
            raise ValueError(f"unsupported provenance retrieval method: {method}")
        for chunk in chunks:
            chunk_id = _telemetry_chunk_id(getattr(chunk, "chunk_id", None))
            identity = (method, chunk_id)
            if not chunk_id or identity in seen:
                continue
            seen.add(identity)
            total += 1
            if len(rows) >= limit:
                continue
            row: dict[str, Any] = {"chunk_id": chunk_id, "method": method}
            score = finite_score(getattr(chunk, "score", None))
            if score is not None:
                row["score"] = score
            rows.append(row)
    return rows, truncation_counts(total=total, recorded=len(rows), label="candidates")


def finite_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def rerank_question_provenance(
    retrieval_provenance: Sequence[Mapping[str, Any]] | None,
    reranked_chunks: Sequence[ScoredChunk],
) -> list[dict[str, Any]]:
    """Project global reranker output back onto the recorded question candidate sets."""

    reranked_by_id = {
        chunk_id: finite_score(chunk.reranker_score)
        for chunk in reranked_chunks
        if (chunk_id := _telemetry_chunk_id(chunk.chunk_id))
    }
    rows: list[dict[str, Any]] = []
    eligible_total = 0
    reported_question_total = 0
    for raw in retrieval_provenance or ():
        if not isinstance(raw, Mapping):
            continue
        reported_question_total = max(
            reported_question_total,
            _safe_non_negative_int(raw.get("questions_total")),
        )
        qid = str(raw.get("question_id") or "").strip()
        if not (qid == "shared" or re.fullmatch(r"q[1-9][0-9]*", qid)):
            continue
        eligible_total += 1
        if len(rows) >= MAX_PROVENANCE_QUESTIONS:
            continue
        input_ids, input_total = bounded_id_sequence(
            raw.get("retrieved_chunk_ids"),
            limit=MAX_PROVENANCE_CHUNK_IDS,
        )
        output_ids = [chunk_id for chunk_id in input_ids if chunk_id in reranked_by_id]
        rows.append(
            {
                "schema_version": PROVENANCE_SCHEMA_VERSION,
                "question_id": qid,
                "input_chunk_ids": input_ids,
                **truncation_counts(
                    total=max(
                        input_total,
                        _safe_non_negative_int(raw.get("retrieved_chunk_ids_total")),
                    ),
                    recorded=len(input_ids),
                    label="input_chunk_ids",
                ),
                "output_chunks": [
                    {
                        "chunk_id": chunk_id,
                        **(
                            {"score": reranked_by_id[chunk_id]}
                            if reranked_by_id[chunk_id] is not None
                            else {}
                        ),
                    }
                    for chunk_id in output_ids
                ],
                "dropped_chunk_ids": [
                    chunk_id for chunk_id in input_ids if chunk_id not in reranked_by_id
                ],
            }
        )
    if rows:
        rows[0].update(
            truncation_counts(
                total=max(eligible_total, reported_question_total),
                recorded=len(rows),
                label="questions",
            )
        )
    return rows


def source_selection_provenance(
    retrieval_provenance: Sequence[Mapping[str, Any]] | None,
    selected_source_ids: Sequence[str] | None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    """Record only coarse per-question candidate overlap.

    The generator selects sources globally and does not expose a claim-to-question
    binding.  Intersection with a question's retrieval candidates is therefore useful
    diagnostic evidence, but it must never be represented as exact source attribution.
    """

    selected, _ = bounded_id_sequence(
        selected_source_ids,
        limit=MAX_PROVENANCE_SOURCE_IDS,
    )
    rows: list[dict[str, Any]] = []
    uncovered: list[str] = []
    eligible_total = 0
    reported_question_total = 0
    for raw in retrieval_provenance or ():
        if not isinstance(raw, Mapping):
            continue
        reported_question_total = max(
            reported_question_total,
            _safe_non_negative_int(raw.get("attributable_questions_total")),
        )
        qid = str(raw.get("question_id") or "").strip()
        if not re.fullmatch(r"q[1-9][0-9]*", qid):
            continue
        eligible_total += 1
        if len(rows) >= MAX_PROVENANCE_QUESTIONS:
            continue
        candidates = set(
            bounded_id_sequence(
                raw.get("retrieved_chunk_ids"),
                limit=MAX_PROVENANCE_CHUNK_IDS,
            )[0]
        )
        matches = [chunk_id for chunk_id in selected if chunk_id in candidates]
        rows.append(
            {
                "question_id": qid,
                "binding_scope": "candidate_overlap_coarse_unattributed",
                "candidate_overlap_source_ids": matches,
            }
        )
        if not matches:
            uncovered.append(qid)
    stats = truncation_counts(
        total=max(eligible_total, reported_question_total),
        recorded=len(rows),
        label="question_overlaps",
    )
    return rows, uncovered, stats


def safe_reason(value: Any, *, default: str) -> str:
    reason = str(value or "").strip().casefold()
    return reason if _SAFE_REASON_RE.fullmatch(reason) else default


def bounded_id_sequence(
    value: Any,
    *,
    limit: int,
) -> tuple[list[str], int]:
    """Return bounded, de-duplicated telemetry IDs plus their pre-truncation count."""

    if limit < 0:
        raise ValueError("provenance ID limit must be non-negative")
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return [], 0
    rows: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in value:
        chunk_id = _telemetry_chunk_id(item)
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        total += 1
        if len(rows) < limit:
            rows.append(chunk_id)
    return rows, total


def truncation_counts(*, total: int, recorded: int, label: str) -> dict[str, int]:
    safe_total = max(0, int(total))
    safe_recorded = min(safe_total, max(0, int(recorded)))
    return {
        f"{label}_total": safe_total,
        f"{label}_recorded": safe_recorded,
        f"{label}_truncated_count": safe_total - safe_recorded,
    }


def _normalized_filter_value(value: Any) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    return " ".join(normalized.split())


def _telemetry_chunk_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    chunk_id = value.strip()
    if not chunk_id:
        return ""
    if len(chunk_id) <= 256 and not any(ord(character) < 32 for character in chunk_id):
        return chunk_id
    digest = hashlib.sha256(chunk_id.encode("utf-8")).hexdigest()
    return f"id_sha256:{digest}"


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0
