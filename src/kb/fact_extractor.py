from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class KnowledgeAspect(StrEnum):
    """Stable domain aspects shared by planning, retrieval and composition.

    Yonote headings are deliberately not part of the public graph contract: they
    change whenever editors rename a section.  The bounded aspect set is the
    semantic layer between a user request and those mutable source headings.
    """

    OVERVIEW = "overview"
    REGISTRATION = "registration"
    ELIGIBILITY = "eligibility"
    DATES = "dates"
    SHIFTS = "shifts"
    LOCATION = "location"
    PROGRAM = "program"
    RESULTS = "results"
    STATUS = "status"
    CONFIRMATION = "confirmation"
    ACCOUNT_ACCESS = "account_access"
    ACCOUNT_DELETION = "account_deletion"
    NAVIGATION = "navigation"
    TECHNICAL = "technical"
    TICKET = "ticket"
    TRAVEL = "travel"
    TRANSFER = "transfer"
    ACCOMMODATION = "accommodation"
    FOOD = "food"
    DOCUMENTS = "documents"
    MEDICAL = "medical"
    ACCESSIBILITY = "accessibility"
    CANCELLATION = "cancellation"
    CONTACTS = "contacts"
    GRANT_NOMINATIONS = "grant_nominations"
    GRANT_AGREEMENT = "grant_agreement"
    GRANT_REPORT = "grant_report"
    VOLUNTEERING = "volunteering"
    CERTIFICATE = "certificate"
    CHAT = "chat"
    INVITATION = "invitation"
    DRESS_CODE = "dress_code"
    CHILDREN = "children"
    FOREIGN_PARTICIPATION = "foreign_participation"


@dataclass(frozen=True)
class FactUnit:
    text: str
    ordinal: int
    line_ordinal: int
    is_list_item: bool = False


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", flags=re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|"
    r"\b\d{1,2}\s*(?:[-–—]\s*\d{1,2}\s*)?"
    r"(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|"
    r"июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*))",
    flags=re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+\b")
_LIST_RE = re.compile(r"^\s*(?:[-*•▪◦–—]|\d+[.)])\s+")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ0-9«„“])")

_STOPWORDS = frozenset(
    {
        "без",
        "был",
        "была",
        "были",
        "будет",
        "вам",
        "вас",
        "весь",
        "вот",
        "где",
        "для",
        "его",
        "если",
        "есть",
        "или",
        "как",
        "какая",
        "какие",
        "какой",
        "когда",
        "кто",
        "мне",
        "может",
        "можно",
        "мой",
        "надо",
        "наш",
        "него",
        "она",
        "они",
        "под",
        "при",
        "про",
        "так",
        "там",
        "тебе",
        "только",
        "уже",
        "что",
        "это",
    }
)

# A small, versioned ontology replaces hundreds of event-specific topic pairs.
# Each row describes a domain concept, never a particular forum.
_QUERY_MARKERS: dict[KnowledgeAspect, tuple[str, ...]] = {
    KnowledgeAspect.OVERVIEW: (
        "что такое",
        "что за",
        "в двух словах",
        "чем занимается",
        "суть",
        "о форуме",
        "о мероприятии",
        "общий период",
    ),
    KnowledgeAspect.REGISTRATION: (
        "регистрац",
        "зарегистр",
        "подать заяв",
        "подач заяв",
        "как попасть",
        "хочу попасть",
        "вписаться",
        "принять участие",
    ),
    KnowledgeAspect.ELIGIBILITY: (
        "кто может участв",
        "кто может стать",
        "кто вообще может",
        "возраст",
        "возрастн",
        "требования к участник",
        "подхожу ли",
    ),
    KnowledgeAspect.DATES: (
        "когда",
        "дат",
        "период",
        "по состоянию",
        "прием уже закрыт",
        "приём уже закрыт",
        "до какого",
        "крайн",
        "дедлайн",
        "срок",
        "разъезд",
        "отъезд",
        "заезд",
    ),
    KnowledgeAspect.SHIFTS: ("смен", "заезд"),
    KnowledgeAspect.LOCATION: (
        "где проходит",
        "где пройдет",
        "где пройдёт",
        "место провед",
        "площадк",
        "адрес",
        "локац",
    ),
    KnowledgeAspect.PROGRAM: ("программ", "расписан", "артист"),
    KnowledgeAspect.RESULTS: ("результат", "итог", "отбор", "список прошед"),
    KnowledgeAspect.STATUS: (
        "статус",
        "на рассмотрении",
        "одобрен",
        "отклонен",
        "отклонён",
        "резерв",
    ),
    KnowledgeAspect.CONFIRMATION: ("подтвердить участ", "подтверждение участ"),
    KnowledgeAspect.ACCOUNT_ACCESS: (
        "потерял доступ",
        "потеряла доступ",
        "старой почт",
        "восстановить доступ",
        "перенести данн",
        "объединить аккаунт",
    ),
    KnowledgeAspect.ACCOUNT_DELETION: ("удалить аккаунт", "удаление аккаунт"),
    KnowledgeAspect.NAVIGATION: (
        "найти мероприят",
        "поиск мероприят",
        "по регион",
        "фильтр",
        "отфильтр",
    ),
    KnowledgeAspect.TECHNICAL: (
        "неактивн",
        "не работает",
        "ошибк",
        "не груз",
        "не открыв",
        "не нажим",
    ),
    KnowledgeAspect.TICKET: (
        "код билет",
        "получить билет",
        "билет не приш",
        "билет после регистрац",
    ),
    KnowledgeAspect.TRAVEL: ("проезд", "дорог", "компенс", "билет до"),
    KnowledgeAspect.TRANSFER: ("трансфер", "шаттл", "автобус"),
    KnowledgeAspect.ACCOMMODATION: ("прожив", "размещ", "жиль", "гостиниц"),
    KnowledgeAspect.FOOD: ("питан", "еда", "питьев", "вегетари"),
    KnowledgeAspect.DOCUMENTS: ("документ", "паспорт", "справк", "что взять"),
    KnowledgeAspect.MEDICAL: ("медицин", "медпункт", "врач", "здоров"),
    KnowledgeAspect.ACCESSIBILITY: ("овз", "инвалид", "доступн", "сопровождающ"),
    KnowledgeAspect.CANCELLATION: (
        "отказаться",
        "отозвать",
        "отменить",
        "не смогу поехать",
        "не могу поехать",
    ),
    KnowledgeAspect.CONTACTS: ("контакт", "куда написать", "почта поддержки"),
    KnowledgeAspect.GRANT_NOMINATIONS: ("номинац", "направлен грант"),
    KnowledgeAspect.GRANT_AGREEMENT: ("грантового соглашен", "грантовое соглашен"),
    KnowledgeAspect.GRANT_REPORT: (
        "грантового отчет",
        "грантового отчёт",
        "итоговый отчет",
        "итоговый отчёт",
    ),
    KnowledgeAspect.VOLUNTEERING: ("волонтер", "волонтёр"),
    KnowledgeAspect.CERTIFICATE: ("сертификат", "диплом", "грамот"),
    KnowledgeAspect.CHAT: ("добавят в чат", "вступить в чат", "ссылка на чат"),
    KnowledgeAspect.INVITATION: ("письмо-вызов", "письмо вызов", "приглашен"),
    KnowledgeAspect.DRESS_CODE: ("дресс", "одежд"),
    KnowledgeAspect.CHILDREN: ("с ребен", "с ребён", "дет", "ребенк", "ребёнк"),
    KnowledgeAspect.FOREIGN_PARTICIPATION: ("иностран", "другой стран", "не гражданин рф"),
}

