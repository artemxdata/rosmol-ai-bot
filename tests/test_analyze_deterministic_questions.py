from __future__ import annotations

from src.graph.nodes.analyze import _ensure_deterministic_questions, _fallback_analysis


def test_fallback_analysis_clarifies_generic_application_request() -> None:
    analysis = _fallback_analysis(
        "Как подать заявку?",
        "Как подать заявку?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.category == "форумы"
    assert analysis.clarification_question is not None
    assert "о какой заявке" in analysis.clarification_question
    assert analysis.questions == []


def test_fallback_analysis_clarifies_generic_help_request() -> None:
    analysis = _fallback_analysis(
        "Помогите",
        "Помогите",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.category == "общее"
    assert analysis.clarification_question is not None
    assert "ФГАИС" in analysis.clarification_question
    assert analysis.questions == []


def test_fallback_analysis_clarifies_forum_specific_question_without_forum() -> None:
    analysis = _fallback_analysis(
        "Будет ли организован фельдшерский пункт?",
        "Будет ли организован фельдшерский пункт?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.category == "форумы"
    assert analysis.clarification_question is not None
    assert "о каком форуме" in analysis.clarification_question
    assert analysis.questions == []


def test_fallback_analysis_clarifies_generic_participant_next_step_without_forum() -> None:
    analysis = _fallback_analysis(
        "Я участник, что дальше?",
        "Я участник, что дальше?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.category == "форумы"
    assert analysis.clarification_question is not None
    assert "о каком форуме" in analysis.clarification_question
    assert analysis.questions == []


def test_fallback_analysis_clarifies_cancel_participation_without_forum() -> None:
    analysis = _fallback_analysis(
        "Можно отменить участие в форуме?",
        "Можно отменить участие в форуме?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.category == "форумы"
    assert analysis.clarification_question is not None
    assert "о каком форуме" in analysis.clarification_question
    assert analysis.questions == []


def test_fallback_analysis_routes_general_grants_to_terms_topic() -> None:
    analysis = _fallback_analysis(
        "Гранты для физлиц",
        "Гранты для физлиц",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.category == "гранты"
    assert [question.topic for question in analysis.questions] == [
        "usloviya_i_sroki_uchastiya"
    ]


def test_fallback_analysis_keeps_control_point_report_in_grants_scope() -> None:
    analysis = _fallback_analysis(
        "До сих пор не проверена ни одна контрольная точка, и окно отчета недоступно",
        "До сих пор не проверена ни одна контрольная точка, и окно отчета недоступно",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.is_offtopic is False
    assert analysis.category == "гранты"
    assert [question.topic for question in analysis.questions] == ["grant_reporting"]


def test_fallback_analysis_routes_project_selection_error_to_support() -> None:
    analysis = _fallback_analysis(
        "Не могу выбрать проект при заполнении заявки",
        "Не могу выбрать проект при заполнении заявки",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.category == "техподдержка"
    assert analysis.complexity.value == "simple"


def test_fallback_analysis_routes_project_competition_to_grants_before_forum_scope() -> None:
    query = (
        "В проектах участвуют только массовые мероприятия? "
        "Могу ли я участвовать в конкурсе с индивидуальным проектом?"
    )
    analysis = _fallback_analysis(
        query,
        query,
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.category == "гранты"
    assert analysis.needs_clarification is False


def test_fallback_analysis_routes_grant_application_ui_error_to_support() -> None:
    analysis = _fallback_analysis(
        "Хочу зарегистрироваться на конкурс грантов, но в поле выбора проекта ничего не выпадает",
        "Хочу зарегистрироваться на конкурс грантов, но в поле выбора проекта ничего не выпадает",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.category == "техподдержка"
    assert analysis.needs_clarification is False


def test_fallback_analysis_escalates_personal_status_request() -> None:
    analysis = _fallback_analysis(
        "Статус заявки",
        "Статус заявки",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.should_escalate is True
    assert analysis.escalation_reason == "personal_status"


def test_fallback_analysis_escalates_specific_technical_review_request() -> None:
    analysis = _fallback_analysis(
        "Добрый день! Высылаю скриншоты по вопросу заявки №10069, на почте ничего нет.",
        "Добрый день! Высылаю скриншоты по вопросу заявки №10069, на почте ничего нет.",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.should_escalate is True
    assert analysis.escalation_reason in {"technical_issue", "personal_status"}


def test_fallback_analysis_escalates_operator_only_brandbook_request() -> None:
    analysis = _fallback_analysis(
        "Добрый день, будет ли брендбук Дня молодёжи 2026?",
        "Добрый день, будет ли брендбук Дня молодёжи 2026?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.should_escalate is True
    assert analysis.escalation_reason == "operator_requested"


def test_fallback_analysis_routes_general_forum_question_to_overview_topic() -> None:
    analysis = _fallback_analysis(
        "Расскажи про Машук",
        "Расскажи про Машук",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Машук"
    assert analysis.category == "форумы"
    assert [question.topic for question in analysis.questions] == ["o_meropriyatii"]


def test_fallback_analysis_routes_inflected_forum_name_to_overview_topic() -> None:
    analysis = _fallback_analysis(
        "Расскажи про Волгу",
        "Расскажи про Волгу",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Волга"
    assert analysis.category == "форумы"
    assert [question.topic for question in analysis.questions] == ["o_meropriyatii"]


def test_fallback_analysis_routes_homoglyph_forum_name_to_overview_topic() -> None:
    analysis = _fallback_analysis(
        "Острова",
        "Острова",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Островa"
    assert analysis.category == "форумы"
    assert [question.topic for question in analysis.questions] == ["o_meropriyatii"]


def test_fallback_analysis_detects_ivolga_application_alias() -> None:
    analysis = _fallback_analysis(
        "Как подать заявку на Иволгу?",
        "Как подать заявку на Иволгу?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is False
    assert analysis.forum_normalized == "Иволга"
    assert [question.topic for question in analysis.questions] == [
        "podacha_zayavki_na_proekt"
    ]


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


def test_fallback_analysis_routes_food_verb_to_food_topic() -> None:
    analysis = _fallback_analysis(
        "Скажите, пожалуйста, где будут жить участники иВолги, и где они будут питаться?",
        "Скажите, пожалуйста, где будут жить участники иВолги, и где они будут питаться?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Иволга"
    topics = {question.topic for question in analysis.questions}
    assert "usloviya_prozhivaniya" in topics
    assert "informaciya_o_ploschadke_pitanie_pite" in topics


def test_fallback_analysis_keeps_forum_location_as_separate_aspect() -> None:
    analysis = _fallback_analysis(
        "Российский Север: какие документы нужны участнику и где будет проходить форум?",
        "Российский Север: какие документы нужны участнику и где будет проходить форум?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Российский Север"
    topics = {question.topic for question in analysis.questions}
    assert "spisok_veschey_i_dokumentov" in topics
    assert "daty_nachala_meropriyatiya" in topics


def test_fallback_analysis_keeps_forum_overview_with_other_aspects() -> None:
    message = (
        "Российский Север: в чём суть форума, когда он проходит, "
        "оплачивается ли дорога и проживание?"
    )
    analysis = _fallback_analysis(
        message,
        message,
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Российский Север"
    topics = {question.topic for question in analysis.questions}
    assert topics >= {
        "o_meropriyatii",
        "daty_nachala_meropriyatiya",
        "oplata_proezda",
        "usloviya_prozhivaniya",
    }


def test_fallback_analysis_routes_where_are_documents_to_event_documents() -> None:
    analysis = _fallback_analysis(
        "Территория смыслов: где документы, будет ли трансфер и где посмотреть результаты?",
        "Территория смыслов: где документы, будет ли трансфер и где посмотреть результаты?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.forum_normalized == "Территория смыслов"
    topics = {question.topic for question in analysis.questions}
    assert topics >= {
        "dokumenty_meropriyatiya",
        "transfer_do_mesta_provedeniya_meropriyatiya",
        "rezultaty_rm",
    }


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
        "Где купить билеты на матчи сборной России?",
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


def test_fallback_analysis_does_not_route_partner_programs_to_forum_program() -> None:
    analysis = _fallback_analysis(
        "Можно ли исправить в заявке информацию по программе партнёров?",
        "Можно ли исправить в заявке информацию по программе партнёров?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert {question.topic for question in analysis.questions} == set()


def test_fallback_analysis_does_not_treat_ticket_word_as_age_or_travel() -> None:
    analysis = _fallback_analysis(
        "При регистрации в МАКС не смог сформировать билет, что делать?",
        "При регистрации в МАКС не смог сформировать билет, что делать?",
        {"complexity": "complex"},
        None,
    )

    assert analysis is not None
    topics = {question.topic for question in analysis.questions}
    assert "vozrastnye_ogranicheniya" not in topics
    assert "oplata_proezda" not in topics
