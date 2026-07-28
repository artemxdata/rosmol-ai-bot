from __future__ import annotations

from pathlib import Path

from scripts.analyze_ticket_dataset import (
    ForumAlias,
    build_golden_candidates,
    build_product_eval_splits,
    build_reranker_pairs,
    choose_answer_candidate,
    choose_question_candidate,
    classify_category,
    classify_escalation,
    classify_topic,
    detect_forum,
    is_low_signal_title,
    load_ticket_rows,
    mask_pii,
    normalize_ticket,
    score_question_segment,
    split_message_segments,
)
from src.kb.source_extractors import SpreadsheetRow


def test_load_ticket_rows_merges_export_continuations(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "scripts.analyze_ticket_dataset.read_xlsx_sheets",
        lambda _path: {
            "Sheet1": [
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=1,
                    cells=("id", "unique_id", "messages", "status"),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=2,
                    cells=("ticket-1", "unique-1", "Первая часть", "closed"),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=3,
                    cells=("ticket-1", "", "Вторая часть", ""),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=4,
                    cells=("ticket-1", "", "Третья часть", ""),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=5,
                    cells=("ticket-1", "", "", ""),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=6,
                    cells=("ticket-2", "unique-2", "Другой тикет", "closed"),
                ),
            ]
        },
    )

    rows = load_ticket_rows(Path("unused.xlsx"))

    assert len(rows) == 2
    assert rows[0]["messages"] == "Первая часть\nВторая часть\nТретья часть"
    assert rows[0]["status"] == "closed"
    assert rows[1]["id"] == "ticket-2"


def test_load_ticket_rows_does_not_merge_incomplete_independent_row(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "scripts.analyze_ticket_dataset.read_xlsx_sheets",
        lambda _path: {
            "Sheet1": [
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=1,
                    cells=("id", "unique_id", "messages", "status"),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=2,
                    cells=("ticket-1", "unique-1", "Первая часть", "closed"),
                ),
                SpreadsheetRow(
                    sheet_name="Sheet1",
                    row_number=3,
                    cells=("ticket-1", "", "Не continuation", "open"),
                ),
            ]
        },
    )

    rows = load_ticket_rows(Path("unused.xlsx"))

    assert len(rows) == 2
    assert rows[0]["messages"] == "Первая часть"
    assert rows[1]["status"] == "open"


def test_mask_pii_masks_contacts_and_urls() -> None:
    masked, pii_types = mask_pii(
        "Пишите на test@example.com или звоните 8(999)123-45-67, ссылка https://x.test/a"
    )

    assert "[EMAIL]" in masked
    assert "[ТЕЛЕФОН]" in masked
    assert "[URL]" in masked
    assert pii_types == ["email", "url", "phone"]


def test_mask_pii_masks_private_platform_identifiers() -> None:
    masked, pii_types = mask_pii(
        "ФИО: Иванов Иван Иванович, @private_user, id12345678, "
        "СНИЛС 123-456-789 01, номер 123456789012345"
    )

    assert "Иванов Иван Иванович" not in masked
    assert "@private_user" not in masked
    assert "id12345678" not in masked
    assert "123-456-789 01" not in masked
    assert "123456789012345" not in masked
    assert {"fio_context", "handle", "vk_id", "snils", "long_id"} <= set(pii_types)


def test_split_message_segments_uses_dialog_separator() -> None:
    assert split_message_segments("Привет --- Ответ оператора --- Спасибо") == [
        "Привет",
        "Ответ оператора",
        "Спасибо",
    ]


def test_golden_candidates_are_balanced_across_groups() -> None:
    records = [
        golden_record("complex-1", category="forums", difficulty="complex", forum="Forum A"),
        golden_record("complex-2", category="forums", difficulty="complex", forum="Forum A"),
        golden_record("simple-1", category="grants", difficulty="simple", forum=""),
        golden_record(
            "escalate-1",
            category="support",
            difficulty="complex",
            should_escalate=True,
            escalation_reason="technical_issue",
        ),
    ]

    golden = build_golden_candidates(records, max_items=3)

    selected_ids = [item["source_ticket_ids"][0] for item in golden]

    assert len(selected_ids) == 3
    assert len({"complex-1", "complex-2"} & set(selected_ids)) == 1
    assert "simple-1" in selected_ids
    assert "escalate-1" in selected_ids