_SOURCE_MARKERS: dict[KnowledgeAspect, tuple[str, ...]] = {
    KnowledgeAspect.OVERVIEW: (
        "o_forume",
        "o_meropriyatii",
        "obschaya_informaciya",
        "chto_takoe_",
        "sut_foruma",
        "sut_festivalya",
        "о форуме",
        "о мероприятии",
        "общая информация",
    ),
    KnowledgeAspect.REGISTRATION: (
        "registraci",
        "podacha_zayav",
        "podat_zayav",
        "poshagovyy_algoritm",
        "регистрац",
        "подача заяв",
    ),
    KnowledgeAspect.ELIGIBILITY: (
        "uchastniki",
        "vozrastnye_ogranicheniya",
        "usloviya_uchastiya",
        "участники",
        "возрастные огранич",
    ),
    KnowledgeAspect.DATES: (
        "daty_",
        "daty_nachala",
        "mesto_i_daty",
        "vremya_zaezda",
        "сроки",
        "даты",
    ),
    KnowledgeAspect.SHIFTS: ("smena_", "tematicheskie_smeny", "смена", "смены"),
    KnowledgeAspect.LOCATION: ("mesto_", "ploschadk", "adres", "место проведения", "площадка"),
    KnowledgeAspect.PROGRAM: ("programma_", "raspisanie", "artist", "программа", "расписание"),
    KnowledgeAspect.RESULTS: ("rezultat", "otbor", "spiski", "результаты", "отбор"),
    KnowledgeAspect.STATUS: ("statusy_zayavok", "rezervnyy_spisok", "статусы заявок"),
    KnowledgeAspect.CONFIRMATION: ("podtverzhdenie_uchast", "подтверждение участия"),
    KnowledgeAspect.ACCOUNT_ACCESS: (
        "obedinenie_akkauntov",
        "vosstanovit_parol",
        "объединение аккаунтов",
    ),
    KnowledgeAspect.ACCOUNT_DELETION: ("udalenie_akkaunta", "удаление аккаунта"),
    KnowledgeAspect.NAVIGATION: (
        "poisk_i_navigaciya",
        "поиск и навигация",
        "filtr",
        "ssylka_https_events",
        "events.myrosmol",
        "всероссийские и окружные форумы",
    ),
    KnowledgeAspect.TECHNICAL: ("tehnichesk", "oshib", "неактивна", "неактивн"),
    KnowledgeAspect.TICKET: ("bilet", "youthday_bot", "билет"),
    KnowledgeAspect.TRAVEL: ("oplata_proezda", "kompensaciya", "проезд", "компенсация"),
    KnowledgeAspect.TRANSFER: ("transfer", "трансфер"),
    KnowledgeAspect.ACCOMMODATION: ("prozhiv", "razmesch", "проживание", "размещение"),
    KnowledgeAspect.FOOD: ("pitan", "питание", "питьевой"),
    KnowledgeAspect.DOCUMENTS: ("dokument", "spisok_veschey", "pamyatka", "документы"),
    KnowledgeAspect.MEDICAL: ("medicin", "zdorov", "medpunkt", "медицина"),
    KnowledgeAspect.ACCESSIBILITY: ("uchastniki_s_ovz", "овз", "доступность"),
    KnowledgeAspect.CANCELLATION: ("otkaz_ot_uchastiya", "otmena_registracii", "отказ от участия"),
    KnowledgeAspect.CONTACTS: ("kontakt", "operator", "обратная связь"),
    KnowledgeAspect.GRANT_NOMINATIONS: ("nominacii_grantovyh", "номинации грантовых"),
    KnowledgeAspect.GRANT_AGREEMENT: (
        "proverka_proekta_grantovogo_soglasheniya",
        "проверка проекта грантового соглашения",
    ),
    KnowledgeAspect.GRANT_REPORT: ("proverka_otcheta", "проверка отчета", "проверка отчёта"),
    KnowledgeAspect.VOLUNTEERING: ("volonter", "volont", "волонт"),
    KnowledgeAspect.CERTIFICATE: ("sertifikat", "diplom", "сертификат"),
    KnowledgeAspect.CHAT: ("dobavlenie_v_chat", "добавление в чат"),
    KnowledgeAspect.INVITATION: ("pismo_vyzov", "письмо-вызов"),
    KnowledgeAspect.DRESS_CODE: ("dress_kod", "дресс-код"),
    KnowledgeAspect.CHILDREN: ("registraciya_detey", "poseschenie_festivalya_s_detmi", "дет"),
    KnowledgeAspect.FOREIGN_PARTICIPATION: ("inostrannye_grazhdane", "иностран"),
}

