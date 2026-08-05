from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.kb.forum_registry import canonicalize_forum_name, forums_are_equivalent
from src.models import Chunk

TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", flags=re.IGNORECASE)


@dataclass(frozen=True)
class SeedDocument:
    record: dict[str, Any]
    tokens: list[str]
    term_counts: Counter[str]
    intent_tokens: set[str]
    topic_tokens: set[str]
    example_tokens: set[str]
    source_tokens: set[str]


class SeedRetriever:
    """Lightweight lexical retriever for local KB QA before ML indexing."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = [record for record in records if record.get("status") == "published"]
        self.documents = [self._build_document(record) for record in self.records]
        self.avg_doc_len = (
            sum(len(document.tokens) for document in self.documents) / len(self.documents)
            if self.documents
            else 0.0
        )
        self.idf = self._compute_idf(self.documents)

    @classmethod
    def from_path(cls, path: Path) -> SeedRetriever:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def retrieve(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> list[Chunk]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scored: list[tuple[float, SeedDocument]] = []
        for document in self.documents:
            if not _matches_filters(document.record, filters or {}):
                continue
            score = self._score(query_tokens, document)
            if score > 0:
                scored.append((score, document))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            Chunk(
                chunk_id=str(document.record["chunk_id"]),
                text=str(document.record.get("text_clean") or document.record.get("text") or ""),
                metadata=_canonicalize_record_forum(document.record),
                score=score,
            )
            for score, document in scored[:top_k]
        ]

    def _build_document(self, record: dict[str, Any]) -> SeedDocument:
        intent_text = str(record.get("intent_name") or "")
        topic_text = str(record.get("topic") or "").replace("_", " ")
        examples_text = " ".join(str(item) for item in record.get("intent_examples") or [])
        source_text = " ".join(
            str(record.get(key) or "")
            for key in ("source_category", "category", "forum_normalized")
        )
        text_parts = [
            str(record.get("text_clean") or record.get("text") or ""),
            _repeat(intent_text, 4),
            _repeat(record.get("forum_normalized"), 4),
            _repeat(record.get("source_category"), 2),
            _repeat(record.get("category"), 2),
            _repeat(topic_text, 2),
            _repeat(examples_text, 2),
        ]
        tokens = tokenize(" ".join(part for part in text_parts if part))
        return SeedDocument(
            record=record,
            tokens=tokens,
            term_counts=Counter(tokens),
            intent_tokens=set(tokenize(intent_text)),
            topic_tokens=set(tokenize(topic_text)),
            example_tokens=set(tokenize(examples_text)),
            source_tokens=set(tokenize(source_text)),
        )

    def _score(self, query_tokens: list[str], document: SeedDocument) -> float:
        k1 = 1.5
        b = 0.75
        doc_len = len(document.tokens)
        if doc_len == 0 or self.avg_doc_len == 0:
            return 0.0

        score = 0.0
        for token in query_tokens:
            term_frequency = document.term_counts.get(token, 0)
            if term_frequency == 0:
                continue
            denominator = term_frequency + k1 * (1 - b + b * doc_len / self.avg_doc_len)
            score += self.idf.get(token, 0.0) * (term_frequency * (k1 + 1)) / denominator
        return score + _field_bonus(set(query_tokens), document)

    @staticmethod
    def _compute_idf(documents: list[SeedDocument]) -> dict[str, float]:
        total = len(documents)
        document_frequency: Counter[str] = Counter()
        for document in documents:
            document_frequency.update(set(document.tokens))
        return {
            token: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in document_frequency.items()
        }


def tokenize(text: str) -> list[str]:
    normalized = text.casefold().replace("ё", "е")
    return TOKEN_RE.findall(normalized)


def _matches_filters(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    if filters.get("status") and record.get("status") != filters["status"]:
        return False
    source_type = filters.get("source_type")
    if source_type and record.get("source_type") != source_type:
        return False
    for key in ("forum_normalized", "category", "topic"):
        expected = filters.get(key)
        if expected is None:
            continue
        if key == "forum_normalized":
            candidates = expected if isinstance(expected, list) else [expected]
            if not any(
                forums_are_equivalent(str(record.get(key) or ""), str(candidate))
                for candidate in candidates
            ):
                return False
            continue
        if isinstance(expected, list):
            if record.get(key) not in expected:
                return False
        elif record.get(key) != expected:
            return False
    return True


def _canonicalize_record_forum(record: dict[str, Any]) -> dict[str, Any]:
    source_forum = str(record.get("forum_normalized") or "").strip()
    canonical_forum = canonicalize_forum_name(source_forum)
    if not canonical_forum or canonical_forum == source_forum:
        return record
    normalized = dict(record)
    normalized["forum_source_value"] = source_forum
    normalized["forum_normalized"] = canonical_forum
    if normalized.get("forum"):
        normalized["forum"] = canonical_forum
    return normalized


def _repeat(value: Any, times: int) -> str:
    if not value:
        return ""
    return " ".join([str(value)] * times)


def _field_bonus(query_tokens: set[str], document: SeedDocument) -> float:
    return (
        3.0 * _coverage(query_tokens, document.intent_tokens)
        + 2.0 * _coverage(query_tokens, document.topic_tokens)
        + 1.5 * _coverage(query_tokens, document.example_tokens)
        + 0.5 * _coverage(query_tokens, document.source_tokens)
    )


def _coverage(query_tokens: set[str], field_tokens: set[str]) -> float:
    if not query_tokens or not field_tokens:
        return 0.0
    return len(query_tokens & field_tokens) / len(query_tokens)
