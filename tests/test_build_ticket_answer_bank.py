from __future__ import annotations

from scripts.build_ticket_answer_bank import (
    RegexOnlyPIIMasker,
    assign_candidate_ids,
    build_candidate,
    has_disallowed_markers,
    quality_score,
    sanitize_text,
    select_balanced,
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


def test_build_candidate_masks_pii_and_strips_signature() -> None:
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

    assert candidate is not None
    assert "ivan@example.ru" not in candidate["answer"]
    assert "+7 999 123 45 67" not in candidate["answer"]
    assert "С уважением" not in candidate["answer"]
    assert "[EMAIL]" in candidate["answer"]
    assert "[ТЕЛЕФОН]" in candidate["answer"]


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
        == "Меня зовут [ИМЯ], телефон [ТЕЛЕФОН], id [ID]"
    )


def test_regex_only_masker_keeps_batch_mode_fast_without_natasha() -> None:
    masked, mapping = RegexOnlyPIIMasker().mask("Иван написал на ivan@example.ru")

    assert masked == "Иван написал на [EMAIL]"
    assert mapping == {"email": ["ivan@example.ru"]}


def test_has_disallowed_markers_detects_private_letter_fragments() -> None:
    assert has_disallowed_markers("Во вложении письмо", "Ответ") is True
    assert has_disallowed_markers("Как подать заявку?", "Заявку можно подать в профиле") is False