# Yonote editors often keep several FAQ answers inside a broadly named section.
# In that shape the heading is not enough to bind a user aspect to the source,
# even though the published chunk body contains an explicit answer.  These
# markers intentionally require a domain-specific phrase; generic words such as
# ``организатор``, ``доступен`` or ``участник`` are not sufficient on their own.
_SOURCE_BODY_MARKERS: dict[KnowledgeAspect, tuple[str, ...]] = {
    KnowledgeAspect.ELIGIBILITY: (
        "возраст участников",
        "возраст участника",
        "возраст от",
        "в возрасте от",
        "участниками могут стать",
        "к участию приглашаются",
        "требования к участникам",
    ),
    KnowledgeAspect.SHIFTS: (
        "первая смена",
        "вторая смена",
        "третья смена",
        "четвертая смена",
        "четвёртая смена",
        "тематическая смена",
        "смена «",
    ),
    KnowledgeAspect.PROGRAM: (
        "программа форума",
        "программа мероприятия",
        "расписание форума",
        "расписание мероприятия",
        "программа будет опубликована",
    ),
    KnowledgeAspect.RESULTS: (
        "результаты отбора",
        "итоги отбора",
        "результаты будут опубликованы",
        "список прошедших",
        "список победителей",
    ),
    KnowledgeAspect.STATUS: (
        "статус заявки",
        "заявка на рассмотрении",
        "заявка одобрена",
        "заявка отклонена",
        "заявка отклонёна",
        "резервный список",
    ),
    KnowledgeAspect.CONFIRMATION: (
        "подтвердить участие",
        "подтверждение участия",
        "подтвердить свое участие",
        "подтвердить своё участие",
    ),
    KnowledgeAspect.ACCOUNT_ACCESS: (
        "восстановить пароль",
        "восстановление пароля",
        "объединить аккаунты",
        "объединение аккаунтов",
        "не могу войти в аккаунт",
        "не получается войти в аккаунт",
    ),
    KnowledgeAspect.ACCOUNT_DELETION: (
        "удалить аккаунт",
        "удаление аккаунта",
        "удалить учетную запись",
        "удалить учётную запись",
    ),
    KnowledgeAspect.TRAVEL: (
        "проезд",
        "оплата проезда",
        "проезд оплачивает",
        "проезд оплачивается",
        "проезд не оплачивается",
        "компенсация проезда",
        "расходы на проезд",
        "дорога оплачивается",
    ),
    KnowledgeAspect.TRANSFER: (
        "трансфер",
        "шаттл",
        "автобус до места проведения",
        "автобусы до места проведения",
    ),
    KnowledgeAspect.ACCOMMODATION: (
        "проживание",
        "размещение участников",
        "место размещения",
        "гостиница для участников",
        "палаточный лагерь",
    ),
    KnowledgeAspect.FOOD: (
        "питание",
        "прием пищи",
        "приём пищи",
        "питьевая вода",
        "питьёвая вода",
        "вегетарианское меню",
    ),
    KnowledgeAspect.DOCUMENTS: (
        "необходимые документы",
        "список документов",
        "пакет документов",
        "оригинал паспорта",
        "копия паспорта",
        "медицинская справка",
    ),
    KnowledgeAspect.MEDICAL: (
        "медицинская помощь",
        "медицинский пункт",
        "медпункт",
        "медицинская справка",
        "дежурный врач",
    ),
    KnowledgeAspect.ACCESSIBILITY: (
        "участники с овз",
        "участников с овз",
        "доступная среда",
        "для людей с инвалидностью",
        "сопровождающее лицо",
    ),
    KnowledgeAspect.CANCELLATION: (
        "отказаться от участия",
        "отказ от участия",
        "отозвать заявку",
        "отмена регистрации",
        "отменить регистрацию",
    ),
    KnowledgeAspect.CONTACTS: (
        "контакты организаторов",
        "контакт организатора",
        "телефон организаторов",
        "телефон организатора",
        "электронная почта",
        "адрес электронной почты",
    ),
    KnowledgeAspect.VOLUNTEERING: (
        "стать волонтером",
        "стать волонтёром",
        "набор волонтеров",
        "набор волонтёров",
        "волонтерская помощь",
        "волонтёрская помощь",
    ),
    KnowledgeAspect.CERTIFICATE: (
        "получить сертификат",
        "сертификат участника",
        "сертификат волонтера",
        "сертификат волонтёра",
        "диплом участника",
    ),
    KnowledgeAspect.CHAT: (
        "чат участников",
        "добавление в чат",
        "добавят в чат",
        "ссылка на чат",
    ),
    KnowledgeAspect.INVITATION: (
        "письмо-вызов",
        "письмо вызов",
        "письмо-приглашение",
        "официальное приглашение",
    ),
    KnowledgeAspect.DRESS_CODE: (
        "дресс-код",
        "дресс код",
        "форма одежды",
        "требования к одежде",
    ),
    KnowledgeAspect.CHILDREN: (
        "участие детей",
        "регистрация детей",
        "с ребенком",
        "с ребёнком",
        "несовершеннолетние участники",
    ),
    KnowledgeAspect.FOREIGN_PARTICIPATION: (
        "иностранные граждане",
        "иностранные участники",
        "граждане других стран",
        "не гражданин рф",
    ),
}

