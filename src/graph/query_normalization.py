from __future__ import annotations

import re

GOSUSLUGI_SHORT_RE = re.compile(r"(?<![0-9a-zа-яё])гу(?![0-9a-zа-яё])", re.IGNORECASE)
PERSONAL_CABINET_SHORT_RE = re.compile(
    r"(?<![0-9a-zа-яё])лк(?![0-9a-zа-яё])",
    re.IGNORECASE,
)


def expand_query_aliases(text: str) -> str:
    expanded = str(text or "")
    expanded = GOSUSLUGI_SHORT_RE.sub("госуслуги", expanded)
    expanded = PERSONAL_CABINET_SHORT_RE.sub("личный кабинет", expanded)
    normalized = expanded.casefold().replace("ё", "е")
    aliases: list[str] = []
    if "парол" in normalized and any(
        marker in normalized for marker in ("восстанов", "забыл", "сброс", "помен")
    ):
        aliases.append("восстановить пароль восстановление пароля личный кабинет")
    if "рекоменд" in normalized and "студент" in normalized:
        aliases.append("рекомендации студенты студенческие сообщества")
    if "грант" in normalized and "подать" in normalized and "заявк" in normalized:
        grant_application_alias = "гранты для физических лиц подать заявку на участие"
        if grant_application_alias not in normalized:
            aliases.append(grant_application_alias)
    if "письмо" in normalized and any(marker in normalized for marker in ("вызов", "регион")):
        aliases.append("письмо-вызов письмо вызов официальное подтверждение участия")
    if aliases:
        expanded = f"{expanded} {' '.join(aliases)}"
    return expanded
