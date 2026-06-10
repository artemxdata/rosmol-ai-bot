from __future__ import annotations

import re
from dataclasses import dataclass, field

PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
PASSPORT_RE = re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)")


@dataclass
class PIIMasker:
    _natasha_ready: bool = False
    _segmenter: object | None = None
    _ner_tagger: object | None = None
    _load_error: Exception | None = None
    placeholders: dict[str, str] = field(
        default_factory=lambda: {
            "phone": "[ТЕЛЕФОН]",
            "email": "[EMAIL]",
            "passport": "[ДОКУМЕНТ]",
            "date": "[ДАТА]",
            "name": "[ИМЯ]",
        }
    )

    def mask(self, text: str) -> tuple[str, dict[str, list[str]]]:
        masked = text
        mapping: dict[str, list[str]] = {key: [] for key in self.placeholders}

        for key, regex in (
            ("email", EMAIL_RE),
            ("phone", PHONE_RE),
            ("passport", PASSPORT_RE),
            ("date", DATE_RE),
        ):
            masked, found = self._mask_regex(masked, regex, self.placeholders[key])
            mapping[key].extend(found)

        masked, names = self._mask_names(masked)
        mapping["name"].extend(names)
        return masked, {key: value for key, value in mapping.items() if value}

    def _mask_regex(
        self,
        text: str,
        regex: re.Pattern[str],
        placeholder: str,
    ) -> tuple[str, list[str]]:
        found: list[str] = []

        def replace(match: re.Match[str]) -> str:
            found.append(match.group(0))
            return placeholder

        return regex.sub(replace, text), found

    def _ensure_natasha(self) -> bool:
        if self._natasha_ready:
            return True
        if self._load_error:
            return False
        try:
            from natasha import NewsEmbedding, NewsNERTagger, Segmenter

            self._segmenter = Segmenter()
            self._ner_tagger = NewsNERTagger(NewsEmbedding())
            self._natasha_ready = True
            return True
        except Exception as exc:
            self._load_error = exc
            return False

    def _mask_names(self, text: str) -> tuple[str, list[str]]:
        if not self._ensure_natasha():
            return text, []

        from natasha import Doc

        doc = Doc(text)
        doc.segment(self._segmenter)
        doc.tag_ner(self._ner_tagger)

        names = [span.text for span in doc.spans if span.type == "PER"]
        masked = text
        for name in sorted(set(names), key=len, reverse=True):
            masked = masked.replace(name, self.placeholders["name"])
        return masked, names
