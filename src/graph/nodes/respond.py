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
}
TONE_REPLACEMENTS.update(
    {
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


async def respond(state: BotState) -> dict:
    started_at = perf_counter()
    tracer = state.get("trace")
    response = state.get("generated_response") or state.get("final_response") or ""
    final = _normalize_tone_to_ty(_normalize_spacing(_strip_source_markers(response)))
    if tracer:
        tracer.add("respond", int((perf_counter() - started_at) * 1000))
    return {"final_response": final}


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

    return TONE_RE.sub(replace, response)


def _normalize_spacing(response: str) -> str:
    response = re.sub(r"(?<=[.!?])(?=\S)", " ", response)
    response = re.sub(r"(?<=[\w\u0410-\u042f\u0430-\u044f\u0401\u0451]):(?=[^\s/])", ": ", response)
    response = re.sub(r"[ \t]{2,}", " ", response)
    return response
