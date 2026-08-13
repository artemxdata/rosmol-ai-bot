from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.graph.response_profiles import (
    chunk_has_event_date_evidence,
    detect_response_profiles,
    infer_response_profile,
    resolve_ticket_response_profile,
    response_has_cross_aspect_drift,
    response_has_cross_aspect_drift_for_profiles,
)
from src.models import QueryAnalysis
from src.response_contract import ResponseProfileName


def _published_seed_record(chunk_id: str) -> dict:
    seed_path = Path(__file__).resolve().parents[1] / "data" / "knowledge_base_seed.json"
    records = json.loads(seed_path.read_text(encoding="utf-8"))
    return next(record for record in records if record.get("chunk_id") == chunk_id)


@pytest.mark.parametrize(
    "chunk_id",
    [
        "yonote_api_rfgkfljjld_s0017_registraciya",
        "yonote_api_gkby3eml8d_s0002_registraciya",
        "yonote_api_gnu4bmbubc_s0002_registraciya",
        "yonote_api_bhjm352dd6_s0003_registraciya",
        "yonote_api_fd4f7puzzj_s0003_registraciya",
        "yonote_api_jzxsccnczf_s0006_etapy",
        "yonote_api_2rhqotnyz9_s0009_kanal_v_tg_t_me_villagedon",
    ],
)
def test_registration_and_selection_seed_chunks_are_not_event_date_evidence(
    chunk_id: str,
) -> None:
    record = _published_seed_record(chunk_id)

    assert not chunk_has_event_date_evidence(record["text_clean"], record)


@pytest.mark.parametrize(
    "chunk_id",
    [
        "yonote_api_lrcgc2vl3g_s0005_dedlayn_podachi_zayavki_na_smenu",
        "yonote_api_cljqo2rlvk_s0027_festival_molodogo_iskusstva",
    ],
)
def test_mixed_seed_chunks_keep_real_event_range_evidence(chunk_id: str) -> None:
    record = _published_seed_record(chunk_id)

    assert chunk_has_event_date_evidence(record["text_clean"], record)


def test_response_aspect_detector_distinguishes_event_date_from_deadline() -> None:
    assert detect_response_profiles("Форум пройдёт 8 августа.") == {
        ResponseProfileName.DATES
    }
    deadline_profiles = detect_response_profiles(
        "Подача заявок проходит до 8 августа."
    )
    assert ResponseProfileName.APPLICATION in deadline_profiles
    assert ResponseProfileName.DATES not in deadline_profiles


def test_single_profile_guard_rejects_event_date_for_travel_question() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.TRAVEL,
        "Форум пройдёт 8 августа.",
    )


def test_single_profile_guard_rejects_extra_date_after_application_answer() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.APPLICATION,
        "Заявку подают через личный кабинет. Форум пройдёт 8 августа.",
    )


def test_single_profile_guard_rejects_extra_date_in_coordinated_clause() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.APPLICATION,
        "Заявку подают через кабинет, а форум пройдёт 8 августа.",
    )


def test_single_profile_guard_rejects_extra_aspect_after_comma() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.APPLICATION,
        "Регистрация открыта до 1 июня, трансфер до площадки бесплатный.",
    )


def test_single_profile_guard_rejects_extra_application_after_semicolon() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.DOCUMENTS,
        "Возьми паспорт; зарегистрироваться можно в кабинете.",
    )


@pytest.mark.parametrize(
    "response",
    [
        "Заявку подают в кабинете и форум пройдёт 8 августа.",
        "Заявку подают в кабинете, дата форума — 8 августа.",
    ],
)
def test_single_profile_guard_rejects_coordinated_event_fact(
    response: str,
) -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.APPLICATION,
        response,
    )


def test_single_profile_guard_rejects_extra_registration_after_documents_answer() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.DOCUMENTS,
        "Возьми паспорт. Зарегистрироваться можно в личном кабинете.",
    )


@pytest.mark.parametrize(
    ("expected", "response"),
    [
        (
            ResponseProfileName.APPLICATION,
            "Подача заявок открыта до 8 августа.",
        ),
        (
            ResponseProfileName.SELECTION_STATUS,
            "Результаты отбора опубликуют 8 августа.",
        ),
    ],
)
def test_business_deadline_date_is_not_mistaken_for_event_date(
    expected: ResponseProfileName,
    response: str,
) -> None:
    assert not response_has_cross_aspect_drift(expected, response)
    assert ResponseProfileName.DATES not in detect_response_profiles(response)


def test_multi_profile_guard_requires_every_requested_aspect() -> None:
    expected = {
        ResponseProfileName.APPLICATION,
        ResponseProfileName.TRAVEL,
    }

    assert response_has_cross_aspect_drift_for_profiles(
        expected,
        "Заявку подают через личный кабинет.",
    )
    assert not response_has_cross_aspect_drift_for_profiles(
        expected,
        "Заявку подают через личный кабинет. Проезд участник оплачивает сам.",
    )


def test_multi_profile_guard_rejects_explicit_unrequested_event_dates() -> None:
    expected = {
        ResponseProfileName.APPLICATION,
        ResponseProfileName.TRAVEL,
    }

    assert response_has_cross_aspect_drift_for_profiles(
        expected,
        (
            "Заявку подают через личный кабинет. "
            "Проезд участник оплачивает сам. Форум пройдёт 8 августа."
        ),
    )


def test_profile_detector_does_not_find_food_inside_winner_word() -> None:
    detected = detect_response_profiles("Победителям сообщат результаты позднее.")

    assert ResponseProfileName.FOOD not in detected
    assert ResponseProfileName.SELECTION_STATUS in detected


