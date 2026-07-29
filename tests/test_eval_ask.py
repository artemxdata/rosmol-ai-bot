from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import eval.run_ask as run_ask_module
from eval.run_ask import (
    _guard_eval_privacy,
    _json_safe,
    _llm_cost_rub_total,
    _normalize_case,
    _quality_gate_failures,
    _trace_dsn_candidates,
    build_seed_ask_cases,
    run_eval,
    score_case,
    summarize_results,
)


def test_normalize_case_accepts_common_fields() -> None:
    case = _normalize_case(
        {
            "case_id": "mashuk-travel",
            "question": "Кто оплачивает проезд на Машук?",
            "expected_chunks": "chunk_1",
            "answer_contains": "оплачивает самостоятельно",
            "expected_escalated": False,
            "expected_generator_model": "source_chunk",
            "tags": "travel",
        }
    )

    assert case == {
        "id": "mashuk-travel",
        "query": "Кто оплачивает проезд на Машук?",
        "privacy_class": "standard",
        "user_id": "ask-eval",
        "channel": "api",
        "forum_context": None,
        "expected_chunk_ids": ["chunk_1"],
        "expected_cited_chunk_ids": [],
        "allowed_cited_source_types": [],
        "equivalent_chunk_ids": {},
        "expected_answer_contains": ["оплачивает самостоятельно"],
        "expected_message_masked_contains": [],
        "forbidden_message_masked_contains": [],
        "expected_behavior": None,
        "expected_response_profile": None,
        "forbidden_response_profiles": [],
        "expected_escalated": False,
        "expected_escalation_reason": None,
        "expected_generator_model": "source_chunk",
        "tags": ["travel"],
    }


def test_normalize_case_keeps_explicit_forum_context() -> None:
    case = _normalize_case(
        {
            "id": "ticket-context",
            "query": "Где мой билет?",
            "forum_context": "День молодёжи",
        }
    )

    assert case["forum_context"] == "День молодёжи"


def test_normalize_case_keeps_private_ticket_privacy_class() -> None:
    case = _normalize_case(
        {
            "id": "private-ticket",
            "query": "private masked ticket query",
            "privacy_class": "private_ticket_derived",
            "label_status": "human_reviewed",
            "requires_human_review": False,
        }
    )

    assert case["privacy_class"] == "private_ticket_derived"


@pytest.mark.parametrize(
    ("requires_human_review", "label_status"),
    [
        (" true ", "human_reviewed"),
        (False, " Weak_Unreviewed "),
    ],
)
def test_normalize_case_rejects_weak_review_flags(
    requires_human_review: object,
    label_status: str,
) -> None:
    with pytest.raises(ValueError, match="requiring human review"):
        _normalize_case(
            {
                "query": "private masked ticket query",
                "requires_human_review": requires_human_review,
                "label_status": label_status,
            }
        )


def test_normalize_case_rejects_unreviewed_private_ticket() -> None:
    with pytest.raises(ValueError, match="human-reviewed"):
        _normalize_case(
            {
                "query": "private masked ticket query",
                "privacy_class": "private_ticket_derived",
            }
        )


def test_private_ticket_eval_allows_only_local_private_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(run_ask_module, "PRIVATE_DATA_ROOT", private_root)
    private_case = [{"privacy_class": "private_ticket_derived"}]
    cases_path = private_root / "cases.json"
    output_path = private_root / "result.json"
    markdown_path = private_root / "result.md"

    _guard_eval_privacy(
        cases=private_case,
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=markdown_path,
        target="http://127.0.0.1:8001/ask",
    )
    _guard_eval_privacy(
        cases=private_case,
        cases_path=cases_path,
        output_path=output_path,
        markdown_path=None,
        target="http://app-ml:8000/ask",
    )

    with pytest.raises(ValueError, match="loopback"):
        _guard_eval_privacy(
            cases=private_case,
            cases_path=cases_path,
            output_path=output_path,
            markdown_path=None,
            target="https://example.test/ask",
        )
    with pytest.raises(ValueError, match="must stay under"):
        _guard_eval_privacy(
            cases=private_case,
            cases_path=cases_path,
            output_path=tmp_path / "public-result.json",
            markdown_path=None,
            target="http://localhost:8001/ask",
        )


