from __future__ import annotations

import re
from time import perf_counter

from src.graph.state import BotState

SOURCE_RE = re.compile(r"[ \t]*\[src:[^\]]+\][ \t]*")
TRAILING_LINE_SPACE_RE = re.compile(r"[ \t]+\n")
LEADING_LINE_SPACE_RE = re.compile(r"\n[ \t]+")
EXCESSIVE_BLANK_LINES_RE = re.compile(r"\n{3,}")
TONE_REPLACEMENTS = {
    "вы": "ты",
    "вам": "тебе",
    "вас": "тебя",
    "ваш": "твой",
    "ваша": "твоя",
    "ваше": "твоё",
    "ваши": "твои",
    "вашем": "твоём",
    "вашей": "твоей",
    "вашему": "твоему",
    "вашего": "твоего",
    "ваших": "твоих",
    "можете": "можешь",
    "сможете": "сможешь",
    "ожидайте": "ожидай",
    "приезжайте": "приезжай",
    "сообщите": "сообщи",
    "выберите": "выбери",
    "перейдите": "перейди",
    "авторизуйтесь": "авторизуйся",
    "нажмите": "нажми",
    "заполните": "заполни",
    "укажите": "укажи",
    "свяжитесь": "свяжись",
    "направьте": "направь",
    "обратитесь": "обратись",
    "изучите": "изучи",
    "проверьте": "проверь",
    "напишите": "напиши",
}
TONE_REPLACEMENTS.update(
    {
        "давайте": "давай",
        "выйдите": "выйди",
        "войдите": "войди",
        "отмените": "отмени",
        "повторите": "повтори",
        "убедитесь": "убедись",
        "используете": "используешь",
        "хотите": "хочешь",
        "приезжаете": "приезжаешь",
        "добираетесь": "добираешься",
        "оплачиваете": "оплачиваешь",
        "получаете": "получаешь",
        "запрашиваете": "запрашиваешь",
        "участвуете": "участвуешь",
        "становитесь": "становишься",
        "являетесь": "являешься",
    }
)
TONE_RE = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in TONE_REPLACEMENTS) + r")\b",
    flags=re.IGNORECASE,
)
PROTECTED_TOKEN_RE = re.compile(
    r"""
    (?:
        [A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}
        |
        https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+
        |
        (?<!@)\b(?:[A-Za-z0-9-]+\.)+(?:ru|рф|com|org|net|gov|su|io|ai|cloud)\b
        (?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
DOMAIN_SPACE_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9-]*)\s*\.\s*"
    r"(ru|рф|com|org|net|gov|su|io|ai|cloud)\b",
    flags=re.IGNORECASE,
)
EMAIL_SPACE_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)@\s*"
    r"([A-Za-z0-9-]+(?:\s*\.\s*[A-Za-z0-9-]+)+)\b",
    flags=re.IGNORECASE,
)
TIME_SPACE_RE = re.compile(r"\b(\d{1,2}):\s+(\d{2})\b")
URL_QUERY_SPACE_RE = re.compile(r"(?<=[A-Za-z0-9_/])\?\s+(?=[A-Za-z0-9_=&%-])")
WHITESPACE_RE = re.compile(r"\s+")
TONE_PHRASE_REPLACEMENTS = (
    (
        re.compile(r"\bты создали ещё один аккаунт\b", flags=re.IGNORECASE),
        "у тебя появился ещё один аккаунт",
    ),
    (
        re.compile(r"\bты используете\b", flags=re.IGNORECASE),
        "ты используешь",
    ),
    (
        re.compile(r"\bты хотите\b", flags=re.IGNORECASE),
        "ты хочешь",
    ),
)


async def respond(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or state.get("final_response") or ""
    final = normalize_final_response(response)
    if tracer:
        tracer.add("respond", int((perf_counter() - started_at) * 1000))
    return {"final_response": final}


def normalize_final_response(response: str) -> str:
    return _normalize_tone_to_ty(_normalize_spacing(_strip_source_markers(response)))


def _strip_source_markers(response: str) -> str:
    without_markers = SOURCE_RE.sub("", response)
    without_markers = TRAILING_LINE_SPACE_RE.sub("\n", without_markers)
    without_markers = LEADING_LINE_SPACE_RE.sub("\n", without_markers)
    without_markers = EXCESSIVE_BLANK_LINES_RE.sub("\n\n", without_markers)
    return "\n".join(line.rstrip() for line in without_markers.strip().splitlines())


def _normalize_tone_to_ty(response: str) -> str:
    def replace(match: re.Match[str]) -> str:
        source = match.group(0)
        replacement = TONE_REPLACEMENTS[source.casefold()]
        if source[:1].isupper():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    normalized = TONE_RE.sub(replace, response)
    for pattern, replacement in TONE_PHRASE_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _normalize_spacing(response: str) -> str:
    response = _repair_structured_token_spacing(response)
    protected, tokens = _protect_structured_tokens(response)
    protected = re.sub(r"(?<=[.!?])(?=\S)", " ", protected)
    protected = re.sub(
        r"(?<=[\w\u0410-\u042f\u0430-\u044f\u0401\u0451]):(?=[^\s/\d])",
        ": ",
        protected,
    )
    response = _restore_structured_tokens(protected, tokens)
    response = _repair_structured_token_spacing(response)
    response = re.sub(r"[ \t]{2,}", " ", response)
    return response


def _repair_structured_token_spacing(response: str) -> str:
    previous = None
    repaired = response
    while previous != repaired:
        previous = repaired
        repaired = DOMAIN_SPACE_RE.sub(r"\1.\2", repaired)
    repaired = EMAIL_SPACE_RE.sub(
        lambda match: match.group(1) + "@" + WHITESPACE_RE.sub("", match.group(2)),
        repaired,
    )
    repaired = TIME_SPACE_RE.sub(r"\1:\2", repaired)
    return URL_QUERY_SPACE_RE.sub("?", repaired)


def _protect_structured_tokens(response: str) -> tuple[str, dict[str, str]]:
    tokens: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        suffix = ""
        while token.endswith((".", ",")) and not _ends_with_known_tld(token):
            suffix = token[-1] + suffix
            token = token[:-1]
        placeholder = f"__STRUCTURED_TOKEN_{len(tokens)}__"
        tokens[placeholder] = token
        return placeholder + suffix

    return PROTECTED_TOKEN_RE.sub(replace, response), tokens


def _restore_structured_tokens(response: str, tokens: dict[str, str]) -> str:
    for placeholder, token in tokens.items():
        response = response.replace(placeholder, token)
    return response


def _ends_with_known_tld(token: str) -> bool:
    normalized = token.casefold()
    return normalized.endswith(
        (
            ".ru",
            ".рф",
            ".com",
            ".org",
            ".net",
            ".gov",
            ".su",
            ".io",
            ".ai",
            ".cloud",
        )
    )
