from __future__ import annotations

from src.graph.nodes.analyze import _ensure_deterministic_questions, _fallback_analysis


def test_fallback_analysis_builds_multi_aspect_forum_questions() -> None:
    analysis = _fallback_analysis(
        "Амур: как подать заявку, кто оплачивает проезд и что делать, если не могу поехать?",
        "Амур: как подать заявку, кто оплачивает проезд и что делать, если не могу поехать?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Амур"
    assert analysis.needs_clarification is False
    assert {question.topic for question in analysis.questions} >= {
        "podacha_zayavki_na_proekt",
        "oplata_proezda",
        "otkaz_ot_uchastiya",
    }
    assert "daty_nachala_meropriyatiya" not in {
        question.topic for question in analysis.questions
    }
    assert "podtverzhdenie_uchastiya_i_org_momenty" not in {
        question.topic for question in analysis.questions
    }


def test_fallback_analysis_routes_items_documents_to_packing_topic() -> None:
    analysis = _fallback_analysis(
        "Больше, чем путешествие: какие вещи взять, что с медпунктом и можно ли с ОВЗ?",
        "Больше, чем путешествие: какие вещи взять, что с медпунктом и можно ли с ОВЗ?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    topics = {question.topic for question in analysis.questions}
    assert topics >= {
        "spisok_veschey_i_dokumentov",
        "informaciya_o_ploschadke_medicina",
        "uchastniki_s_ovz",
    }
    assert "dokumenty_meropriyatiya" not in topics


def test_deterministic_questions_are_merged_with_partial_llm_questions() -> None:
    payload = {
        "forum_normalized": "Таврида",
        "category": "форумы",
        "questions": [
            {
                "text": "Где получить письмо-вызов?",
                "topic": "pismo_vyzov",
                "category": "форумы",
                "forum_normalized": "Таврида",
            }
        ],
    }

    _ensure_deterministic_questions(
        payload,
        "Таврида: где взять письмо-вызов, когда будет сертификат и можно ли изменить заявку?",
    )

    topics = {question["topic"] for question in payload["questions"]}
    assert topics >= {
        "pismo_vyzov",
        "kogda_budet_sertifikat",
        "vnesti_izmeneniya_v_zayavku",
    }


def test_fallback_analysis_marks_everyday_requests_as_offtopic() -> None:
    for query in (
        "Закажи мне такси до дома",
        "Где заказать роллы рядом со мной?",
        "Составь исковое заявление в суд",
    ):
        analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

        assert analysis is not None
        assert analysis.is_offtopic is True
        assert analysis.should_escalate is False
        assert analysis.needs_clarification is True


def test_fallback_analysis_does_not_treat_forum_result_lists_as_offtopic() -> None:
    analysis = _fallback_analysis(
        "Российский Север Результаты отбора и списки",
        "Российский Север Результаты отбора и списки",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.is_offtopic is False
    assert analysis.category == "форумы"
    assert analysis.forum_normalized == "Российский Север"
    assert [question.topic for question in analysis.questions] == ["rezultaty_rm"]


def test_fallback_analysis_does_not_treat_birthdate_as_event_date() -> None:
    analysis = _fallback_analysis(
        "Моя дата рождения 01.02.2000, где найти ID профиля?",
        "Моя дата рождения [ДАТА], где найти ID профиля?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert [question.topic for question in analysis.questions] == ["gde_nayti_id_profilya"]