_ASPECT_UNIT_MARKERS: dict[KnowledgeAspect, tuple[str, ...]] = {
    KnowledgeAspect.OVERVIEW: ("—", "является", "это ", "цель", "созда", "площадк"),
    KnowledgeAspect.REGISTRATION: (
        "зарегистр",
        "подать",
        "заяв",
        "перейти",
        "выбрать",
        "заполнить",
        "ссылк",
    ),
    KnowledgeAspect.ELIGIBILITY: ("участ", "граждан", "возраст", "от ", "до ", "может стать"),
    KnowledgeAspect.DATES: (
        "дат",
        "до ",
        "с ",
        "по ",
        "начал",
        "заверш",
        "заезд",
        "разъезд",
        "отъезд",
    ),
    KnowledgeAspect.SHIFTS: ("смен", "единство", "правда", "родина"),
    KnowledgeAspect.LOCATION: ("адрес", "место", "площадк", "проходит", "состоится"),
    KnowledgeAspect.PROGRAM: ("программ", "расписан", "доступн", "опублик"),
    KnowledgeAspect.RESULTS: ("результат", "списк", "отбор", "известн", "опублик"),
    KnowledgeAspect.STATUS: (
        "статус",
        "на рассмотрении",
        "одобрен",
        "отклонен",
        "отклонён",
        "резерв",
    ),
    KnowledgeAspect.CONFIRMATION: ("подтверд", "зайти", "выбрать", "нажать", "письм"),
    KnowledgeAspect.ACCOUNT_ACCESS: ("аккаунт", "почт", "госуслуг", "id", "перенес", "support@"),
    KnowledgeAspect.ACCOUNT_DELETION: (
        "удал",
        "пользователь",
        "техническая поддержка",
        "не имеет права",
    ),
    KnowledgeAspect.NAVIGATION: ("мероприят", "раздел", "фильтр", "регион", "подраздел"),
    KnowledgeAspect.TECHNICAL: ("ошиб", "неактив", "профил", "настройк", "необходимо"),
    KnowledgeAspect.TICKET: ("билет", "код", "диалог", "почт", "показать"),
    KnowledgeAspect.TRAVEL: ("проезд", "дорог", "билет", "компенс", "оплач"),
    KnowledgeAspect.TRANSFER: ("трансфер", "автобус", "шаттл", "маршрут"),
    KnowledgeAspect.ACCOMMODATION: ("прожив", "размещ", "гостиниц", "палат", "организатор"),
    KnowledgeAspect.FOOD: ("питан", "еда", "питьев", "вода", "организатор"),
    KnowledgeAspect.DOCUMENTS: ("документ", "паспорт", "справк", "взять", "необходимо"),
    KnowledgeAspect.MEDICAL: ("мед", "врач", "здоров", "пункт"),
    KnowledgeAspect.ACCESSIBILITY: ("овз", "инвалид", "сопровож", "доступ"),
    KnowledgeAspect.CANCELLATION: ("отказ", "отозвать", "отмен", "заявк"),
    KnowledgeAspect.CONTACTS: ("контакт", "почт", "телефон", "написать", "обратиться"),
    KnowledgeAspect.GRANT_NOMINATIONS: ("номинац", "тематика", "18", "эксперт"),
    KnowledgeAspect.GRANT_AGREEMENT: ("куратор", "провер", "до ", "дн", "соглашен"),
    KnowledgeAspect.GRANT_REPORT: ("отчет", "отчёт", "провер", "до ", "рабочих дн", "статус"),
    KnowledgeAspect.VOLUNTEERING: ("волонтер", "волонтёр", "фильтр", "заявк", "кликнуть"),
    KnowledgeAspect.CERTIFICATE: ("сертификат", "диплом", "грамот", "личном кабинет", "почт"),
    KnowledgeAspect.CHAT: ("чат", "ссылк", "добав"),
    KnowledgeAspect.INVITATION: ("письмо", "вызов", "приглаш"),
    KnowledgeAspect.DRESS_CODE: ("одежд", "дресс", "обув"),
    KnowledgeAspect.CHILDREN: ("дет", "ребен", "ребён", "родител", "возраст"),
    KnowledgeAspect.FOREIGN_PARTICIPATION: ("иностран", "граждан", "страны", "ссылк"),
}


def infer_query_aspects(text: str) -> frozenset[KnowledgeAspect]:
    normalized = normalize_fact_text(text)
    aspects = {
        aspect
        for aspect, markers in _QUERY_MARKERS.items()
        if any(_has_marker(normalized, marker) for marker in markers)
    }
    if _has_marker(normalized, "удал") and any(
        _has_marker(normalized, marker) for marker in ("аккаунт", "профил")
    ):
        aspects.add(KnowledgeAspect.ACCOUNT_DELETION)
    if any(
        _has_marker(normalized, marker)
        for marker in ("найти", "поиск", "фильтр", "отфильтр")
    ) and _has_marker(normalized, "мероприят"):
        aspects.add(KnowledgeAspect.NAVIGATION)
    if KnowledgeAspect.DATES in aspects and _has_marker(normalized, "заявк"):
        aspects.add(KnowledgeAspect.REGISTRATION)
    if KnowledgeAspect.DATES in aspects and any(
        _has_marker(normalized, marker)
        for marker in ("прием заяв", "прием уже закрыт", "приём уже закрыт")
    ):
        aspects.add(KnowledgeAspect.REGISTRATION)
    if _has_marker(normalized, "шаг") and _has_marker(normalized, "подач"):
        aspects.add(KnowledgeAspect.REGISTRATION)
    if _has_marker(normalized, "форум") and any(
        _has_marker(normalized, marker)
        for marker in ("какие", "сейчас", "есть", "найти")
    ):
        aspects.add(KnowledgeAspect.NAVIGATION)
    if KnowledgeAspect.ACCESSIBILITY in aspects and not any(
        _has_marker(normalized, marker)
        for marker in ("овз", "инвалид", "сопровождающ", "коляск", "сурдоперевод")
    ):
        aspects.discard(KnowledgeAspect.ACCESSIBILITY)
    return frozenset(aspects)