def test_golden_candidates_skip_unanswerable_non_escalation_records() -> None:
    records = [
        golden_record("answerable"),
        {
            **golden_record("weak"),
            "answer_candidate": "",
            "answerable_by_kb": False,
            "should_escalate": False,
        },
    ]

    golden = build_golden_candidates(records, max_items=10)

    assert [item["source_ticket_ids"][0] for item in golden] == ["answerable"]


def golden_record(
    ticket_id: str,
    *,
    category: str = "forums",
    difficulty: str = "simple",
    forum: str = "Forum A",
    should_escalate: bool = False,
    escalation_reason: str | None = None,
) -> dict[str, object]:
    return {
        "ticket_hash": ticket_id,
        "ticket_id": ticket_id,
        "question_candidate": f"How to solve ticket {ticket_id}?",
        "answer_candidate": "Use the official knowledge-base answer for this exact ticket.",
        "answerable_by_kb": not should_escalate,
        "should_escalate": should_escalate,
        "escalation_reason": escalation_reason,
        "forum_normalized": forum,
        "category": category,
        "topic": "registration",
        "intent": f"{category}.registration",
        "difficulty": difficulty,
        "quality_notes": "",
    }


def test_low_signal_title_is_not_used_as_question_candidate() -> None:
    record = normalize_ticket(
        {
            "id": "1",
            "title": "Личное сообщение VKontakte",
            "messages": (
                "Здравствуйте, где посмотреть статус заявки на форум Машук? --- "
                "Статус заявки отображается в личном кабинете."
            ),
            "status": "closed",
            "typical_atypical": "Типовой",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert is_low_signal_title("Личное сообщение VKontakte") is True
    assert record["question_candidate"] == (
        "Здравствуйте, где посмотреть статус заявки на форум Машук?"
    )


def test_choose_answer_candidate_prefers_support_reply_over_later_user_fragment() -> None:
    answer = choose_answer_candidate(
        [
            "Как подать заявку на форум Машук?",
            (
                "Подать заявку можно через личный кабинет Росмолодёжи. "
                "Необходимо выбрать мероприятие, заполнить профиль и отправить заявку."
            ),
            (
                "Не успела подать заявку, можете пожалуйста открыть регистрацию? "
                "Очень хочу принять участие и буду ждать ответа."
            ),
        ],
        "Как подать заявку на форум Машук?",
    )

    assert answer.startswith("Подать заявку можно")


def test_choose_answer_candidate_rejects_thread_artifact_user_fragments() -> None:
    answer = choose_answer_candidate(
        [
            "Служба Заботы Росмолодёжи: 2025 г., 10:12. Запрос отправлял по гранту.",
            "Прошу открыть подачу заявки, потому что не успел отправить проект.",
        ],
        "Можно ли открыть подачу заявки повторно?",
    )

    assert answer == ""


def test_choose_question_candidate_prefers_user_question_over_support_answer() -> None:
    answer_like = (
        "Здесь все зависит от возраста ребенка. Если ребенку от 14 до 17 лет, "
        "необходимо приложить согласие законного представителя."
    )
    user_like = "Подскажите, можно ли пройти регистрацию на форум с ребенком?"

    question = choose_question_candidate("", [answer_like, user_like])

    assert question == user_like
    assert score_question_segment(user_like) > score_question_segment(answer_like)


def test_choose_question_candidate_skips_answer_only_segments() -> None:
    question = choose_question_candidate(
        "Здесь все зависит от возраста участника",
        [
            "Для этого необходимо перейти в личный кабинет и проверить статус заявки.",
            "Обрати внимание: регистрация проходит только через платформу Росмолодежи.",
        ],
    )

    assert question == ""


def test_choose_question_candidate_skips_support_acknowledgement_and_form_artifacts() -> None:
    question = choose_question_candidate(
        "Личное сообщение VKontakte",
        [
            "Для оценки качества обслуживания, пожалуйста, нажмите на кнопку внизу:",
            "тест",
            (
                "Здравствуйте! Мы уже занимаемся вашим вопросом. "
                "Вернемся с ответом в течение 15 минут 🤗"
            ),
        ],
    )

    assert question == ""


def test_choose_question_candidate_prefers_user_button_over_long_bot_copy() -> None:
    question = choose_question_candidate(
        "Личное сообщение VKontakte",
        [
            (
                "Реализация проекта предполагает не только подготовку мероприятия, "
                "но и подведение итогов. Для этого нужно заполнить отчёт и приложить "
                "подтверждающие документы по официальной форме."
            ),
            "В списке нет моего мероприятия",
        ],
    )

    assert question == "В списке нет моего мероприятия"


def test_classification_detects_forum_registration_and_technical_escalation() -> None:
    text = "Не могу войти в профиль, кнопка подачи заявки на форум Машук не работает"

    assert classify_category(text) == "техподдержка"
    assert classify_topic(text) == "доступ_и_техническая_ошибка"
    assert classify_escalation(text, {"status": "closed"}) == "technical_issue"
    assert (
        detect_forum(
            text,
            [ForumAlias(normalized="Машук", aliases=("Машук",))],
        )
        == "Машук"
    )


def test_normalize_ticket_builds_masked_private_record() -> None:
    record = normalize_ticket(
        {
            "id": "1",
            "unique_id": "u1",
            "department": "MAX Бот",
            "status": "closed",
            "title": "Как подать заявку на форум Машук?",
            "messages": (
                "Как подать заявку? --- Подать заявку можно в личном кабинете "
                "Росмолодёжи после авторизации и выбора нужного мероприятия."
            ),
            "typical_atypical": "Типовой",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert record["ticket_id"].startswith("ticket::")
    assert record["ticket_id"] != "1"
    assert record["unique_id"] != "u1"
    assert record["category"] == "платформа_фгаис"
    assert record["forum_normalized"] == "Машук"
    assert record["answerable_by_kb"] is True
    assert record["should_escalate"] is False


def test_normalize_ticket_profiles_only_the_user_question_not_operator_answer() -> None:
    record = normalize_ticket(
        {
            "id": "aspect-1",
            "unique_id": "aspect-1",
            "department": "ВК Умный Бот",
            "status": "closed",
            "title": "Когда проходит форум Машук?",
            "messages": (
                "Когда проходит форум Машук? --- "
                "Трансфер от вокзала до площадки организуют бесплатно."
            ),
            "typical_atypical": "Типовой",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert record["response_profile"] == "dates"
    assert record["query_category"] == "форумы"
    assert record["query_forum_normalized"] == "Машук"
    assert record["query_should_escalate"] is False


def test_product_labels_do_not_use_operator_copy_or_ticket_status() -> None:
    record = normalize_ticket(
        {
            "id": "query-only-1",
            "unique_id": "query-only-1",
            "department": "ВК Умный Бот",
            "status": "open",
            "title": "Когда проходит форум Машук?",
            "messages": (
                "Когда проходит форум Машук? --- "
                "Перевожу на оператора. Трансфер организуют от вокзала."
            ),
            "typical_atypical": "Типовой",
            "date_created": "2026-01-01 10:00:00",
            "date_updated": "2026-01-02 10:00:00",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    splits, _summary = build_product_eval_splits([record])
    case = next(case for cases in splits.values() for case in cases)

    assert case["category"] == "форумы"
    assert case["entity"] == "Машук"
    assert case["expected_response_profile"] == "dates"
    assert case["expected_route"] == "answer"
    assert case["expected_escalation_reason"] is None
    assert case["available_at"] == "2026-01-02T10:00:00"


def test_golden_candidates_and_reranker_pairs_are_built() -> None:
    records = [
        {
            "ticket_hash": "a",
            "ticket_id": "1",
            "question_candidate": "Как подать заявку на Машук?",
            "answer_candidate": "Подать заявку можно через личный кабинет Росмолодёжи.",
            "answerable_by_kb": True,
            "should_escalate": False,
            "escalation_reason": None,
            "forum_normalized": "Машук",
            "category": "платформа_фгаис",
            "topic": "регистрация_и_заявка",
            "intent": "платформа_фгаис.регистрация_и_заявка",
            "difficulty": "simple",
            "quality_notes": "",
        },
        {
            "ticket_hash": "b",
            "ticket_id": "2",
            "question_candidate": "Как подать заявку на Утро?",
            "answer_candidate": "Для форума Утро заявка также подается через личный кабинет.",
            "answerable_by_kb": True,
            "should_escalate": False,
            "escalation_reason": None,
            "forum_normalized": "Утро",
            "category": "платформа_фгаис",
            "topic": "регистрация_и_заявка",
            "intent": "платформа_фгаис.регистрация_и_заявка",
            "difficulty": "simple",
            "quality_notes": "",
        },
    ]

    golden = build_golden_candidates(records, max_items=10)
    pairs = build_reranker_pairs(golden, records, max_pairs=10)

    assert len(golden) == 2
    assert golden[0]["deprecated_for_product_eval"] is True
    assert golden[0]["operator_answer_used_as_fact"] is True
    assert pairs[0]["query"] == "Как подать заявку на Машук?"
    assert pairs[0]["deprecated_for_product_eval"] is True
    assert pairs[0]["hard_negative_texts"] == [
        "Для форума Утро заявка также подается через личный кабинет."
    ]


def test_product_eval_split_is_ticket_level_query_only_and_leakage_safe() -> None:
    labels = [
        "альфа",
        "бета",
        "гамма",
        "дельта",
        "эпсилон",
        "дзета",
        "эта",
        "тета",
        "йота",
        "каппа",
        "лямбда",
        "мю",
        "ню",
        "кси",
        "омикрон",
        "пи",
        "ро",
        "сигма",
        "тау",
        "ипсилон",
    ]
    records = []
    for index, label in enumerate(labels, start=1):
        query = (
            "Когда проходит Машук?"
            if index in {1, 20}
            else f"Какие документы нужны для темы {label}?"
        )
        records.append(
            {
                "ticket_hash": f"hash-{index}",
                "question_candidate": query,
                "answer_candidate": "Операторский ответ не должен попасть в eval case.",
                "created_at": f"2026-01-{index:02d} 10:00:00",
                "channel": "ВК Умный Бот",
                "category": "форумы",
                "forum_normalized": "Машук" if index in {1, 20} else "",
                "query_forum_normalized": "Машук" if index in {1, 20} else "",
                "response_profile": "dates" if index in {1, 20} else "documents",
                "needs_clarification": False,
                "should_escalate": False,
                "escalation_reason": None,
            }
        )

    splits, summary = build_product_eval_splits(records)

    assert summary["total"] == 20
    assert splits["validation"]
    assert splits["holdout"]
    assert summary["sealed_holdout_ready"] is False
    assert summary["unit"] == "merged_ticket_query_candidate"
    cluster_splits: dict[str, set[str]] = {}
    for split, cases in splits.items():
        for case in cases:
            cluster_splits.setdefault(case["duplicate_cluster_id"], set()).add(split)
            assert "answer_candidate" not in case
            assert case["operator_answer_included"] is False
            assert case["requires_human_review"] is True
    assert all(len(split_names) == 1 for split_names in cluster_splits.values())

    repeated = [
        case
        for case in splits["calibration"]
        if case["query"] == "Когда проходит Машук?"
    ]
    assert len(repeated) == 2
