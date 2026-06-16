from __future__ import annotations

from scripts.build_ticket_answer_bank import (
    RegexOnlyPIIMasker,
    assign_candidate_ids,
    build_candidate,
    has_disallowed_markers,
    has_unsafe_answer_shape,
    has_unsafe_question_shape,
    quality_score,
    sanitize_text,
    select_balanced,
    strip_greeting,
    strip_leading_addressee,
)
from src.security.pii_masker import PIIMasker


def _ticket(**overrides):
    data = {
        "ticket_hash": "abc123",
        "should_escalate": False,
        "answerable_by_kb": True,
        "question_candidate": "Как зарегистрироваться на форум Машук?",
        "answer_candidate": (
            "Подать заявку на форум можно через личный кабинет на платформе. "
            "Необходимо выбрать мероприятие, заполнить профиль и отправить заявку."
        ),
        "category": "форумы",
        "topic": "регистрация_и_заявка",
        "forum_normalized": "Машук",
        "difficulty": "medium",
        "intent": "форумы.регистрация_и_заявка",
    }
    data.update(overrides)
    return data


def test_quality_score_rejects_personal_letter_answer() -> None:
    score, reasons = quality_score(
        _ticket(),
        "Компенсируют ли проезд?",
        (
            "Меня зовут Иван Иванов. Во вложении прикрепляю письмо и прошу "
            "рассмотреть возможность компенсации расходов."
        ),
    )

    assert score < 9
    assert "looks_like_user_letter" in reasons


def test_sanitize_text_masks_pii_and_strips_signature() -> None:
    sanitized = sanitize_text(
        (
            "Добрый день, Иван! Восстановить доступ можно через форму сброса пароля. "
            "Если код не приходит на ivan@example.ru или телефон +7 999 123 45 67, "
            "проверьте папку спам и повторите запрос. С уважением, поддержка"
        ),
        PIIMasker(),
    )

    assert "ivan@example.ru" not in sanitized
    assert "+7 999 123 45 67" not in sanitized
    assert "С уважением" not in sanitized
    assert "[EMAIL]" in sanitized
    assert "[ТЕЛЕФОН]" in sanitized
    assert not sanitized.startswith("Добрый день")


def test_build_candidate_rejects_answer_with_sensitive_placeholders() -> None:
    candidate = build_candidate(
        _ticket(
            question_candidate="Здравствуйте, Иван, как восстановить доступ?",
            answer_candidate=(
                "Добрый день, Иван! Восстановить доступ можно через форму сброса пароля. "
                "Если код не приходит на ivan@example.ru или телефон +7 999 123 45 67, "
                "проверьте папку спам и повторите запрос. С уважением, поддержка"
            ),
            category="платформа_фгаис",
            topic="доступ_и_техническая_ошибка",
            forum_normalized=None,
        ),
        chunks=[],
        masker=PIIMasker(),
        min_quality_score=5,
        top_matches=3,
    )

    assert candidate is None


def test_build_candidate_skips_escalation_ticket() -> None:
    candidate = build_candidate(
        _ticket(should_escalate=True),
        chunks=[],
        masker=PIIMasker(),
        min_quality_score=1,
        top_matches=3,
    )

    assert candidate is None


def test_build_candidate_skips_personal_letter_marker_in_question() -> None:
    candidate = build_candidate(
        _ticket(question_candidate="Во вложении прикрепляю письмо, что делать?"),
        chunks=[],
        masker=PIIMasker(),
        min_quality_score=1,
        top_matches=3,
    )

    assert candidate is None


def test_select_balanced_round_robins_topics() -> None:
    selected = select_balanced(
        [
            {
                "id": "a1",
                "category": "форумы",
                "topic": "регистрация",
                "forum_normalized": "А",
                "quality_score": 12,
            },
            {
                "id": "a2",
                "category": "форумы",
                "topic": "регистрация",
                "forum_normalized": "А",
                "quality_score": 11,
            },
            {
                "id": "b1",
                "category": "гранты",
                "topic": "отчет",
                "forum_normalized": None,
                "quality_score": 10,
            },
        ],
        limit=2,
    )

    assert [item["id"] for item in selected] == ["b1", "a1"]