def test_build_seed_ask_cases_uses_intent_examples() -> None:
    cases = build_seed_ask_cases(
        [
            {
                "chunk_id": "travel",
                "status": "published",
                "category": "форумы",
                "forum_normalized": "Машук",
                "intent_examples": ["кто платит за дорогу"],
            },
            {
                "chunk_id": "old",
                "status": "archived",
                "intent_examples": ["старый вопрос"],
            },
        ],
        user_prefix="local",
    )

    assert cases == [
        {
            "id": "seed_balanced::travel",
            "query": "Машук кто платит за дорогу",
            "user_id": "local-1",
            "channel": "api",
            "expected_chunk_ids": ["travel"],
            "expected_answer_contains": [],
            "expected_behavior": "answer",
            "expected_escalated": None,
            "expected_escalation_reason": None,
            "expected_generator_model": None,
            "tags": ["seed_balanced", "category:форумы", "forum:Машук"],
        }
    ]


def test_trace_dsn_candidates_add_localhost_fallback() -> None:
    candidates = _trace_dsn_candidates(
        "postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot"
    )

    assert candidates == [
        "postgresql://rosmol:rosmol@postgres:5432/rosmol_ai_bot",
        "postgresql://rosmol:rosmol@localhost:5432/rosmol_ai_bot",
    ]


def test_json_safe_decodes_asyncpg_json_strings() -> None:
    value = _json_safe('[{"chunk_id": "travel", "score": 0.9}]')

    assert value == [{"chunk_id": "travel", "score": 0.9}]


def test_llm_cost_rub_total_ignores_missing_and_invalid_values() -> None:
    assert _llm_cost_rub_total(
        [
            {"llm_estimated_cost_rub": "1.25"},
            {"llm_estimated_cost_rub": 2},
            {"llm_estimated_cost_rub": None},
            {"llm_estimated_cost_rub": "not-a-number"},
            {},
        ]
    ) == 3.25


def test_score_case_uses_trace_for_chunk_model_and_escalation_checks() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
            "expected_generator_model": "source_chunk",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивается самостоятельно.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
        "cache_hit": False,
        "total_latency_ms": 110,
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["escalation_match"] is True
    assert result["generator_model_match"] is True
    assert result["passed"] is True


def test_score_case_normalizes_spacing_inside_numeric_dates() -> None:
    case = _normalize_case(
        {
            "id": "date-spacing",
            "query": "До какого числа можно подать заявку?",
            "expected_answer_contains": ["12.09.2026"],
        }
    )

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Подать заявку можно до 12. 09. 2026 включительно.",
            "latency_ms": 20,
            "error": None,
        },
        None,
    )

    assert result["answer_contains_match"] is True
    assert result["passed"] is True


def test_normalize_case_accepts_expected_behavior_aliases() -> None:
    case = _normalize_case(
        {
            "id": "scope",
            "query": "Какая погода завтра?",
            "expected_response_type": "offtopic",
        }
    )

    assert case["expected_behavior"] == "scope_note"


def test_normalize_case_validates_expected_response_profile() -> None:
    case = _normalize_case(
        {
            "id": "dates",
            "query": "Когда проходит форум Машук?",
            "expected_response_profile": "dates",
        }
    )

    assert case["expected_response_profile"] == "dates"
    assert case["forbidden_response_profiles"] == [
        "application",
        "selection_status",
        "travel",
    ]

    with pytest.raises(ValueError, match="expected_response_profile"):
        _normalize_case(
            {
                "id": "invalid-profile",
                "query": "Когда проходит форум?",
                "expected_response_profile": "travel_and_dates",
            }
        )


def test_normalize_case_unions_explicit_and_default_forbidden_profiles() -> None:
    case = _normalize_case(
        {
            "id": "dates-with-explicit-forbidden",
            "query": "Когда проходит форум Машук?",
            "expected_response_profile": "dates",
            "forbidden_response_profiles": ["food", "travel"],
        }
    )

    assert case["forbidden_response_profiles"] == [
        "application",
        "food",
        "selection_status",
        "travel",
    ]


