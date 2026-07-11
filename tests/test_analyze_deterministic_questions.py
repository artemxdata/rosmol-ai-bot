from __future__ import annotations

import pytest

from src.graph.nodes.analyze import (
    ACCOUNT_CHECK_RESPONSE,
    APPLICATION_SUCCESS_RESPONSE,
    BOT_CAPABILITIES_RESPONSE,
    GRANT_CONTEXT_RESPONSE,
    GREETING_RESPONSE,
    _ensure_deterministic_questions,
    _fallback_analysis,
)
from src.models import Channel, Session


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


@pytest.mark.parametrize(
    "query",
    [
        "Я запутался, куда мне обратиться",
        "Мне нужна помощь по молодежной теме",
        "Не понимаю, с чего начать",
    ],
)
def test_fallback_analysis_clarifies_broad_help_requests(query: str) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None
    assert "ФГАИС" in analysis.clarification_question


def test_fallback_analysis_clarifies_uncertain_scope_request() -> None:
    analysis = _fallback_analysis(
        "У меня вопрос, но не знаю, к вам ли это",
        "У меня вопрос, но не знаю, к вам ли это",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.category == "общее"
    assert analysis.clarification_question is not None
    assert "ФГАИС" in analysis.clarification_question


def test_fallback_analysis_clarifies_vague_application_problem() -> None:
    analysis = _fallback_analysis(
        "У меня с заявкой какая-то проблема",
        "У меня с заявкой какая-то проблема",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.clarification_question is not None
    assert "о какой заявке" in analysis.clarification_question


def test_fallback_analysis_uses_original_text_for_masked_politician_name() -> None:
    analysis = _fallback_analysis(
        "Как ты относишься к Путину?",
        "Как ты относишься к [ИМЯ]?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.is_offtopic is True
    assert analysis.should_escalate is False


def test_fallback_analysis_uses_original_text_for_masked_colloquial_issue() -> None:
    analysis = _fallback_analysis(
        "Задолбался с этим кабинетом",
        "[ИМЯ] с этим кабинетом",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None
    assert "личном кабинете" in analysis.clarification_question


def test_fallback_analysis_routes_forum_discovery_to_published_navigation_topic() -> None:
    analysis = _fallback_analysis(
        "Чё по форумам вообще сейчас есть?",
        "Чё по форумам вообще сейчас есть?",
        {"complexity": "simple"},
        None,
    )

    assert analysis is not None
    assert analysis.category == "общее"
    assert analysis.needs_clarification is False
    assert [question.topic for question in analysis.questions] == [
        "rekomendacii_obschie"
    ]


def test_fallback_analysis_detects_tavrida_art_application_alias() -> None:
    query = "Как попасть на Тавриду.Арт и подать заявку?"
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.forum_normalized == "Таврида"
    assert analysis.needs_clarification is False
    assert [question.topic for question in analysis.questions] == [
        "podacha_zayavki_na_proekt"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "Как ты относишься к Путину?",
        "Чей Крым?",
        "Израиль или Иран?",
        "Ты тупой?",
        "Почему ты такой дурак?",
        "Зачем вообще нужен такой тупой бот?",
        "Ваш Росмол — та ещё шарага, да?",
        "Поговори со мной",
        "Что я получу, если смешаю корень златоцветника и настойку полыни?",
    ],
)
def test_fallback_analysis_scopes_politics_abuse_and_provocations_without_operator(
    query: str,
) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.category == "offtopic"
    assert analysis.is_offtopic is True
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.escalation_reason is None


@pytest.mark.parametrize(
    ("query", "expected_response"),
    [
        ("Как дела?", GREETING_RESPONSE),
        ("Ты нейросеть?", BOT_CAPABILITIES_RESPONSE),
        ("Что ты вообще умеешь?", BOT_CAPABILITIES_RESPONSE),
        ("Что вы вообще можете подсказать?", BOT_CAPABILITIES_RESPONSE),
        ("Ты хоть что-то можешь?", BOT_CAPABILITIES_RESPONSE),
        ("Ты исскусственный интеллект?", BOT_CAPABILITIES_RESPONSE),
        ("Ты ответишь?", BOT_CAPABILITIES_RESPONSE),
        ("Как задавать тебе вопросы, чтобы ты понял?", BOT_CAPABILITIES_RESPONSE),
    ],
)
def test_fallback_analysis_answers_bot_meta_questions_deterministically(
    query: str,
    expected_response: str,
) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.category == "общее"
    assert analysis.needs_clarification is True
    assert analysis.clarification_question == expected_response
    assert analysis.should_escalate is False


@pytest.mark.parametrize(
    "query",
    ["Ошибка", "Не работает", "Не пускает", "Не прикрепляется", "Что делать?"],
)
def test_fallback_analysis_clarifies_ambiguous_short_requests(query: str) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.clarification_question is not None
    assert "Уточни" in analysis.clarification_question
    assert analysis.should_escalate is False


@pytest.mark.parametrize(
    "query",
    [
        "у вас баг",
        "Там ошибка, но я не понял какая",
        "Я что-то нажал, и теперь непонятно что дальше",
        "Отправка вложения без контекста",
        "У меня с анкетой какая-то фигня",
    ],
)
def test_fallback_analysis_clarifies_broad_ambiguous_technical_requests(
    query: str,
) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None
    assert (
        "Уточни" in analysis.clarification_question
        or "Добавь" in analysis.clarification_question
    )


def test_fallback_analysis_clarifies_unknown_received_letter() -> None:
    query = "Мне пришло письмо, и я не понимаю, что делать"
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None
    assert "от кого пришло письмо" in analysis.clarification_question


@pytest.mark.parametrize(
    "query",
    [
        "Мне отказали, можно ли подать заново и куда написать?",
        "Я не прошел отбор, но мне кажется, что это ошибка",
        "Я зарегистрировался, но заявка как будто не отправлена",
    ],
)
def test_fallback_analysis_clarifies_unspecified_application_or_selection(
    query: str,
) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None
    assert "о какой заявке" in analysis.clarification_question


@pytest.mark.parametrize(
    "query",
    [
        "Хочу понять, как получить жилье молодому специалисту",
        "Меня отчисляют, вы можете помочь?",
        "Можете подсказать по льготам и выплатам для молодежи?",
    ],
)
def test_fallback_analysis_scopes_unrelated_social_support_requests(query: str) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.is_offtopic is True
    assert analysis.should_escalate is False


def test_fallback_analysis_routes_colloquial_forum_entry_to_application_topic() -> None:
    query = "Хочу попасть на форум Ростов, расскажи, что мне нужно сделать"
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.forum_normalized == "Ростов"
    assert analysis.category == "форумы"
    assert [question.topic for question in analysis.questions] == [
        "podacha_zayavki_na_proekt"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "Я чёт не вкурил, как туда попасть",
        "Можно как-то вписаться в движ?",
        "Я хочу залететь на программу, но хз подхожу ли",
        "Крч, я подался, дальше чё",
    ],
)
def test_fallback_analysis_clarifies_colloquial_action_without_event(query: str) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.category == "форумы"
    assert analysis.needs_clarification is True
    assert analysis.clarification_question is not None
    assert "форуме или мероприятии" in analysis.clarification_question


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


def test_fallback_analysis_does_not_clarify_signed_letter_manual_check() -> None:
    text = (
        "Мне нужно повторно отправить подписанное письмо с печатью, "
        "проверьте мою заявку"
    )
    analysis = _fallback_analysis(text, text, {"complexity": "simple"}, None)

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


def test_fallback_analysis_asks_for_feedback_details_without_operator() -> None:
    query = "Хочу дать фидбек"
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None
    assert "что именно хочешь оценить" in analysis.clarification_question


@pytest.mark.parametrize(
    "query",
    [
        "Я из маленького города и не понимаю, какие у меня вообще есть возможности",
        "Хочу понять, куда вообще можно податься, и есть ли что-то для моего региона",
        "Я после колледжа, не понимаю куда двигаться, есть ли у вас что-то полезное",
        "У меня нет сильного опыта и портфолио, мне есть смысл куда-то подаваться?",
    ],
)
def test_fallback_analysis_routes_broad_opportunity_discovery_to_catalog(
    query: str,
) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.category == "общее"
    assert analysis.should_escalate is False
    assert [question.topic for question in analysis.questions] == [
        "rekomendacii_obschie"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "Я уже участвовал раньше, надо заново регистрироваться или нет?",
        "Мне 34, я не поздно вообще для ваших программ?",
    ],
)
def test_fallback_analysis_clarifies_account_or_age_without_specific_event(
    query: str,
) -> None:
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert analysis.clarification_question is not None


def test_fallback_analysis_routes_short_grant_link_request_to_application_source() -> None:
    query = "Ссылка на грант"
    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.category == "гранты"
    assert [question.topic for question in analysis.questions] == [
        "podat_zayavku_na_uchastie"
    ]


def _session_with_turn(user: str, bot: str) -> Session:
    return Session(
        user_id="followup-user",
        channel=Channel.API,
        user_id_hash="followup-hash",
        last_messages=[{"user": user, "bot": bot}],
    )


@pytest.mark.parametrize(
    ("query", "expected_fragment"),
    [
        ("а это не форум, а программа", "предыдущая тема не подходит"),
        ("а это не на грант заявка", "предыдущая тема не подходит"),
        ("а где там про условия?", "какие условия тебя интересуют"),
        ("нет, это от вас письмо", "тему письма"),
        ("я про лк на вашем сайте", "какой сайт открыт"),
    ],
)
def test_explicit_followup_correction_wins_over_session_context(
    query: str,
    expected_fragment: str,
) -> None:
    session = _session_with_turn(
        "Хочу узнать про форум Амур",
        "Форум «Амур» пройдёт в 2026 году.",
    )

    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, session)

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert expected_fragment in str(analysis.clarification_question)


