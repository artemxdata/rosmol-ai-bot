from __future__ import annotations

import re

LIVE_PERSON_RE = re.compile(
    r"\bжив(?:ой|ого|ым)?\s+человек(?:а|ом)?\b",
    re.IGNORECASE | re.UNICODE,
)

TARGET_MARKERS = ("оператор", "специалист", "сотрудник", "поддержк")
EMPLOYMENT_MARKERS = (
    "ваканс",
    "работ",
    "трудоустр",
    "устроиться",
    "стать",
    "побыть",
    "резюме",
    "карьер",
)
ACTION_MARKERS = (
    "хочу",
    "нужен",
    "нужна",
    "нужно",
    "можно",
    "перевед",
    "соедин",
    "свяж",
    "поговор",
    "позов",
    "передай",
    "передайте",
    "позови",
    "позовите",
    "жду",
    "ожида",
)


def is_operator_request(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().replace("ё", "е").split())
    if not normalized:
        return False
    if LIVE_PERSON_RE.search(normalized):
        return True
    if not any(marker in normalized for marker in TARGET_MARKERS):
        return False
    if any(marker in normalized for marker in EMPLOYMENT_MARKERS):
        return False
    return any(marker in normalized for marker in ACTION_MARKERS)