def test_score_case_checks_routing_profile_from_trace() -> None:
    case = _normalize_case(
        {
            "id": "dates",
            "query": "Когда проходит форум Машук?",
            "expected_behavior": "answer",
            "expected_response_profile": "dates",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Форум проходит с 10 по 14 августа.",
        "latency_ms": 120,
        "error": None,
    }

    matched = score_case(
        case,
        http_result,
        {
            "was_escalated": False,
            "query_analysis": {"response_profile": "dates"},
        },
    )
    mismatched = score_case(
        case,
        http_result,
        {
            "was_escalated": False,
            "query_analysis": '{"response_profile": "travel"}',
        },
    )

    assert matched["routing_response_profile_match"] is True
    assert matched["passed"] is True
    assert mismatched["routing_response_profile_match"] is False
    assert mismatched["passed"] is False
    assert (
        "routing_response_profile_mismatch:dates!=travel"
        in mismatched["failure_reasons"]
    )


def test_score_case_fails_profile_check_without_trace() -> None:
    case = _normalize_case(
        {
            "id": "dates-no-trace",
            "query": "Когда проходит форум Машук?",
            "expected_response_profile": "dates",
        }
    )

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Форум проходит в августе.",
            "latency_ms": 120,
            "error": None,
        },
        None,
    )

    assert result["routing_response_profile_match"] is None
    assert result["passed"] is False
    assert "trace_missing" in result["failure_reasons"]


def test_score_case_rejects_forbidden_answer_profile_drift() -> None:
    case = _normalize_case(
        {
            "id": "dates-vs-travel",
            "query": "Когда проходит форум Машук?",
            "expected_behavior": "answer",
            "expected_response_profile": "dates",
            "forbidden_response_profiles": [
                "travel",
                "application",
            ],
        }
    )
    trace = {
        "was_escalated": False,
        "query_analysis": {"response_profile": "dates"},
    }

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Трансфер от вокзала до площадки будет бесплатным.",
            "latency_ms": 120,
            "error": None,
        },
        trace,
    )

    assert result["routing_response_profile_match"] is True
    assert result["detected_response_profiles"] == ["travel"]
    assert result["forbidden_response_profile_hits"] == ["travel"]
    assert result["forbidden_response_profiles_absent"] is False
    assert result["passed"] is False
    assert (
        "forbidden_response_profile_detected:travel"
        in result["failure_reasons"]
    )


def test_score_case_rejects_eval_only_travel_paraphrase_for_dates() -> None:
    case = _normalize_case(
        {
            "id": "dates-vs-travel-paraphrase",
            "query": "Когда проходит форум Машук?",
            "expected_behavior": "answer",
            "expected_response_profile": "dates",
        }
    )

    result = score_case(
        case,
        {
            "http_status": 200,
            "request_id": "11111111-1111-1111-1111-111111111111",
            "response": "Организаторы довезут участников от точки сбора.",
            "latency_ms": 120,
            "error": None,
        },
        {
            "was_escalated": False,
            "query_analysis": {"response_profile": "dates"},
        },
    )

    assert result["detected_response_profiles"] == ["travel"]
    assert result["forbidden_response_profile_hits"] == ["travel"]
    assert result["forbidden_response_profiles_absent"] is False
    assert result["passed"] is False


def test_normalize_case_infers_scope_note_from_seed_topic() -> None:
    case = _normalize_case(
        {
            "id": "seed_balanced::xlsx_fallback_r0022_offtop_ne_po_rosmolodezhi",
            "query": "как починить телефон",
            "expected_chunk_ids": ["xlsx_fallback_r0022_offtop_ne_po_rosmolodezhi"],
            "expected_cited_chunk_ids": ["xlsx_fallback_r0022_offtop_ne_po_rosmolodezhi"],
            "tags": ["seed_balanced", "topic:offtop_ne_po_rosmolodezhi"],
        }
    )

    assert case["expected_behavior"] == "scope_note"
    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []


