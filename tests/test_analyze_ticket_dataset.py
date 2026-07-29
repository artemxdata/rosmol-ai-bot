from __future__ import annotations

from pathlib import Path

import pytest

from scripts.analyze_ticket_dataset import (
    ForumAlias,
    build_artifact_manifest,
    build_golden_candidates,
    build_product_conversation_splits,
    build_product_eval_splits,
    build_product_role_review_queue,
    build_product_split_plan,
    build_reranker_pairs,
    choose_answer_candidate,
    choose_question_candidate,
    classify_category,
    classify_escalation,
    classify_segment_role,
    classify_topic,
    detect_forum,
    ensure_private_output_dir,
    is_low_signal_title,
    load_ticket_rows,
    mask_pii,
    normalize_ticket,
    promote_staged_artifacts,
    reconstruct_dialogue_turns,
    score_question_segment,
    split_message_segments,
    validate_artifact_manifest,
    write_json,
    write_json_array,
)
from src.kb.source_extractors import SpreadsheetRow


def test_ticket_analysis_rejects_output_outside_private_data(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="data.private"):
        ensure_private_output_dir(tmp_path / "public-output")


def test_mask_pii_repeats_until_adjacent_phone_numbers_are_removed() -> None:
    masked, pii_types = mask_pii(
        "+7 900 000 00 00+7 901 111 11 11"
    )

    assert "+7 900" not in masked
    assert "+7 901" not in masked
    assert pii_types == ["phone"]


