from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.response_contract import (
    EMOJI_OWNERS,
    REQUIRED_MESSAGE_IDS,
    RESPONSE_CONTRACT_PATH,
    ResponseContract,
    ResponseContractError,
    ResponseProfileName,
    load_response_contract,
)


@pytest.fixture
def contract() -> ResponseContract:
    return load_response_contract()


def _raw_contract() -> dict:
    return json.loads(RESPONSE_CONTRACT_PATH.read_text(encoding="utf-8"))


def _walk_json(value: object) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    strings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.append(str(key))
            nested_keys, nested_strings = _walk_json(nested)
            keys.extend(nested_keys)
            strings.extend(nested_strings)
    elif isinstance(value, list):
        for nested in value:
            nested_keys, nested_strings = _walk_json(nested)
            keys.extend(nested_keys)
            strings.extend(nested_strings)
    elif isinstance(value, str):
        strings.append(value)
    return keys, strings


def test_contract_loads_as_strict_versioned_model(contract: ResponseContract) -> None:
    assert contract.contract_version == "1.0.0"
    assert contract.language == "ru"
    assert contract.address_form == "ты"
    assert contract.legacy_nlu_role == "approved_copy_and_structure_only"
    assert contract.fact_policy.authority == "published_yonote_release_snapshot"
    assert contract.fact_policy.source_type == "yonote"
    assert contract.fact_policy.live_runtime_lookup is False
    assert contract.fact_policy.legacy_factual_answers_allowed is False
    assert contract.fact_policy.operator_answers_allowed_as_facts is False
    assert set(message.id for message in contract.messages) == REQUIRED_MESSAGE_IDS


def test_canonical_service_copy_is_preserved(contract: ResponseContract) -> None:
    assert contract.message("greeting").text == "Чем я могу быть полезен?"
    assert (
        contract.message("operator_transfer").text
        == "Перевожу на оператора. Пожалуйста, ожидай."
    )
    assert contract.message("gratitude").variants == (
        "Всегда рад!",
        "Рад быть полезным!",
    )
    assert contract.message("farewell").text == "Обращайся! 😊"
    assert (
        contract.message("unknown_forum").text
        == "Уточни, пожалуйста, название форума, о котором спрашиваешь."
    )
    assert (
        contract.message("dates_event_clarification").text
        == "Подскажи, пожалуйста, даты какого мероприятия тебя интересуют?"
    )
    assert (
        contract.message("unclear_request").text
        == "Я не совсем понял вопрос. Попробуй переформулировать — я помогу "
        "с мероприятиями, грантами и техническими вопросами."
    )
    assert (
        contract.message("capabilities").text
        == "ЗаБотливый Бот создан, чтобы информировать о деятельности Росмолодёжи, "
        "форумах, грантах и возможностях для молодёжи.\n\n"
        "Спрашивай о мероприятиях, проектах и поддержке — я найду ответ!"
    )


def test_text_only_clarification_template_has_no_legacy_button_placeholder(
    contract: ResponseContract,
) -> None:
    clarification = contract.message("clarification_with_options")

    assert clarification.template == "Пожалуйста, уточни свой вопрос 👇\n{options}"
    assert clarification.required_placeholders == ("options",)
    assert clarification.options_style == "numbered_text"
    assert "{buttons}" not in clarification.template
    assert "omnichannel_buttons" not in clarification.template


def test_catalog_contains_no_legacy_facts_urls_dates_or_secrets() -> None:
    raw = _raw_contract()
    keys, strings = _walk_json(raw)
    serialized = json.dumps(raw, ensure_ascii=False).casefold()
    forbidden_copy_fields = {
        "answer",
        "answer_text",
        "bot_message",
        "conditions",
        "credentials",
        "dates",
        "dates_mentioned",
        "fact_values",
        "facts",
        "factual_copy",
        "legacy_copy",
        "link",
        "links",
        "phones",
        "reference_answer",
        "registration_url",
        "text_clean",
        "text_raw",
        "url",
        "urls",
    }
    secret_markers = (
        "api_key",
        "authorization",
        "bearer",
        "credential",
        "dsn",
        "password",
        "secret",
        "token",
        "пароль",
        "секрет",
        "токен",
    )

    assert not ({key.casefold() for key in keys} & forbidden_copy_fields)
    assert not re.search(r"https?://|www\.", serialized)
    assert not re.search(r"\b(?:19|20)\d{2}\b", serialized)
    assert not re.search(
        r"\b\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})\b",
        serialized,
    )
    assert not re.search(
        r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|"
        r"августа|сентября|октября|ноября|декабря)\b",
        serialized,
    )
    assert all(marker not in serialized for marker in secret_markers)
    assert all("{buttons}" not in text.casefold() for text in strings)


def test_catalog_allows_only_the_reviewed_options_placeholder() -> None:
    _, strings = _walk_json(_raw_contract())
    placeholders = {
        placeholder
        for text in strings
        for placeholder in re.findall(r"\{[^{}]+\}", text)
    }

    assert placeholders == {"{options}"}


def test_gratitude_variant_selection_is_deterministic(
    contract: ResponseContract,
) -> None:
    gratitude = contract.message("gratitude")

    first = gratitude.select_text("ticket-42")
    assert first == gratitude.select_text("ticket-42")
    assert first in gratitude.variants


def test_only_approved_emojis_have_exact_message_owners(
    contract: ResponseContract,
) -> None:
    assert contract.global_allowed_emojis == ("😊", "👇")
    actual_owners = {
        emoji: {
            message.id
            for message in contract.messages
            if emoji in message.allowed_emojis
        }
        for emoji in contract.global_allowed_emojis
    }
    assert actual_owners == {
        emoji: set(owners) for emoji, owners in EMOJI_OWNERS.items()
    }


def test_unapproved_emoji_is_rejected() -> None:
    raw = _raw_contract()
    greeting = next(message for message in raw["messages"] if message["id"] == "greeting")
    greeting["text"] += " 🚀"

    with pytest.raises(ValidationError, match="emoji content mismatch"):
        ResponseContract.model_validate(raw)


def test_limits_and_composition_are_fixed(contract: ResponseContract) -> None:
    assert contract.limits.simple_max_chars == 450
    assert contract.limits.compound_max_chars == 900
    assert contract.composition.section_order == (
        "direct_answer",
        "necessary_details",
        "source_link_or_next_step",
    )
    assert contract.composition.max_source_links == 1
    assert contract.composition.mechanical_truncation_allowed is False
    assert contract.composition.llm_may_add_emoji is False
    assert contract.composition.text_only_options is True


def test_message_over_its_limit_is_rejected() -> None:
    raw = _raw_contract()
    greeting = next(message for message in raw["messages"] if message["id"] == "greeting")
    greeting["text"] = "а" * 451

    with pytest.raises(ValidationError, match="exceeds its character limit"):
        ResponseContract.model_validate(raw)


def test_all_response_profiles_are_declared_once(
    contract: ResponseContract,
) -> None:
    expected = {profile.value for profile in ResponseProfileName}

    assert {profile.name.value for profile in contract.profiles} == expected
    assert len(contract.profiles) == len(expected)
    assert (
        contract.profile(ResponseProfileName.DATES).clarification_message_id
        == "dates_event_clarification"
    )


def test_loader_rejects_invalid_json_without_echoing_contents(tmp_path: Path) -> None:
    invalid_path = tmp_path / "response-contract.json"
    invalid_path.write_text('{"api_key": "must-not-be-echoed"', encoding="utf-8")

    with pytest.raises(ResponseContractError) as error:
        load_response_contract(invalid_path)

    assert "must-not-be-echoed" not in str(error.value)
