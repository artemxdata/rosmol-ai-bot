from __future__ import annotations

import re

_HOMOGLYPHS = str.maketrans(
    {
        "a": "а",
        "c": "с",
        "e": "е",
        "o": "о",
        "p": "р",
        "x": "х",
        "y": "у",
    }
)

PROFANITY_RE = re.compile(
    r"\b(?:"
    r"бля(?:д|т)?\w*|бле(?:ать|дь|ть)\w*|"
    r"сук(?:а|и|у|ой|е|ам|ами|ах|ин)\w*|"
    r"суч(?:ар|к(?:а|и|у|ой|е)|ий)\w*|"
    r"(?:на|по|за|о|об|при|у|вы)?ху(?:й|я|е|ю|и|ли|ево|ев)\w*|"
    r"пизд\w*|"
    r"(?:за|на|по|про|у|вы|въ|до|пере|от|раз|под|при|об)?[её]б\w*|"
    r"долбо[её]б\w*|мудак\w*|мудил\w*|"
    r"говн\w*|гавн\w*|задолб\w*|"
    r"мраз\w*|твар\w*|урод\w*|чмо\w*|шлюх\w*|"
    r"г[ао]ндон\w*|пид[ао]р\w*|жоп\w*|засран\w*|"
    r"fuck\w*|motherfuck\w*|bitch\w*|"
    r"(?:na)?hu(?:y|i|j)\w*|blya(?:t|d)?\w*|suka\w*|"
    r"mudak\w*|pizd\w*|(?:y)?ebat\w*|pidor\w*|gandon\w*"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

HOSTILE_PHRASE_RE = re.compile(
    r"\b(?:заткнись|сдохни|пош[её]л\s+вон|иди\s+лесом)\b",
    re.IGNORECASE | re.UNICODE,
)

OBFUSCATED_PROFANITY_RE = re.compile(
    r"(?:^|[\W_])(?:"
    r"(?:н[\W_]+а[\W_]+)?х[\W_]+у[\W_]+(?:й|я|е|ю|и)|"
    r"х[\W_]+й|"
    r"п[\W_]+и[\W_]+з[\W_]+д|"
    r"[её][\W_]+б[\W_]+(?:а|у|и|е|н|л)|"
    r"б[\W_]+л[\W_]+я"
    r")(?:$|[\W_])",
    re.IGNORECASE | re.UNICODE,
)


def check(text: str) -> bool:
    raw = " ".join(str(text or "").casefold().replace("ё", "е").split())
    normalized = _normalize(raw)
    return bool(
        PROFANITY_RE.search(raw)
        or PROFANITY_RE.search(normalized)
        or HOSTILE_PHRASE_RE.search(normalized)
        or OBFUSCATED_PROFANITY_RE.search(normalized)
    )


def _normalize(text: str) -> str:
    normalized = str(text or "").casefold().replace("ё", "е")
    normalized = normalized.translate(_HOMOGLYPHS)
    return " ".join(normalized.split())