def test_normalize_case_infers_escalation_from_seed_topic() -> None:
    case = _normalize_case(
        {
            "id": "seed_balanced::xlsx_fallback_r0017_pereklyuchit_na_operatora",
            "query": "Жду ответ оператора",
            "expected_chunk_ids": ["xlsx_fallback_r0017_pereklyuchit_na_operatora"],
            "expected_cited_chunk_ids": ["xlsx_fallback_r0017_pereklyuchit_na_operatora"],
            "tags": ["seed_balanced", "topic:pereklyuchit_na_operatora"],
        }
    )

    assert case["expected_behavior"] == "escalate"
    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []


def test_normalize_case_infers_clarify_for_generic_application_query() -> None:
    case = _normalize_case(
        {
            "id": "seed_balanced::xlsx_category_r0007_podat_zayavku_na_uchastie",
            "query": "Подать заявку на участие",
            "expected_chunk_ids": ["xlsx_category_r0007_podat_zayavku_na_uchastie"],
            "expected_cited_chunk_ids": ["xlsx_category_r0007_podat_zayavku_na_uchastie"],
        }
    )

    assert case["expected_behavior"] == "clarify"
    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []


def test_score_case_accepts_scope_note_behavior() -> None:
    case = _normalize_case(
        {
            "id": "weather",
            "query": "Какая погода завтра?",
            "expected_behavior": "scope_note",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": (
            "Я отвечаю на вопросы по мероприятиям, форумам, ФГАИС «Молодёжь России» "
            "и грантам Росмолодёжи. Задай, пожалуйста, вопрос по этим темам."
        ),
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": False})

    assert result["observed_behavior"] == "scope_note"
    assert result["behavior_match"] is True
    assert result["passed"] is True


def test_score_case_accepts_clarify_behavior() -> None:
    case = _normalize_case(
        {
            "id": "generic-application",
            "query": "Подать заявку на участие",
            "expected_behavior": "clarify",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Уточни, пожалуйста, речь о форуме, мероприятии или грантовом конкурсе?",
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": False})

    assert result["observed_behavior"] == "clarify"


def test_score_case_treats_final_clarification_as_clarify_with_stale_citations() -> None:
    case = {
        "id": "clarify_after_verify",
        "query": "а это не форум, а программа",
        "expected_behavior": "clarify",
        "expected_escalated": False,
    }
    http_result = {
        "http_status": 200,
        "request_id": "request-clarify-after-verify",
        "response": "Уточни, пожалуйста, точное название программы.",
        "latency_ms": 10,
        "error": None,
    }
    trace = {
        "was_escalated": False,
        "cited_sources": ["stale_retrieval_source"],
    }

    result = score_case(case, http_result, trace)

    assert result["observed_behavior"] == "clarify"
    assert result["passed"] is True
    assert result["passed"] is True


def test_score_case_does_not_treat_supported_answer_wording_as_clarify() -> None:
    case = _normalize_case(
        {
            "id": "sourced-answer-with-clarify-word",
            "query": "Можно ли с ОВЗ?",
            "expected_behavior": "answer",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": (
            "Участники с ОВЗ могут участвовать. Детали можно уточнить через "
            "Службу Заботы организаторов."
        ),
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(
        case,
        http_result,
        {"was_escalated": False, "cited_sources": ["ovz"]},
    )

    assert result["observed_behavior"] == "answer"
    assert result["passed"] is True


def test_score_case_accepts_escalate_behavior() -> None:
    case = _normalize_case(
        {
            "id": "operator",
            "query": "Позови оператора",
            "expected_behavior": "escalate",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Передаю обращение специалисту.",
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": True})

    assert result["observed_behavior"] == "escalate"
    assert result["passed"] is True


def test_score_case_can_require_masked_trace_text() -> None:
    case = _normalize_case(
        {
            "id": "pii-phone",
            "query": "Мой телефон +7 999 123-45-67, как зарегистрироваться?",
            "expected_behavior": "answer",
            "expected_message_masked_contains": ["[ТЕЛЕФОН]"],
            "forbidden_message_masked_contains": ["+7 999 123-45-67"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Ответ по регистрации.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {"message_masked": "Мой телефон [ТЕЛЕФОН], как зарегистрироваться?"}

    result = score_case(case, http_result, trace)

    assert result["message_masked_contains_match"] is True
    assert result["message_masked_forbidden_absent_match"] is True
    assert result["passed"] is True


def test_score_case_fails_when_masked_trace_contains_raw_pii() -> None:
    case = _normalize_case(
        {
            "id": "pii-phone",
            "query": "Мой телефон +7 999 123-45-67, как зарегистрироваться?",
            "expected_behavior": "answer",
            "forbidden_message_masked_contains": ["+7 999 123-45-67"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Ответ по регистрации.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {"message_masked": "Мой телефон +7 999 123-45-67, как зарегистрироваться?"}

    result = score_case(case, http_result, trace)

    assert result["message_masked_forbidden_absent_match"] is False
    assert result["passed"] is False
    assert "message_masked_forbidden_contains_raw_pii" in result["failure_reasons"]


def test_score_case_rejects_behavior_mismatch() -> None:
    case = _normalize_case(
        {
            "id": "generic-application",
            "query": "Подать заявку на участие",
            "expected_behavior": "clarify",
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подать заявку можно на странице мероприятия.",
        "latency_ms": 120,
        "error": None,
    }

    result = score_case(case, http_result, {"was_escalated": False})

    assert result["observed_behavior"] == "answer"
    assert result["passed"] is False
    assert result["failure_reasons"] == ["behavior_mismatch:clarify!=answer"]


def test_score_case_rejects_false_insufficient_source_answer() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Ответа на вопрос о проезде в источниках нет.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "ai-sage/GigaChat3-10B-A1.8B",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["no_false_insufficient_source_response"] is False
    assert result["passed"] is False


def test_score_case_rejects_non_answer_source_reference() -> None:
    case = _normalize_case(
        {
            "id": "travel",
            "query": "Кто платит за дорогу?",
            "expected_chunk_ids": ["travel"],
            "expected_escalated": False,
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Информация о проезде уже была предоставлена в источнике.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "ai-sage/GigaChat3-10B-A1.8B",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["no_non_answer_response"] is False
    assert result["passed"] is False


def test_score_case_requires_all_expected_chunks_when_multiple_are_declared() -> None:
    case = _normalize_case(
        {
            "id": "multi",
            "query": "Проезд и проживание?",
            "expected_chunk_ids": ["travel", "housing"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивает направляющая сторона.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is False
    assert result["missing_expected_chunk_ids"] == ["housing"]
    assert result["passed"] is False


def test_score_case_requires_expected_cited_chunks() -> None:
    case = _normalize_case(
        {
            "id": "multi",
            "query": "Проезд и проживание?",
            "expected_chunk_ids": ["travel", "housing"],
            "expected_cited_chunk_ids": ["travel", "housing"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Проезд оплачивает направляющая сторона.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["travel"],
        "retrieved_chunks": [{"chunk_id": "housing"}],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["expected_cited_chunk_hit"] is False
    assert result["missing_expected_cited_chunk_ids"] == ["housing"]
    assert result["cited_source_ids"] == ["travel"]
    assert result["cited_source_types"] == ["unknown"]
    assert result["passed"] is False
    assert result["failure_reasons"] == ["expected_chunk_not_cited"]


def test_score_case_accepts_equivalent_cited_chunk() -> None:
    case = _normalize_case(
        {
            "id": "equivalent",
            "query": "Where is the status?",
            "expected_chunk_ids": ["expected_status"],
            "expected_cited_chunk_ids": ["expected_status"],
            "equivalent_chunk_ids": {"expected_status": ["neighbor_status"]},
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Status answer.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["neighbor_status"],
        "retrieved_chunks": [{"chunk_id": "expected_status"}],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["expected_chunk_hit"] is True
    assert result["expected_cited_chunk_hit"] is False
    assert result["expected_cited_or_equivalent_chunk_hit"] is True
    assert result["passed"] is True
    assert result["failure_reasons"] == []


def test_score_case_reports_cited_source_types() -> None:
    case = _normalize_case(
        {
            "id": "source-type",
            "query": "Почему отклонили заявку?",
            "expected_chunk_ids": ["ticket_answer_bank_006"],
            "expected_cited_chunk_ids": ["ticket_answer_bank_006"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Причина отклонения доступна в личном кабинете.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["xlsx_category_r0004_otkazali_v_zayavke"],
        "retrieved_chunks": [
            {
                "chunk_id": "xlsx_category_r0004_otkazali_v_zayavke",
                "metadata": {"source_type": "xlsx"},
            }
        ],
        "reranker_scores": [
            {
                "chunk_id": "ticket_answer_bank_006",
                "metadata": {"source_type": "ticket_answer_bank"},
            }
        ],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_ids"] == ["xlsx_category_r0004_otkazali_v_zayavke"]
    assert result["cited_source_types"] == ["xlsx"]
    assert result["failure_reasons"] == ["expected_chunk_not_cited"]


def test_normalize_case_accepts_allowed_cited_source_types() -> None:
    case = _normalize_case(
        {
            "id": "yonote-policy",
            "query": "Что такое Росмолодёжь?",
            "allowed_cited_source_types": [" Yonote ", "yonote"],
        }
    )

    assert case["allowed_cited_source_types"] == ["yonote"]


def test_score_case_rejects_any_citation_outside_allowed_source_types() -> None:
    expected_chunk = "yonote_api_source_s0001_fact"
    case = _normalize_case(
        {
            "id": "yonote-only-policy",
            "query": "Что подтверждает источник?",
            "expected_chunk_ids": [expected_chunk],
            "expected_cited_chunk_ids": [expected_chunk],
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подтверждённый ответ.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": [
            expected_chunk,
            "xlsx_category_r0001_legacy",
        ],
        "retrieved_chunks": [
            {
                "chunk_id": expected_chunk,
                "metadata": {"source_type": "yonote"},
            },
            {
                "chunk_id": "xlsx_category_r0001_legacy",
                "metadata": {"source_type": "xlsx"},
            },
        ],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_types"] == ["xlsx", "yonote"]
    assert result["unexpected_cited_source_types"] == ["xlsx"]
    assert result["cited_source_types_allowed"] is False
    assert result["passed"] is False
    assert result["failure_reasons"] == ["forbidden_cited_source_type"]


def test_score_case_infers_yonote_source_type_from_chunk_id() -> None:
    expected_chunk = "yonote_api_source_s0001_fact"
    case = _normalize_case(
        {
            "id": "yonote-prefix",
            "query": "Что подтверждает источник?",
            "expected_chunk_ids": [expected_chunk],
            "expected_cited_chunk_ids": [expected_chunk],
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подтверждённый ответ.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": [expected_chunk],
        "retrieved_chunks": [],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_types"] == ["yonote"]
    assert result["unexpected_cited_source_types"] == []
    assert result["cited_source_types_allowed"] is True
    assert result["passed"] is True


def test_clarification_rejects_forbidden_residual_citation_source() -> None:
    case = _normalize_case(
        {
            "id": "clarify-source-policy",
            "query": "Когда проходит мероприятие?",
            "expected_behavior": "clarify",
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Уточни, пожалуйста, название мероприятия.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": ["xlsx_category_r0001_legacy"],
        "retrieved_chunks": [
            {
                "chunk_id": "xlsx_category_r0001_legacy",
                "metadata": {"source_type": "xlsx"},
            }
        ],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": None,
    }

    result = score_case(case, http_result, trace)

    assert case["expected_chunk_ids"] == []
    assert case["expected_cited_chunk_ids"] == []
    assert case["allowed_cited_source_types"] == ["yonote"]
    assert result["observed_behavior"] == "clarify"
    assert result["cited_source_types_allowed"] is False
    assert result["passed"] is False
    assert result["failure_reasons"] == ["forbidden_cited_source_type"]


def test_cited_source_type_metadata_is_case_insensitive() -> None:
    expected_chunk = "custom_yonote_chunk"
    case = _normalize_case(
        {
            "id": "yonote-metadata-case",
            "query": "Что подтверждает источник?",
            "expected_chunk_ids": [expected_chunk],
            "expected_cited_chunk_ids": [expected_chunk],
            "allowed_cited_source_types": ["yonote"],
        }
    )
    http_result = {
        "http_status": 200,
        "request_id": "11111111-1111-1111-1111-111111111111",
        "response": "Подтверждённый ответ.",
        "latency_ms": 120,
        "error": None,
    }
    trace = {
        "cited_sources": [expected_chunk],
        "retrieved_chunks": [
            {
                "chunk_id": expected_chunk,
                "metadata": {"source_type": " Yonote "},
            }
        ],
        "reranker_scores": [],
        "was_escalated": False,
        "generator_model": "source_chunk",
    }

    result = score_case(case, http_result, trace)

    assert result["cited_source_types"] == ["yonote"]
    assert result["cited_source_types_allowed"] is True
    assert result["passed"] is True


def test_quality_gate_failures_are_opt_in_and_fail_closed() -> None:
    metrics = {
        "pass_rate": 0.95,
        "trace_coverage_rate": None,
    }

    assert _quality_gate_failures(
        metrics,
        fail_on_any_case=False,
        require_complete_traces=False,
    ) == []
    assert _quality_gate_failures(
        metrics,
        fail_on_any_case=True,
        require_complete_traces=True,
    ) == [
        "case_pass_rate_below_100_percent",
        "trace_coverage_below_100_percent",
    ]


def test_quality_gate_accepts_only_exact_complete_rates() -> None:
    assert _quality_gate_failures(
        {
            "pass_rate": 1.0,
            "trace_coverage_rate": 1.0,
        },
        fail_on_any_case=True,
        require_complete_traces=True,
    ) == []


def test_score_case_classifies_infrastructure_http_error() -> None:
    case = _normalize_case(
        {
            "id": "infra",
            "query": "Привет",
            "expected_chunk_ids": ["hello"],
        }
    )
    http_result = {
        "http_status": None,
        "request_id": None,
        "response": "",
        "latency_ms": 120,
        "error": "ConnectError: All connection attempts failed",
    }

    result = score_case(case, http_result, None)

    assert result["passed"] is False
    assert result["failure_reasons"] == [
        "http_error",
        "expected_chunk_not_observed",
    ]


def test_summarize_results_counts_core_metrics() -> None:
    metrics = summarize_results(
        [
            {
                "passed": True,
                "http_success": True,
                "expected_chunk_ids": ["a"],
                "expected_chunk_hit": True,
                "expected_answer_contains": [],
                "trace_found": True,
                "was_escalated": False,
                "cache_hit": False,
                "generator_model": "source_chunk",
                "max_reranker_score": 0.9,
                "latency_ms": 100,
                "trace_total_latency_ms": 90,
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "llm_total_tokens": 0,
                "llm_estimated_cost_rub": 0.0,
                "llm_usage": [],
            },
            {
                "passed": False,
                "http_success": True,
                "failure_reasons": ["unexpected_escalation"],
                "expected_chunk_ids": ["b"],
                "expected_chunk_hit": True,
                "expected_answer_contains": [],
                "trace_found": True,
                "was_escalated": True,
                "escalation_reason": "low_confidence",
                "cache_hit": False,
                "generator_model": None,
                "max_reranker_score": 0.05,
                "latency_ms": 300,
                "trace_total_latency_ms": 250,
                "llm_prompt_tokens": 10,
                "llm_completion_tokens": 5,
                "llm_total_tokens": 15,
                "llm_estimated_cost_rub": 0.01,
                "llm_usage": [{"model": "m", "total_tokens": 15}],
            },
        ],
        target="http://test/ask",
        cases_path=Path("cases.json"),
    )

    assert metrics["cases_total"] == 2
    assert metrics["pass_rate"] == 0.5
    assert metrics["expected_chunk_hit_rate"] == 1.0
    assert metrics["escalation_rate"] == 0.5
    assert metrics["source_chunk_rate"] == 0.5
    assert metrics["low_confidence_expected_chunk_hits"] == 1
    assert metrics["low_confidence_expected_chunk_hit_rate"] == 0.5
    assert metrics["reranker_score"] == {"avg": 0.475, "p50": 0.05, "p95": 0.9, "max": 0.9}
    assert metrics["latency_ms"]["p95"] == 300
    assert metrics["llm_total_tokens"] == 15
    assert metrics["llm_estimated_cost_rub"] == 0.01
    assert metrics["failure_reason_counts"] == {"unexpected_escalation": 1}
    assert metrics["likely_infrastructure_failure"] is False


def test_summarize_results_marks_likely_infrastructure_failure() -> None:
    metrics = summarize_results(
        [
            {
                "passed": False,
                "http_success": False,
                "failure_reasons": ["http_error"],
                "expected_chunk_ids": ["a"],
                "expected_answer_contains": [],
                "trace_found": False,
                "latency_ms": 100,
                "llm_prompt_tokens": 0,
                "llm_completion_tokens": 0,
                "llm_total_tokens": 0,
                "llm_estimated_cost_rub": 0.0,
                "llm_usage": [],
            }
        ],
        target="http://test/ask",
        cases_path=Path("cases.json"),
    )

    assert metrics["failure_reason_counts"] == {"http_error": 1}
    assert metrics["likely_infrastructure_failure"] is True


@pytest.mark.asyncio
async def test_run_eval_writes_json_and_markdown_without_db(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    markdown = tmp_path / "ask_metrics.md"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "hello",
                    "query": "Привет",
                    "expected_answer_contains": ["Здравствуйте"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["text"] == "Привет"
        assert request.headers["X-Eval-Run-Id"].startswith("ask-eval-")
        assert request.headers["X-Eval-Case-Id"] == "hello"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        markdown_path=markdown,
        transport=httpx.MockTransport(handler),
    )

    assert metrics["cases_total"] == 1
    assert metrics["pass_rate"] == 1.0
    assert metrics["eval_run_id"].startswith("ask-eval-")
    assert json.loads(output.read_text(encoding="utf-8"))["results"][0]["passed"] is True
    assert "Ask Eval Report" in markdown.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_eval_can_send_bypass_cache_header(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps([{"id": "hello", "query": "Привет"}], ensure_ascii=False),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Bypass-Cache"] == "1"
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "Здравствуйте!",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        bypass_cache=True,
    )

    assert metrics["cases_total"] == 1
    assert metrics["http_success_rate"] == 1.0


@pytest.mark.asyncio
async def test_run_eval_blocks_large_live_run_without_budget(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [{"id": f"case-{idx}", "query": f"Вопрос {idx}"} for idx in range(21)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit LLM budget"):
        await run_eval(
            cases_path=cases,
            output_path=output,
            target="http://localhost:8001/ask",
            trace_lookup=False,
            api_key_env=None,
        )

    assert not output.exists()


@pytest.mark.asyncio
async def test_run_eval_allows_large_mock_run_without_budget(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [{"id": f"case-{idx}", "query": f"Вопрос {idx}"} for idx in range(21)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
    )

    assert metrics["cases_total"] == 21
    assert metrics["llm_budget_rub"] is None
    assert metrics["llm_budget_exceeded"] is None


@pytest.mark.asyncio
async def test_run_eval_marks_budget_status_and_case_limit(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [{"id": f"case-{idx}", "query": f"Вопрос {idx}"} for idx in range(5)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    metrics = await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        max_cases=2,
        max_llm_cost_rub=0.0,
    )

    assert metrics["cases_total"] == 2
    assert metrics["cases_original_total"] == 5
    assert metrics["cases_limit"] == 2
    assert metrics["cases_limited"] is True
    assert metrics["llm_budget_rub"] == 0.0
    assert metrics["llm_budget_exceeded"] is False


@pytest.mark.asyncio
async def test_run_eval_user_prefix_isolates_loaded_case_users(tmp_path: Path) -> None:
    cases = tmp_path / "ask_cases.json"
    output = tmp_path / "ask_metrics.json"
    cases.write_text(
        json.dumps(
            [
                {"id": "one", "query": "Первый", "user_id": "same"},
                {"id": "two", "query": "Второй", "user_id": "same"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seen_user_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen_user_ids.append(payload["user_id"])
        return httpx.Response(
            200,
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "response": "OK",
            },
        )

    await run_eval(
        cases_path=cases,
        output_path=output,
        target="http://test/ask",
        trace_lookup=False,
        api_key_env=None,
        transport=httpx.MockTransport(handler),
        generated_user_prefix="isolated",
    )

    assert seen_user_ids == ["isolated-1", "isolated-2"]