def test_plural_application_statuses_route_to_selection_status() -> None:
    text = "Что означают статусы заявки во ФГАИС?"

    assert (
        infer_response_profile(QueryAnalysis(category="платформа_фгаис"), text)
        == ResponseProfileName.SELECTION_STATUS
    )
    assert ResponseProfileName.SELECTION_STATUS in detect_response_profiles(text)


def test_location_filter_is_not_misclassified_as_documents() -> None:
    application_excerpt = (
        "Настройка фильтров поиска (местоположение, даты и тематика). "
        "После изучения информации для подачи заявки нужно кликнуть на кнопку."
    )

    detected = detect_response_profiles(application_excerpt)

    assert ResponseProfileName.APPLICATION in detected
    assert ResponseProfileName.DOCUMENTS not in detected
    assert not response_has_cross_aspect_drift(
        ResponseProfileName.APPLICATION,
        application_excerpt,
    )


@pytest.mark.parametrize(
    "text",
    (
        "Положение форума опубликовано.",
        "Условия указаны в положении о форуме.",
        "Архив положений о форумах доступен на сайте.",
    ),
)
def test_standalone_event_regulation_remains_a_documents_marker(text: str) -> None:
    assert ResponseProfileName.DOCUMENTS in detect_response_profiles(text)


def test_eligibility_condition_may_name_legal_entity_registration() -> None:
    eligibility = (
        "Юридическое лицо, зарегистрированное на территории Российской Федерации, "
        "может участвовать; возраст представителя — от 18 до 55 лет."
    )

    assert not response_has_cross_aspect_drift(
        ResponseProfileName.ELIGIBILITY,
        eligibility,
    )
    assert response_has_cross_aspect_drift(
        ResponseProfileName.ELIGIBILITY,
        "Зарегистрироваться можно через личный кабинет.",
    )
    assert response_has_cross_aspect_drift(
        ResponseProfileName.ELIGIBILITY,
        "Юридическое лицо может участвовать и подать заявку до 1 июня.",
    )


@pytest.mark.parametrize(
    ("expected", "response"),
    [
        (
            ResponseProfileName.DOCUMENTS,
            "Для регистрации нужен паспорт.",
        ),
        (
            ResponseProfileName.DOCUMENTS,
            "Письмо-вызов пришлют после отбора.",
        ),
        (
            ResponseProfileName.TRAVEL,
            "Трансфер отправляется в день заезда, 8 августа.",
        ),
        (
            ResponseProfileName.SELECTION_STATUS,
            "Результаты объявят в день закрытия форума — 8 августа.",
        ),
        (
            ResponseProfileName.APPLICATION,
            "Исправить ошибку в заявке можно до отправки.",
        ),
    ],
)
def test_cross_aspect_guard_allows_context_inside_direct_answer_clause(
    expected: ResponseProfileName,
    response: str,
) -> None:
    assert not response_has_cross_aspect_drift(expected, response)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Если билет на мероприятие не пришёл, проверь папку «Спам».",
            ResponseProfileName.APPLICATION,
        ),
        (
            "Билет на День молодёжи доступен в разделе «Мои билеты».",
            ResponseProfileName.APPLICATION,
        ),
        (
            "Кто оплачивает билет на поезд до форума?",
            ResponseProfileName.TRAVEL,
        ),
        (
            "Можно ли компенсировать транспортный билет?",
            ResponseProfileName.TRAVEL,
        ),
        (
            "Кто оплачивает билеты до мероприятия?",
            ResponseProfileName.TRAVEL,
        ),
    ],
)
def test_ticket_resolver_distinguishes_admission_and_transport(
    text: str,
    expected: ResponseProfileName,
) -> None:
    assert resolve_ticket_response_profile(text) == expected
    assert infer_response_profile(QueryAnalysis(category="форумы"), text) == expected
    assert expected in detect_response_profiles(text)


def test_bare_ticket_does_not_imply_travel_or_application() -> None:
    text = "Билет доступен."

    assert resolve_ticket_response_profile(text) is None
    assert not detect_response_profiles(text) & {
        ResponseProfileName.APPLICATION,
        ResponseProfileName.TRAVEL,
    }


def test_cannot_attend_request_is_withdrawal_not_travel() -> None:
    analysis = QueryAnalysis(category="форумы")

    assert infer_response_profile(
        analysis,
        "Что делать, если не получается поехать?",
    ) == ResponseProfileName.APPLICATION
    assert infer_response_profile(
        analysis,
        "Не получается приехать в день заезда.",
    ) == ResponseProfileName.TRAVEL


def test_multi_profile_completeness_uses_full_detector() -> None:
    expected = {
        ResponseProfileName.PROGRAM,
        ResponseProfileName.FOOD,
        ResponseProfileName.ACCOMMODATION,
    }
    response = (
        "Программа опубликована в личном кабинете. "
        "Питание организовано на площадке. "
        "Участников размещают в гостинице."
    )

    assert not response_has_cross_aspect_drift_for_profiles(expected, response)


def test_multi_profile_guard_still_rejects_unrequested_strict_aspect() -> None:
    expected = {
        ResponseProfileName.FOOD,
        ResponseProfileName.ACCOMMODATION,
    }
    response = (
        "Питание организовано на площадке. "
        "Участников размещают в гостинице. Проезд оплачивает участник."
    )

    assert response_has_cross_aspect_drift_for_profiles(expected, response)