def test_application_success_is_acknowledged_without_reopening_clarification() -> None:
    session = _session_with_turn(
        "Как подать заявку?",
        "Уточни, пожалуйста, о какой заявке речь?",
    )

    analysis = _fallback_analysis(
        "У меня получилось подать заявку",
        "У меня получилось подать заявку",
        {"complexity": "simple"},
        session,
    )

    assert analysis is not None
    assert analysis.clarification_question == APPLICATION_SUCCESS_RESPONSE
    assert analysis.should_escalate is False


def test_account_existence_followup_returns_grounded_self_check() -> None:
    session = _session_with_turn(
        "Не могу восстановить пароль",
        "Открой форму восстановления пароля.",
    )

    analysis = _fallback_analysis(
        "А как вообще понять, есть ли у меня аккаунт?",
        "А как вообще понять, есть ли у меня аккаунт?",
        {"complexity": "simple"},
        session,
    )

    assert analysis is not None
    assert analysis.clarification_question == ACCOUNT_CHECK_RESPONSE
    assert analysis.should_escalate is False


def test_unknown_short_name_after_clarification_does_not_fuzzy_match_source() -> None:
    session = _session_with_turn(
        "У меня проблема с заявкой",
        "Уточни, пожалуйста, точное название программы.",
    )

    analysis = _fallback_analysis(
        "Россия сельская",
        "Россия сельская",
        {"complexity": "simple"},
        session,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert "Не нахожу точного" in str(analysis.clarification_question)


def test_vague_application_followup_preserves_personal_status_escalation() -> None:
    session = _session_with_turn(
        "Я подал заявку, но не понимаю, прошёл ли",
        "Передаю обращение специалисту.",
    )

    analysis = _fallback_analysis(
        "Ага, а что по заявке?",
        "Ага, а что по заявке?",
        {"complexity": "simple"},
        session,
    )

    assert analysis is not None
    assert analysis.should_escalate is True
    assert analysis.escalation_reason == "personal_status"


@pytest.mark.parametrize(
    "query",
    [
        "Не знаю, что мне интересно",
        "А для Адыгеи есть что-то?",
        "А в Анадыре что-то проводится?",
        "Алло, куда мне пойти участвовать, если я закончил колледж?",
    ],
)
def test_followup_opportunity_discovery_uses_catalog(query: str) -> None:
    session = _session_with_turn(
        "Куда можно податься?",
        "Можно подобрать мероприятие по интересам.",
    )

    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, session)

    assert analysis is not None
    assert analysis.should_escalate is False
    assert [question.topic for question in analysis.questions] == [
        "rekomendacii_obschie"
    ]


