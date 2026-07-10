from __future__ import annotations

import re

PROFANITY_RE = re.compile(
    r"\b(?:"
    r"бля(?:д|т)?\w*|"
    r"сук(?:а|и|у|ой|ин|он)?\w*|"
    r"ху(?:й|я|е|ё|ю|и)\w*|"
    r"пизд\w*|"
    r"(?:за|на|по|про|у|вы|въ|до|пере|от|раз|под|при|об)?[её]б\w*|"
    r"долбо[её]б\w*|мудак\w*|мудил\w*|"
    r"говн\w*|гавн\w*|задолб\w*"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def check(text: str) -> bool:
    return bool(PROFANITY_RE.search(text))
