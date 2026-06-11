from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.models import Chunk

TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", flags=re.IGNORECASE)


@dataclass(frozen=True)
class SeedDocument:
    record: dict[str, Any]
    tokens: list[str]
    term_counts: Counter[str]


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
                metadata=document.record,
                score=score,
            )
            for score, document in scored[:top_k]
        ]

    def _build_document(self, record: dict[str, Any]) -> SeedDocument:
        text_parts = [
            str(record.get("text_clean") or record.get("text") or ""),
            _repeat(record.get("intent_name"), 4),
            _repeat(record.get("forum_normalized"), 4),
            _repeat(record.get("source_category"), 2),
            _repeat(record.get("category"), 2),
            _repeat(record.get("topic"), 2),
            _repeat(" ".join(str(item) for item in record.get("intent_examples") or []), 2),
        ]
        tokens = tokenize(" ".join(part for part in text_parts if part))
        return SeedDocument(record=record, tokens=tokens, term_counts=Counter(tokens))

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
        return score

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
    for key in ("forum_normalized", "category", "topic"):
        expected = filters.get(key)
        if expected is None:
            continue
        if isinstance(expected, list):
            if record.get(key) not in expected:
                return False
        elif record.get(key) != expected:
            return False
    return True


def _repeat(value: Any, times: int) -> str:
    if not value:
        return ""
    return " ".join([str(value)] * times)