def test_vague_cabinet_complaint_uses_previous_turn_for_clarification() -> None:
    session = _session_with_turn(
        "Я про личный кабинет на вашем сайте",
        "Уточни, пожалуйста, какой сайт открыт.",
    )

    analysis = _fallback_analysis(
        "Да он просто тупит и неудобный",
        "Да он просто тупит и неудобный",
        {"complexity": "simple"},
        session,
    )

    assert analysis is not None
    assert analysis.needs_clarification is True
    assert analysis.should_escalate is False
    assert "что именно не работает" in str(analysis.clarification_question)


def test_forum_to_grant_correction_is_acknowledged_without_operator() -> None:
    session = _session_with_turn(
        "Форум Россия страна веселья",
        "Уточни точное название форума.",
    )

    analysis = _fallback_analysis(
        "А, это не форум, а грант!",
        "А, это не форум, а грант!",
        {"complexity": "simple"},
        session,
    )

    assert analysis is not None
    assert analysis.clarification_question == GRANT_CONTEXT_RESPONSE
    assert analysis.should_escalate is False


@pytest.mark.parametrize(
    "query",
    [
        "Есть идея проекта, но нет опыта и команды",
        "Даже одному можно участвовать?",
    ],
)
def test_project_team_questions_route_to_confirmed_team_source(query: str) -> None:
    session = _session_with_turn(
        "Есть идея проекта, но нет опыта и команды",
        "Команда проекта не обязательна.",
    )

    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, session)

    assert analysis is not None
    assert analysis.category == "гранты"
    assert analysis.should_escalate is False
    assert [question.topic for question in analysis.questions] == ["komanda_proekta"]


def test_sport_recommendation_routes_to_confirmed_sport_source() -> None:
    query = "Я спортсмен, где я могу поучаствовать?"

    analysis = _fallback_analysis(query, query, {"complexity": "simple"}, None)

    assert analysis is not None
    assert analysis.category == "платформа_фгаис"
    assert [question.topic for question in analysis.questions] == ["rekomendacii_sport"]


def test_no_travel_opportunity_request_routes_to_catalog() -> None:
    query = (
        "Я работаю и учусь, у меня нет возможности надолго уезжать, "
        "но хочется на форум"
    )

    analysis = _fallback_analysis(query, query, {"complexity": "complex"}, None)

    assert analysis is not None
    assert analysis.category == "общее"
    assert [question.topic for question in analysis.questions] == [
        "rekomendacii_obschie"
    ]