def test_assign_candidate_ids_removes_ticket_hash_ids() -> None:
    assert assign_candidate_ids([{"id": "ticket_answer::1234567890"}]) == [
        {"id": "ticket_answer_bank::001"}
    ]


def test_sanitize_text_masks_name_phrase() -> None:
    assert (
        sanitize_text(
            "Меня зовут Иван Иванов, телефон 89991234567, id 123456789",
            PIIMasker(),
        )
        == "Меня зовут [ИМЯ], телефон [ТЕЛЕФОН], ID [ID]"
    )


def test_regex_only_masker_keeps_batch_mode_fast_without_natasha() -> None:
    masked, mapping = RegexOnlyPIIMasker().mask("Иван написал на ivan@example.ru")

    assert masked == "Иван написал на [EMAIL]"
    assert mapping == {"email": ["ivan@example.ru"]}


def test_has_disallowed_markers_detects_private_letter_fragments() -> None:
    assert has_disallowed_markers("Во вложении письмо", "Ответ") is True
    assert (
        has_disallowed_markers(
            "Служба Заботы Росмолодёжи: 2025 г., 10:12. Как подать заявку?",
            "Подать заявку можно через личный кабинет.",
        )
        is True
    )
    assert has_disallowed_markers("Как подать заявку?", "Заявку можно подать в профиле") is False


def test_has_unsafe_answer_shape_rejects_placeholders_and_first_person() -> None:
    assert has_unsafe_answer_shape("Напишите нам на [EMAIL]") is True
    assert has_unsafe_answer_shape("Проверьте заявку с ID [ID]") is True
    assert has_unsafe_answer_shape("Мне пришло письмо, помогите подтвердить заявку") is True
    assert has_unsafe_answer_shape("Прошу оказать содействие в данном вопросе") is True
    assert has_unsafe_answer_shape("Возможно ли продлить подачу заявки?") is True
    assert has_unsafe_answer_shape("ID: 825b4722-4c56-4c6e-b343-d05fc0b8df52") is True
    assert has_unsafe_answer_shape("Не успела подать заявку, можете пожалуйста открыть?") is True
    assert has_unsafe_answer_shape("К сожалению, не смогу быть на форуме, почта та же") is True
    assert has_unsafe_answer_shape("Julia Sushkova commented " * 5) is True
    assert (
        has_unsafe_answer_shape(
            "Служба Заботы Росмолодёжи: 2025 г., 10:12. Запрос отправлял по гранту"
        )
        is True
    )
    assert has_unsafe_answer_shape("(не черновик, именно проект) со всеми документами") is True
    assert has_unsafe_answer_shape("и не получила обратную связь по проекту") is True
    assert has_unsafe_answer_shape("Не могу подать заявку на грант") is True
    assert has_unsafe_answer_shape("Заявку можно подать через личный кабинет") is False


def test_has_unsafe_question_shape_rejects_placeholders_but_allows_first_person() -> None:
    assert has_unsafe_question_shape("Не могу войти в аккаунт [EMAIL]") is True
    assert has_unsafe_question_shape("Где найти ID: 825b4722-4c56-4c6e-b343-d05fc0b8df52?") is True
    assert has_unsafe_question_shape("Служба Заботы Росмолодёжи: как подать заявку?") is True
    assert has_unsafe_question_shape("Мне не пришло письмо, что делать?") is False


def test_strip_greeting_removes_support_salutation() -> None:
    assert (
        strip_greeting("Добрый день, [ИМЯ]! Заявку можно подать через личный кабинет.")
        == "Заявку можно подать через личный кабинет."
    )


def test_strip_leading_addressee_removes_name_prefix() -> None:
    assert (
        strip_leading_addressee(
            "Анастасия, поскольку аккаунт верифицирован через ЕСИА, измените данные на Госуслугах."
        )
        == "поскольку аккаунт верифицирован через ЕСИА, измените данные на Госуслугах."
    )