def plan_query_aspects(text: str) -> frozenset[KnowledgeAspect]:
    """Return answer slots rather than every lexical signal in a request."""

    normalized = normalize_fact_text(text)
    aspects = set(infer_query_aspects(text))
    domain_definitions = {
        KnowledgeAspect.GRANT_NOMINATIONS,
    }
    if aspects & domain_definitions:
        aspects.discard(KnowledgeAspect.OVERVIEW)
    if KnowledgeAspect.GRANT_REPORT in aspects:
        aspects.discard(KnowledgeAspect.RESULTS)
    if KnowledgeAspect.CONFIRMATION in aspects and not any(
        _has_marker(normalized, marker)
        for marker in ("что значит статус", "что означает статус", "статусы заявок")
    ):
        aspects.discard(KnowledgeAspect.STATUS)

    temporal_facets = {
        KnowledgeAspect.PROGRAM,
        KnowledgeAspect.RESULTS,
        KnowledgeAspect.GRANT_AGREEMENT,
        KnowledgeAspect.GRANT_REPORT,
    }
    if aspects & temporal_facets:
        aspects.discard(KnowledgeAspect.DATES)
    if (
        KnowledgeAspect.SHIFTS in aspects
        and KnowledgeAspect.DATES in aspects
        and re.search(
            r"\b(?:какие(?:\s+\w+){0,3}|список|названия)\s+смен\w*\b",
            normalized,
        )
        is None
    ):
        aspects.discard(KnowledgeAspect.SHIFTS)
    explicit_navigation = bool(
        (
            any(
                _has_marker(normalized, marker)
                for marker in ("найти", "поиск", "фильтр", "отфильтр")
            )
            and _has_marker(normalized, "мероприят")
        )
        or any(
            _has_marker(normalized, marker)
            for marker in (
                "по регион",
                "какие форумы",
                "что по форум",
                "че по форум",
            )
        )
    )
    if KnowledgeAspect.NAVIGATION in aspects and not explicit_navigation:
        # ``infer_query_aspects`` deliberately has a broad recall rule for
        # phrases such as "какие ... форумы".  At answer-planning time an
        # entity-bound question like "какие смены форума" must not acquire a
        # second, unrelated navigation slot.
        aspects.discard(KnowledgeAspect.NAVIGATION)
    return frozenset(aspects)


def infer_source_aspects(
    metadata: Mapping[str, Any] | None,
    source_text: str = "",
) -> frozenset[KnowledgeAspect]:
    metadata = metadata or {}
    heading = metadata.get("source_heading_path") or []
    if isinstance(heading, (list, tuple)):
        heading_text = " ".join(str(item) for item in heading)
    else:
        heading_text = str(heading or "")
    signature = normalize_fact_text(
        " ".join(
            (
                str(metadata.get("topic") or ""),
                str(metadata.get("intent_name") or ""),
                heading_text,
            )
        ).replace("_", " ")
    )
    raw_topic_signature = str(metadata.get("topic") or "").strip().casefold()
    combined = f"{raw_topic_signature} {signature}"
    aspects = {
        aspect
        for aspect, markers in _SOURCE_MARKERS.items()
        if any(_has_marker(combined, marker) for marker in markers)
    }
    # Some legacy Yonote documents use the exact topic ``forum`` for a
    # registration/deadline card. Treat only that exact topic as registration;
    # the former stem marker also polluted every ``programma_foruma`` heading.
    if raw_topic_signature == "forum":
        aspects.add(KnowledgeAspect.REGISTRATION)
    # A few headings are intentionally broad. Their source body is used only
    # to add a compatible facet, never to invent a new entity or fact.
    source_normalized = normalize_fact_text(source_text)
    aspects.update(
        aspect
        for aspect, markers in _SOURCE_BODY_MARKERS.items()
        if any(_has_marker(source_normalized, marker) for marker in markers)
    )
    if _has_marker(source_normalized, "билет"):
        aspects.add(KnowledgeAspect.TICKET)
    if any(
        _has_marker(source_normalized, marker)
        for marker in (
            "подать заявку",
            "подача заявки",
            "пройти верификацию",
            "зарегистрироваться",
        )
    ):
        aspects.add(KnowledgeAspect.REGISTRATION)
    if any(
        _has_marker(source_normalized, marker)
        for marker in ("ошибк", "неактивн", "не работает")
    ):
        aspects.add(KnowledgeAspect.TECHNICAL)
    if any(
        _has_marker(source_normalized, marker)
        for marker in (
            "адрес",
            "место проведения",
            "расположен",
            "состоится в",
            "пройдет в",
            "пройдёт в",
        )
    ):
        aspects.add(KnowledgeAspect.LOCATION)
    if any(
        _has_marker(source_normalized, marker)
        for marker in (
            "фильтр поиска",
            "фильтры поиска",
            "доступные мероприятия",
            "отсортировать по регионам",
        )
    ):
        aspects.add(KnowledgeAspect.NAVIGATION)
    if _DATE_RE.search(source_text) or _TIME_RE.search(source_text):
        aspects.add(KnowledgeAspect.DATES)
    return frozenset(aspects)


