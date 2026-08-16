from __future__ import annotations

import re
from time import perf_counter

from src.graph.state import BotState
from src.models import Complexity
from src.response_contract import contains_emoji_like_symbols, get_response_contract

SOURCE_RE = re.compile(r"[ \t]*\[src:[^\]]+\][ \t]*")
URL_RE = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)
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
        (?<!\d)\d{1,2}[./]\d{1,2}[./]\d{4}(?!\d)
        |
        (?-i:
            [\u0410-\u042f\u0401A-Z][\u0410-\u044f\u0401\u0451A-Za-z0-9-]{1,63}\.
            [\u0410-\u042f\u0401A-Z][\u0410-\u044f\u0401\u0451A-Za-z0-9-]{1,63}
        )
        |
        [A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}
        |
        https?://(?:
            [A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+
            |
            (?:[А-Яа-яЁё0-9-]+\.)+рф
            (?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?
        )
        |
        (?<!@)\b(?:[\w-]+\.)+(?:ru|рф|com|org|net|gov|su|io|ai|me|cloud)\b
        (?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
DOMAIN_SPACE_RE = re.compile(
    r"\b([\w][\w-]*)\s*\.\s*"
    r"(ru|рф|com|org|net|gov|su|io|ai|me|cloud)\b",
    flags=re.IGNORECASE,
)
BARE_DOMAIN_RE = re.compile(
    r"(?<![@/\w.-])"
    r"(?P<url>(?:[\w-]+\.)+(?:ru|рф|com|org|net|gov|su|io|ai|me|cloud)"
    r"(?::\d{1,5})?(?:/[^\s<>()]*)?)",
    flags=re.IGNORECASE,
)
EMAIL_SPACE_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)@\s*"
    r"([A-Za-z0-9-]+(?:\s*\.\s*[A-Za-z0-9-]+)+)\b",
    flags=re.IGNORECASE,
)
TIME_SPACE_RE = re.compile(r"\b(\d{1,2}):\s+(\d{2})\b")
DATE_SPACE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*([./])\s*(\d{1,2})\s*([./])\s*(\d{4})(?!\d)"
)
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
_RESPONSE_CONTRACT = get_response_contract()
_OPERATOR_TRANSFER_RESPONSE = _RESPONSE_CONTRACT.message(
    "operator_transfer"
).select_text()
_CATALOG_EMOJI_TEXTS = frozenset(
    text
    for message in _RESPONSE_CONTRACT.messages
    if message.allowed_emojis
    for text in message.user_facing_texts
)


async def respond(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or state.get("final_response") or ""
    final = normalize_final_response(response)
    violation = _final_response_contract_violation(final, state)
    if violation:
        if tracer:
            tracer.add(
                "respond",
                int((perf_counter() - started_at) * 1000),
                response_guard=state.get("response_guard"),
                contract_violation=violation,
            )
        return {
            "final_response": _OPERATOR_TRANSFER_RESPONSE,
            "should_escalate": True,
            "escalation_reason": f"final_response_{violation}",
        }
    if tracer:
        tracer.add(
            "respond",
            int((perf_counter() - started_at) * 1000),
            response_guard=state.get("response_guard"),
        )
    return {"final_response": final}


def _final_response_contract_violation(
    response: str,
    state: BotState,
) -> str | None:
    if not response:
        return "empty"
    if len(response) > _final_response_limit(state):
        return "too_long"
    if len(URL_RE.findall(response)) > _RESPONSE_CONTRACT.composition.max_source_links:
        return "too_many_links"
    if contains_emoji_like_symbols(response) and response not in _CATALOG_EMOJI_TEXTS:
        clarification_template = _RESPONSE_CONTRACT.message(
            "clarification_with_options"
        ).template
        clarification_prefix = str(clarification_template or "").split("{options}", 1)[0]
        if not clarification_prefix or not response.startswith(clarification_prefix):
            return "unapproved_emoji"
    return None


def _final_response_limit(state: BotState) -> int:
    analysis = state.get("analysis")
    complexity = getattr(analysis, "complexity", None)
    questions = getattr(analysis, "questions", None) or []
    if complexity == Complexity.COMPLEX or len(questions) > 1:
        return _RESPONSE_CONTRACT.limits.compound_max_chars
    return _RESPONSE_CONTRACT.limits.simple_max_chars


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
    response = _prefix_bare_domains(response)
    response = re.sub(r"[ \t]{2,}", " ", response)
    return response


def _prefix_bare_domains(response: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        suffix = ""
        while url.endswith((".", ",", ";", ":", "!", "?")):
            suffix = url[-1] + suffix
            url = url[:-1]
        return f"https://{url}{suffix}"

    return BARE_DOMAIN_RE.sub(replace, response)


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
    repaired = DATE_SPACE_RE.sub(r"\1\2\3\4\5", repaired)
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
            ".me",
            ".cloud",
        )
    )