def test_json_array_writer_preserves_existing_file_on_serialization_error(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cases.json"
    target.write_text('{"previous": true}\n', encoding="utf-8")

    with pytest.raises(TypeError):
        write_json_array(
            target,
            [
                {"ok": 1},
                {"not_json_serializable": {1}},
            ],
        )

    assert target.read_text(encoding="utf-8") == '{"previous": true}\n'


def test_artifact_manifest_detects_partial_or_tampered_output(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()
    input_path = tmp_path / "source.xlsx"
    forums_path = tmp_path / "forums.json"
    input_path.write_bytes(b"source")
    forums_path.write_text("[]\n", encoding="utf-8")
    write_json(staging / "dataset_profile.json", {"tickets": 1})
    write_json(staging / "product_split_summary.json", {"total": 1})
    manifest = build_artifact_manifest(
        staging,
        input_path=input_path,
        forums_path=forums_path,
    )
    write_json(staging / "artifact_manifest.json", manifest)

    promote_staged_artifacts(staging, output)

    validated = validate_artifact_manifest(output)
    assert validated["artifact_count"] == 2
    (output / "dataset_profile.json").write_text(
        '{"tickets": 2}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mismatch"):
        validate_artifact_manifest(output)


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


def test_reconstruct_dialogue_turns_preserves_order_without_forced_alternation() -> None:
    turns = reconstruct_dialogue_turns(
        [
            "Когда проходит форум Машук?",
            "Как подать заявку на форум?",
            "Для этого необходимо открыть личный кабинет и выбрать мероприятие.",
            "А трансфер будет?",
        ]
    )

    assert [turn.index for turn in turns] == [0, 1, 2, 3]
    assert [turn.role for turn in turns] == [
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert turns[0].role_confidence == "high"
    assert turns[2].assistant_kind == "unknown"


def test_reconstruct_dialogue_turns_keeps_ambiguous_copy_unknown() -> None:
    turn = classify_segment_role("Хорошо", index=0)

    assert turn.role == "unknown"
    assert turn.role_confidence == "low"
    assert turn.role_reason == "ambiguous_without_speaker_metadata"


@pytest.mark.parametrize(
    "text",
    (
        "Подскажите, пожалуйста, номер заявки?",
        "Подскажите номер заявки?",
        "Укажите номер заявки?",
        "Пожалуйста, укажите номер заявки?",
    ),
)
def test_operator_data_request_is_not_promoted_to_user_turn(text: str) -> None:
    turn = classify_segment_role(text, index=0)

    assert turn.role == "assistant"
    assert turn.role_confidence == "medium"
    assert turn.role_reason == "assistant_data_request"


@pytest.mark.parametrize(
    "text",
    (
        "Вы уже подавали заявку?",
        "Вам удалось войти в личный кабинет?",
        "Проблема ещё актуальна?",
        "Подскажите, пожалуйста, у вас получилось войти?",
        "Трансфер вам нужен?",
    ),
)
def test_operator_status_check_is_not_promoted_to_user_turn(text: str) -> None:
    turn = classify_segment_role(text, index=0)

    assert turn.role == "assistant"
    assert turn.role_confidence == "medium"
    assert turn.role_reason == "assistant_status_check"


@pytest.mark.parametrize(
    "text",
    (
        "Я передал обращение коллегам.",
        "Я уточню информацию и вернусь с ответом.",
        "Я проверю статус заявки.",
    ),
)
def test_operator_promise_is_not_promoted_to_user_turn(text: str) -> None:
    turn = classify_segment_role(text, index=0)

    assert turn.role == "assistant"
    assert turn.role_confidence == "medium"
    assert turn.role_reason == "assistant_followup_promise"


@pytest.mark.parametrize(
    "text",
    (
        "Заявка отправлена?",
        "Я отправил заявку.",
    ),
)
def test_question_mark_or_first_person_alone_does_not_make_user_high(
    text: str,
) -> None:
    turn = classify_segment_role(text, index=0)

    assert (turn.role, turn.role_confidence) != ("user", "high")


def test_user_question_after_system_copy_is_extracted_without_bot_prefix() -> None:
    turn = classify_segment_role(
        (
            "Чем я могу быть полезен? "
            "Не могу войти в личный кабинет, что делать?"
        ),
        index=0,
    )

    assert turn.role == "user"
    assert turn.role_confidence == "high"
    assert turn.role_reason == "user_request_after_system_copy"
    assert turn.text_masked == "Не могу войти в личный кабинет, что делать?"


def test_short_user_problem_requires_role_review_without_first_person() -> None:
    turn = classify_segment_role("Регистрация не работает", index=0)

    assert turn.role == "user"
    assert turn.role_confidence == "medium"
    assert turn.role_reason == "ambiguous_reported_problem"


@pytest.mark.parametrize(
    "text",
    (
        "У меня регистрация не работает",
        "Я не могу войти в личный кабинет",
        "Мне не приходит код подтверждения",
    ),
)
def test_first_person_problem_is_high_confidence_user_turn(text: str) -> None:
    turn = classify_segment_role(text, index=0)

    assert turn.role == "user"
    assert turn.role_confidence == "high"
    assert turn.role_reason == "reported_problem"


def test_instruction_about_known_problem_remains_assistant_copy() -> None:
    turn = classify_segment_role(
        "Регистрация не работает, попробуйте подать заявку позднее.",
        index=0,
    )

    assert turn.role != "user"


def test_operator_problem_explanation_is_not_promoted_to_user_turn() -> None:
    turn = classify_segment_role(
        "Ошибка возникает из-за неверно заполненного поля.",
        index=0,
    )

    assert turn.role != "user"


def test_short_reply_after_clarification_requires_role_review() -> None:
    turns = reconstruct_dialogue_turns(
        [
            "Уточни, пожалуйста, название форума, о котором спрашиваешь?",
            "Машук",
        ]
    )

    assert turns[0].role == "assistant"
    assert turns[0].role_confidence == "medium"
    assert turns[1].role == "user"
    assert turns[1].role_confidence == "medium"
    assert turns[1].role_reason == "short_reply_after_clarification"


def test_medium_confidence_turn_keeps_ticket_in_partial_role_review() -> None:
    record = normalize_ticket(
        {
            "id": "partial-role",
            "title": "Личное сообщение VKontakte",
            "messages": (
                "Когда проходит форум Машук? --- "
                "Уточни, пожалуйста, название форума, о котором спрашиваешь?"
            ),
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert record["high_confidence_user_turns_count"] == 1
    assert record["review_required_turns_count"] == 1
    assert record["role_reconstruction_status"] == "partial"


def test_role_reconstruction_masks_each_turn_before_serialization() -> None:
    turns = reconstruct_dialogue_turns(
        ["Подскажите, статус заявки можно прислать на test@example.com?"]
    )

    assert turns[0].role == "user"
    assert turns[0].has_pii is True
    assert turns[0].pii_types == ("email",)
    assert "test@example.com" not in turns[0].text_masked
    assert "[EMAIL]" in turns[0].text_masked


def test_choose_question_candidate_checks_segments_after_eighth() -> None:
    segments = ["Хорошо"] * 8 + ["Когда проходит форум Машук?"]

    assert choose_question_candidate("", segments) == "Когда проходит форум Машук?"


def test_high_confidence_user_turn_overrides_conflicting_ticket_title() -> None:
    record = normalize_ticket(
        {
            "id": "title-conflict",
            "title": "Статус заявки",
            "messages": "Когда проходит форум Машук?",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    assert record["question_candidate"] == "Когда проходит форум Машук?"
    assert record["response_profile"] == "dates"
    assert record["title_role_review_candidate"] == ""


def test_title_only_candidate_stays_in_role_review_and_out_of_product_splits() -> None:
    record = normalize_ticket(
        {
            "id": "title-only",
            "title": "Статус заявки",
            "messages": "Хорошо",
            "date_created": "2026-01-20 10:00:00",
        },
        [],
    )

    splits, summary = build_product_eval_splits([record])
    role_review_queue = build_product_role_review_queue([record])
    title_review = next(
        item
        for item in role_review_queue
        if item["role_reason"] == "unverified_ticket_title"
    )

    assert record["question_candidate"] == ""
    assert record["high_confidence_user_turns_count"] == 0
    assert record["role_reconstruction_status"] == "unresolved"
    assert record["title_role_review_candidate"] == "Статус заявки"
    assert summary["total"] == 0
    assert all(not cases for cases in splits.values())
    assert title_review["turn_index"] == -1
    assert title_review["requires_human_review"] is True


def test_product_conversation_payload_contains_only_high_confidence_user_turns() -> None:
    record = normalize_ticket(
        {
            "id": "conversation-1",
            "unique_id": "conversation-1",
            "department": "ВК Умный Бот",
            "status": "closed",
            "title": "Личное сообщение VKontakte",
            "messages": (
                "Когда проходит форум Машук? --- "
                "Для этого необходимо проверить страницу мероприятия в личном кабинете. --- "
                "А трансфер будет?"
            ),
            "date_created": "2026-01-01 10:00:00",
        },
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )

    splits, summary = build_product_conversation_splits(
        [record],
        [ForumAlias(normalized="Машук", aliases=("Машук",))],
    )
    conversation = next(
        item
        for conversations in splits.values()
        for item in conversations
    )

    assert summary["total"] == 1
    assert summary["turns_total"] == 2
    assert [turn["source_turn_index"] for turn in conversation["turns"]] == [0, 2]
    assert all(turn["operator_answer_included"] is False for turn in conversation["turns"])
    assert all("expected_behavior" not in turn for turn in conversation["turns"])
    assert all("predicted_behavior" in turn for turn in conversation["turns"])
    assert conversation["operator_answer_used_as_fact"] is False


def test_product_conversation_components_do_not_cross_splits() -> None:
    def record(
        ticket_hash: str,
        day: int,
        queries: list[str],
    ) -> dict[str, object]:
        return {
            "ticket_hash": ticket_hash,
            "created_at": f"2026-01-{day:02d} 10:00:00",
            "channel": "api",
            "role_reconstruction_status": "complete",
            "dialogue_turns": [
                {
                    "turn_index": index,
                    "role": "user",
                    "role_confidence": "high",
                    "text_masked": query,
                }
                for index, query in enumerate(queries)
            ],
        }

    records = [
        record(
            "linked-a",
            1,
            ["Когда проходит форум?", "Как подать заявку?"],
        ),
        record(
            "linked-b",
            10,
            ["Как подать заявку?", "Какие документы нужны?"],
        ),
        record(
            "linked-c",
            20,
            ["Какие документы нужны?", "Где посмотреть программу?"],
        ),
    ]
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
    ]
    records.extend(
        record(
            f"independent-{index}",
            index + 1,
            [f"Что нужно для темы {label}?"],
        )
        for index, label in enumerate(labels, start=1)
    )

    splits, _summary = build_product_conversation_splits(records, [])
    component_splits: dict[str, set[str]] = {}
    linked = []
    for split, conversations in splits.items():
        for conversation in conversations:
            component_splits.setdefault(
                conversation["duplicate_component_id"],
                set(),
            ).add(split)
            if conversation["ticket_id_hash"].startswith("linked-"):
                linked.append((split, conversation["duplicate_component_id"]))

    assert len({component_id for _, component_id in linked}) == 1
    assert {split for split, _ in linked} == {"calibration"}
    assert all(len(split_names) == 1 for split_names in component_splits.values())


def test_role_review_queue_excludes_high_confidence_turns() -> None:
    queue = build_product_role_review_queue(
        [
            {
                "ticket_hash": "review-1",
                "dialogue_turns": [
                    {
                        "turn_index": 0,
                        "role": "user",
                        "role_confidence": "high",
                        "text_masked": "Когда проходит форум?",
                    },
                    {
                        "turn_index": 1,
                        "role": "unknown",
                        "role_confidence": "low",
                        "role_reason": "ambiguous_without_speaker_metadata",
                        "text_masked": "Хорошо",
                    },
                ],
            }
        ]
    )

    assert len(queue) == 1
    assert queue[0]["turn_index"] == 1
    assert queue[0]["requires_human_review"] is True
    assert queue[0]["operator_answer_used_as_fact"] is False


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
    assert case["forbidden_response_profiles"] == [
        "application",
        "selection_status",
        "travel",
    ]
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
                "role_reconstruction_status": "complete",
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


def test_shared_split_plan_keeps_each_source_ticket_and_partial_component_together() -> None:
    labels = (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "iota",
        "kappa",
        "lambda",
        "mu",
        "nu",
        "xi",
        "omicron",
        "pi",
        "rho",
        "sigma",
        "tau",
        "alpha",
    )
    records: list[dict[str, object]] = []
    for index, label in enumerate(labels, start=1):
        query = f"When is event {label}?"
        records.append(
            {
                "ticket_hash": f"cross-artifact-{index}",
                "question_candidate": query,
                "created_at": f"2026-01-{index:02d} 10:00:00",
                "channel": "api",
                "query_forum_normalized": "",
                "response_profile": "dates",
                "query_needs_clarification": False,
                "query_should_escalate": False,
                "query_escalation_reason": None,
                "role_reconstruction_status": (
                    "partial"
                    if index == len(labels)
                    else "complete"
                ),
                "dialogue_turns": [
                    {
                        "turn_index": 0,
                        "role": "user",
                        "role_confidence": "high",
                        "text_masked": query,
                    }
                ],
            }
        )

    plan = build_product_split_plan(records, [])
    query_splits, _query_summary = build_product_eval_splits(
        records,
        [],
        split_plan=plan,
    )
    conversation_splits, _conversation_summary = (
        build_product_conversation_splits(
            records,
            [],
            split_plan=plan,
        )
    )

    query_locations = {
        case["ticket_id_hash"]: (
            split,
            case["duplicate_component_id"],
        )
        for split, cases in query_splits.items()
        for case in cases
    }
    conversation_locations = {
        conversation["ticket_id_hash"]: (
            split,
            conversation["duplicate_component_id"],
        )
        for split, conversations in conversation_splits.items()
        for conversation in conversations
    }

    assert query_locations.keys() == conversation_locations.keys()
    assert query_locations == conversation_locations
    assert query_splits["validation"]
    assert query_splits["holdout"]
    assert conversation_splits["validation"]
    assert conversation_splits["holdout"]

    partial_ticket = "cross-artifact-20"
    linked_early_ticket = "cross-artifact-1"
    assert query_locations[partial_ticket][0] == "calibration"
    assert query_locations[linked_early_ticket] == query_locations[partial_ticket]
    assert all(
        partial_ticket != item["ticket_id_hash"]
        for split in ("validation", "holdout")
        for item in query_splits[split] + conversation_splits[split]
    )