def aspects_are_compatible(
    question_text: str,
    metadata: Mapping[str, Any] | None,
    source_text: str = "",
) -> bool:
    requested = infer_query_aspects(question_text)
    available = infer_source_aspects(metadata, source_text)
    if not requested or not available:
        return False
    if not source_scope_constraints_match(question_text, metadata):
        return False
    generic_requested = {KnowledgeAspect.DATES}
    requested_without_dates = requested - generic_requested
    requested_for_match = (
        requested
        if requested_without_dates in (set(), {KnowledgeAspect.SHIFTS})
        else requested_without_dates
    )
    if requested_for_match & available:
        return True
    compatible_pairs = {
        (KnowledgeAspect.DATES, KnowledgeAspect.REGISTRATION),
        (KnowledgeAspect.REGISTRATION, KnowledgeAspect.VOLUNTEERING),
        (KnowledgeAspect.TRAVEL, KnowledgeAspect.TRANSFER),
        (KnowledgeAspect.FOOD, KnowledgeAspect.ACCOMMODATION),
        (KnowledgeAspect.ACCOMMODATION, KnowledgeAspect.FOOD),
    }
    return any(
        (left, right) in compatible_pairs
        for left in requested_for_match
        for right in available
    )


def source_scope_constraints_match(
    question_text: str,
    metadata: Mapping[str, Any] | None,
) -> bool:
    return _ordinal_constraints_match(question_text, metadata)


def extract_source_fact_excerpts(
    source_text: str,
    question_text: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    max_chars: int = 340,
    max_units: int = 4,
    requested_aspects: frozenset[KnowledgeAspect] | None = None,
) -> list[str]:
    """Select bounded, source-verbatim facts relevant to one question.

    The function neither paraphrases nor supplies missing values.  It is safe to
    use before an LLM and deterministic enough for regression tests.  Returning
    an empty list means that the normal grounded fallback must handle the case.
    """

    if max_chars <= 0 or max_units <= 0:
        return []
    units = source_fact_units(source_text, metadata)
    if not units:
        return []

    requested_aspects = requested_aspects or plan_query_aspects(question_text)
    source_aspects = infer_source_aspects(metadata, source_text)
    if not requested_aspects or not source_aspects:
        return []
    if not source_scope_constraints_match(question_text, metadata):
        return []
    if not requested_aspects & source_aspects:
        return []

    query_tokens = _fact_stems(question_text)
    requested_phrases = _requested_quoted_phrases(question_text)
    scores = [
        _score_fact_unit(
            unit,
            query_tokens=query_tokens,
            requested_aspects=requested_aspects,
            requested_phrases=requested_phrases,
        )
        for unit in units
    ]
    ranked = sorted(
        zip(scores, units, strict=True),
        key=lambda item: (-item[0], item[1].ordinal),
    )
    if not ranked or ranked[0][0] <= 0:
        return []

    desired_units = _desired_unit_count(question_text, requested_aspects, max_units)
    selected: list[FactUnit] = []
    selected_ordinals: set[int] = set()

    for phrase in requested_phrases:
        matching = next(
            (unit for unit in units if phrase in normalize_fact_text(unit.text)),
            None,
        )
        if matching is not None and matching.ordinal not in selected_ordinals:
            selected.append(matching)
            selected_ordinals.add(matching.ordinal)

    for score, unit in ranked:
        if len(selected) >= desired_units:
            break
        if unit.ordinal in selected_ordinals:
            continue
        if selected and score < max(1.0, ranked[0][0] * 0.28):
            continue
        selected.append(unit)
        selected_ordinals.add(unit.ordinal)

    if not selected:
        return []
    selected.sort(key=lambda item: item.ordinal)
    return _fit_fact_units(selected, max_chars=max_chars, max_units=max_units)


def source_fact_units(
    source_text: str,
    metadata: Mapping[str, Any] | None = None,
) -> list[FactUnit]:
    lines = [
        " ".join(line.replace("\u00a0", " ").split())
        for line in str(source_text or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return []

    heading_values = _source_heading_values(metadata)
    units: list[FactUnit] = []
    seen: set[str] = set()
    for line_ordinal, line in enumerate(lines):
        normalized_line = normalize_fact_text(_LIST_RE.sub("", line))
        if (
            line_ordinal == 0
            and normalized_line in heading_values
            and not _heading_is_fact_bearing(line)
        ):
            continue
        if (
            normalized_line in heading_values
            and len(lines) > 1
            and not _heading_is_fact_bearing(line)
        ):
            continue

        is_list_item = bool(_LIST_RE.match(line))
        candidates = [line]
        if not is_list_item and len(line) > 180:
            candidates = [
                candidate.strip()
                for candidate in _SENTENCE_BOUNDARY_RE.split(line)
                if candidate.strip()
            ]
        for candidate in candidates:
            normalized = normalize_fact_text(_LIST_RE.sub("", candidate))
            if not normalized or normalized in seen:
                continue
            if _is_nonfactual_unit(normalized):
                continue
            seen.add(normalized)
            units.append(
                FactUnit(
                    text=candidate.strip(),
                    ordinal=len(units),
                    line_ordinal=line_ordinal,
                    is_list_item=is_list_item,
                )
            )
    return units


def normalize_fact_text(value: Any) -> str:
    normalized = str(value or "").casefold().replace("ё", "е").replace("ë", "е")
    normalized = re.sub(r"[^0-9a-zа-я@._:/+–—-]+", " ", normalized)
    return " ".join(normalized.split())


def semantic_fact_tokens(value: Any) -> frozenset[str]:
    """Return lightweight morphology-normalized terms for source selection."""

    normalized = normalize_fact_text(value)
    terms = set(_fact_stems(normalized))
    ordinal_aliases = (
        (r"\bперв\w*\b", "1"),
        (r"\bвтор\w*\b", "2"),
        (r"\bтрет\w*\b", "3"),
        (r"\bчетверт\w*\b", "4"),
    )
    for pattern, value_alias in ordinal_aliases:
        if re.search(pattern, normalized):
            terms.add(value_alias)
    return frozenset(terms)


def source_answer_signal_score(
    question_text: str,
    aspect: KnowledgeAspect,
    metadata: Mapping[str, Any] | None,
    source_text: str,
) -> int:
    """Score generic evidence that a card answers the requested facet.

    The signals describe reusable answer shapes (procedure, publication time,
    definition), not event names or calibration case IDs.
    """

    del metadata
    question = normalize_fact_text(question_text)
    source = normalize_fact_text(source_text)
    score = 0
    if aspect == KnowledgeAspect.REGISTRATION and any(
        marker in question
        for marker in ("как", "шаг", "подать", "попасть", "зарегистр")
    ):
        action_groups = (
            ("регистрац", "зарегистр", "создать аккаунт"),
            ("верифиц", "привязать аккаунт"),
            ("выбрать",),
            ("заполнить",),
            ("оформить",),
            ("выполнить",),
            ("подать заявку",),
            ("дождаться", "ожидать результат"),
        )
        score += sum(
            1 for group in action_groups if any(marker in source for marker in group)
        )
        if any(marker in source for marker in ("пошагов", "последовательност")):
            score += 2
        if "http" in source:
            score += 2
        asks_registration_deadline = any(
            marker in question
            for marker in (
                "до какой",
                "до какого",
                "принимал",
                "прием заяв",
                "срок заяв",
                "состояни",
            )
        )
        if asks_registration_deadline and re.search(
            r"(?:подать\s+заяв\w*|при[её]м\w*\s+заяв\w*|"
            r"окончани\w*\s+(?:при[её]ма|подачи)).{0,100}\bдо\b",
            source,
        ):
            score += 8
        asks_account_creation = bool(
            any(marker in question for marker in ("создать", "новый"))
            and any(marker in question for marker in ("кабинет", "аккаунт"))
        )
        source_creates_account = bool(
            "аккаунт будет создан" in source
            or (
                "письмо" in source
                and "подтвержд" in source
                and any(marker in source for marker in ("кабинет", "аккаунт"))
            )
        )
        if asks_account_creation:
            score += 8 if source_creates_account else -4
        query_volunteer = "волонтер" in question
        source_volunteer = "волонтер" in source
        if source_volunteer != query_volunteer:
            score -= 6
        query_viewer = "зрител" in question
        source_viewer = "зрител" in source
        if source_viewer != query_viewer:
            score -= 4
    if aspect == KnowledgeAspect.PROGRAM and any(
        marker in question
        for marker in ("когда", "дадут", "доступ", "опублику", "появится")
    ):
        score += 4 * sum(
            marker in source
            for marker in (
                "будет доступ",
                "за сутки",
                "накануне",
                "опублику",
                "перед началом",
            )
        )
    if aspect == KnowledgeAspect.DATES and any(
        marker in question
        for marker in (
            "до какой",
            "до какого",
            "принимал",
            "прием заяв",
            "срок заяв",
            "состояни",
        )
    ):
        if re.search(
            r"(?:подать\s+заяв\w*|при[её]м\w*\s+заяв\w*|"
            r"окончани\w*\s+(?:при[её]ма|подачи)).{0,100}\bдо\b",
            source,
        ):
            score += 8
    if aspect == KnowledgeAspect.VOLUNTEERING:
        if "в качестве волонтер" in source:
            score += 4
        if any(marker in source for marker in ("подач заяв", "подать заявку")):
            score += 3
    if aspect == KnowledgeAspect.GRANT_NOMINATIONS:
        if "номинация - это тематика проекта" in source:
            score += 5
        if re.search(r"\b\d+\s+стандартн\w*\s+номинац", source):
            score += 3
    return score


def _source_heading_values(metadata: Mapping[str, Any] | None) -> set[str]:
    metadata = metadata or {}
    values = {
        normalize_fact_text(str(metadata.get("intent_name") or "")),
        normalize_fact_text(str(metadata.get("topic") or "").replace("_", " ")),
    }
    heading = metadata.get("source_heading_path") or []
    if isinstance(heading, (list, tuple)):
        values.update(normalize_fact_text(item) for item in heading)
    return {value for value in values if value}


def _fact_stems(text: str) -> set[str]:
    return {
        stem
        for token in _TOKEN_RE.findall(normalize_fact_text(text))
        if len(token) >= 3 and token not in _STOPWORDS
        if (stem := _russian_stem(token))
    }


def _russian_stem(token: str) -> str:
    token = token.casefold().replace("ё", "е")
    if token.isdigit() or len(token) <= 4:
        return token
    endings = (
        "иями",
        "ями",
        "ами",
        "ого",
        "ему",
        "ому",
        "ыми",
        "ими",
        "овать",
        "евать",
        "ировать",
        "ация",
        "ения",
        "ание",
        "ение",
        "иться",
        "аться",
        "яться",
        "аться",
        "ая",
        "яя",
        "ое",
        "ее",
        "ые",
        "ие",
        "ый",
        "ий",
        "ой",
        "ую",
        "юю",
        "ам",
        "ям",
        "ах",
        "ях",
        "ов",
        "ев",
        "ей",
        "ом",
        "ем",
        "ы",
        "и",
        "а",
        "я",
        "у",
        "ю",
        "е",
        "о",
        "ь",
    )
    for ending in endings:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    return token


def _requested_quoted_phrases(text: str) -> tuple[str, ...]:
    phrases = re.findall(r"[«„\"]([^»“\"]{3,80})[»“\"]", str(text or ""))
    normalized = [normalize_fact_text(phrase) for phrase in phrases]
    # Event names are scope, not facts to repeat. Status labels and named shifts
    # are useful selection anchors and are normally one to three words long.
    return tuple(
        phrase
        for phrase in normalized
        if phrase and len(phrase.split()) <= 4
    )


def _score_fact_unit(
    unit: FactUnit,
    *,
    query_tokens: set[str],
    requested_aspects: frozenset[KnowledgeAspect],
    requested_phrases: tuple[str, ...],
) -> float:
    normalized = normalize_fact_text(unit.text)
    unit_tokens = _fact_stems(normalized)
    overlap = len(query_tokens & unit_tokens)
    coverage = overlap / max(1, len(query_tokens))
    score = overlap * 2.2 + coverage * 3.0
    score += max(0.0, 1.6 - unit.ordinal * 0.12)
    if unit.is_list_item:
        score += 0.35
    if any(phrase in normalized for phrase in requested_phrases):
        score += 5.0

    for aspect in requested_aspects:
        marker_hits = sum(
            1
            for marker in _ASPECT_UNIT_MARKERS[aspect]
            if _has_marker(normalized, marker)
        )
        score += min(marker_hits, 3) * 1.8

    if KnowledgeAspect.DATES in requested_aspects:
        if _DATE_RE.search(unit.text):
            score += 6.0
        if _TIME_RE.search(unit.text):
            score += 2.0
    if any(
        aspect in requested_aspects
        for aspect in (
            KnowledgeAspect.GRANT_NOMINATIONS,
            KnowledgeAspect.GRANT_AGREEMENT,
            KnowledgeAspect.GRANT_REPORT,
            KnowledgeAspect.ELIGIBILITY,
        )
    ) and _NUMBER_RE.search(unit.text):
        score += 2.5
    if KnowledgeAspect.REGISTRATION in requested_aspects and _URL_RE.search(unit.text):
        score += 2.5
    if KnowledgeAspect.OVERVIEW in requested_aspects and unit.ordinal == 0:
        score += 4.0
    return score


def _desired_unit_count(
    question_text: str,
    aspects: frozenset[KnowledgeAspect],
    max_units: int,
) -> int:
    normalized = normalize_fact_text(question_text)
    desired = 1
    if len(aspects) > 1 or " и " in f" {normalized} ":
        desired = 3
    if any(
        aspect in aspects
        for aspect in (
            KnowledgeAspect.REGISTRATION,
            KnowledgeAspect.CONFIRMATION,
            KnowledgeAspect.ACCOUNT_ACCESS,
            KnowledgeAspect.ELIGIBILITY,
            KnowledgeAspect.NAVIGATION,
            KnowledgeAspect.STATUS,
            KnowledgeAspect.VOLUNTEERING,
        )
    ):
        desired = max(desired, 3)
    if KnowledgeAspect.OVERVIEW in aspects:
        desired = max(desired, 2)
    return min(max_units, desired)


def _fit_fact_units(
    units: Iterable[FactUnit],
    *,
    max_chars: int,
    max_units: int,
) -> list[str]:
    result: list[str] = []
    used = 0
    url_seen = False
    for unit in units:
        if len(result) >= max_units:
            break
        text = " ".join(unit.text.split()).strip()
        if not text:
            continue
        urls = _URL_RE.findall(text)
        if urls and url_seen:
            text = _URL_RE.sub("", text)
            text = re.sub(r"\s+([.,;:])", r"\1", text).strip()
        separator = 1 if result else 0
        if used + separator + len(text) > max_chars:
            continue
        result.append(text)
        used += separator + len(text)
        url_seen = url_seen or bool(urls)
    return result


def _is_nonfactual_unit(normalized: str) -> bool:
    if not normalized:
        return True
    if normalized in {"ссылка", "фото", "изображение", "скриншот", "пример"}:
        return True
    return bool(_URL_RE.fullmatch(normalized) and len(normalized.split()) == 1)


def _heading_is_fact_bearing(line: str) -> bool:
    return bool(
        _URL_RE.search(line)
        or _DATE_RE.search(line)
        or _TIME_RE.search(line)
    )


def _has_marker(normalized: str, marker: str) -> bool:
    marker = normalize_fact_text(marker)
    if not marker:
        return False
    if " " in marker or any(symbol in marker for symbol in ("@", ".", "/", "_")):
        return marker in normalized
    return re.search(rf"(?<![0-9a-zа-яё]){re.escape(marker)}[0-9a-zа-яё-]*", normalized) is not None


def _ordinal_constraints_match(
    question_text: str,
    metadata: Mapping[str, Any] | None,
) -> bool:
    metadata = metadata or {}
    heading = metadata.get("source_heading_path") or []
    heading_text = (
        " ".join(str(value) for value in heading)
        if isinstance(heading, (list, tuple))
        else str(heading or "")
    )
    source_scope = " ".join(
        (
            str(metadata.get("topic") or "").replace("_", " "),
            str(metadata.get("intent_name") or ""),
            str(metadata.get("forum_normalized") or ""),
            heading_text,
        )
    )
    for noun in ("смен", "сезон"):
        requested = _nearby_ordinals(question_text, noun)
        available = _nearby_ordinals(source_scope, noun)
        if requested and available and requested.isdisjoint(available):
            return False
    return True


def _nearby_ordinals(text: str, noun_stem: str) -> set[int]:
    normalized = normalize_fact_text(text)
    word_stems = (
        ("перв", 1),
        ("втор", 2),
        ("трет", 3),
        ("четверт", 4),
        ("пят", 5),
        ("шест", 6),
        ("седьм", 7),
        ("восьм", 8),
        ("девят", 9),
        ("десят", 10),
    )
    ordinals = {
        ordinal
        for stem, ordinal in word_stems
        if re.search(
            rf"(?:\b{stem}\w*\b.{{0,24}}\b{noun_stem}\w*\b|"
            rf"\b{noun_stem}\w*\b.{{0,24}}\b{stem}\w*\b)",
            normalized,
        )
    }
    ordinals.update(
        int(value)
        for pair in re.findall(
            rf"(?:\b(\d+)\s*(?:[-–—]\s*)?(?:я|ая|й|ый|ой)?\s+{noun_stem}\w*\b|"
            rf"\b{noun_stem}\w*\s*(?:№|#)?\s*(\d+)\b)",
            normalized,
        )
        for value in pair
        if value
    )
    return ordinals
