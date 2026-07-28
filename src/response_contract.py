from __future__ import annotations

import hashlib
import json
import string
import unicodedata
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RESPONSE_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "response_contract_v1.json"
)

REQUIRED_MESSAGE_IDS = frozenset(
    {
        "greeting",
        "operator_transfer",
        "gratitude",
        "farewell",
        "unknown_forum",
        "dates_event_clarification",
        "unclear_request",
        "clarification_with_options",
        "capabilities",
    }
)
EMOJI_OWNERS = {
    "😊": frozenset({"farewell"}),
    "👇": frozenset({"clarification_with_options"}),
}


class ResponseContractError(ValueError):
    """The versioned response contract cannot be loaded safely."""


class ResponseProfileName(StrEnum):
    DATES = "dates"
    APPLICATION = "application"
    ELIGIBILITY = "eligibility"
    DOCUMENTS = "documents"
    SELECTION_STATUS = "selection_status"
    PROGRAM = "program"
    TRAVEL = "travel"
    ACCOMMODATION = "accommodation"
    FOOD = "food"
    ACCESSIBILITY = "accessibility"
    GRANTS = "grants"
    TECHNICAL = "technical"
    GENERIC = "generic"


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactPolicy(StrictContractModel):
    authority: Literal["published_yonote_release_snapshot"]
    source_type: Literal["yonote"]
    live_runtime_lookup: Literal[False]
    legacy_factual_answers_allowed: Literal[False]
    operator_answers_allowed_as_facts: Literal[False]
    missing_fact_actions: tuple[Literal["clarify", "controlled_escalation"], ...]

    @model_validator(mode="after")
    def validate_missing_fact_actions(self) -> FactPolicy:
        if self.missing_fact_actions != ("clarify", "controlled_escalation"):
            raise ValueError(
                "missing_fact_actions must remain clarify then controlled_escalation"
            )
        return self


class ResponseLimits(StrictContractModel):
    simple_max_chars: int = Field(gt=0)
    compound_max_chars: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_limit_order(self) -> ResponseLimits:
        if self.compound_max_chars <= self.simple_max_chars:
            raise ValueError("compound_max_chars must be greater than simple_max_chars")
        return self


class CompositionPolicy(StrictContractModel):
    section_order: tuple[
        Literal["direct_answer", "necessary_details", "source_link_or_next_step"], ...
    ]
    max_source_links: Literal[1]
    mechanical_truncation_allowed: Literal[False]
    llm_may_add_emoji: Literal[False]
    text_only_options: Literal[True]

    @model_validator(mode="after")
    def validate_section_order(self) -> CompositionPolicy:
        expected = (
            "direct_answer",
            "necessary_details",
            "source_link_or_next_step",
        )
        if self.section_order != expected:
            raise ValueError("response section order does not match contract v1")
        return self


class ServiceMessage(StrictContractModel):
    id: str = Field(min_length=1)
    kind: Literal["exact", "variants", "template"]
    complexity: Literal["simple", "compound"]
    text: str | None = None
    variants: tuple[str, ...] = ()
    template: str | None = None
    selection: Literal["stable_hash"] | None = None
    required_placeholders: tuple[str, ...] = ()
    options_style: Literal["numbered_text"] | None = None
    allowed_emojis: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> ServiceMessage:
        if self.kind == "exact":
            if not self.text or self.variants or self.template is not None:
                raise ValueError("exact message must contain only non-empty text")
            if self.selection is not None or self.required_placeholders or self.options_style:
                raise ValueError("exact message contains variant or template settings")
        elif self.kind == "variants":
            if self.text is not None or self.template is not None:
                raise ValueError("variant message cannot contain text or template")
            if len(self.variants) < 2 or any(not item.strip() for item in self.variants):
                raise ValueError("variant message requires at least two non-empty variants")
            if len(set(self.variants)) != len(self.variants):
                raise ValueError("variant message contains duplicate variants")
            if self.selection != "stable_hash":
                raise ValueError("variant selection must be deterministic")
            if self.required_placeholders or self.options_style:
                raise ValueError("variant message contains template settings")
        else:
            if self.text is not None or self.variants or not self.template:
                raise ValueError("template message must contain only a non-empty template")
            if self.selection is not None:
                raise ValueError("template message cannot define variant selection")
            placeholders = tuple(
                field_name
                for _, field_name, _, _ in string.Formatter().parse(self.template)
                if field_name is not None
            )
            if placeholders != self.required_placeholders:
                raise ValueError("template placeholders do not match required_placeholders")
            if len(set(placeholders)) != len(placeholders):
                raise ValueError("template placeholders must be unique")
            if self.options_style and "options" not in placeholders:
                raise ValueError("options_style requires an options placeholder")
        return self

    @property
    def user_facing_texts(self) -> tuple[str, ...]:
        if self.text is not None:
            return (self.text,)
        if self.template is not None:
            return (self.template,)
        return self.variants

    def select_text(self, stable_key: str = "") -> str:
        """Return exact/template text or a reproducible approved variant."""

        if self.kind != "variants":
            return self.user_facing_texts[0]
        digest = hashlib.sha256(
            f"{self.id}:{stable_key}".encode()
        ).digest()
        index = int.from_bytes(digest[:8], "big") % len(self.variants)
        return self.variants[index]


class ResponseProfile(StrictContractModel):
    name: ResponseProfileName
    guidance: str = Field(min_length=1)
    context_keys: tuple[str, ...] = ()
    clarification_message_id: str | None = None

    @model_validator(mode="after")
    def validate_context_keys(self) -> ResponseProfile:
        if any(not key.strip() for key in self.context_keys):
            raise ValueError("profile context keys must be non-empty")
        if len(set(self.context_keys)) != len(self.context_keys):
            raise ValueError("profile context keys must be unique")
        return self


class ResponseContract(StrictContractModel):
    contract_version: Literal["1.0.0"]
    language: Literal["ru"]
    address_form: Literal["ты"]
    legacy_nlu_role: Literal["approved_copy_and_structure_only"]
    fact_policy: FactPolicy
    limits: ResponseLimits
    composition: CompositionPolicy
    global_allowed_emojis: tuple[str, ...]
    messages: tuple[ServiceMessage, ...]
    profiles: tuple[ResponseProfile, ...]

    @model_validator(mode="after")
    def validate_contract(self) -> ResponseContract:
        if self.limits.simple_max_chars != 450 or self.limits.compound_max_chars != 900:
            raise ValueError("response contract v1 limits must be 450 and 900 characters")

        if self.global_allowed_emojis != tuple(EMOJI_OWNERS):
            raise ValueError("global emoji allowlist does not match response contract v1")

        message_by_id = _unique_by_id(self.messages)
        if set(message_by_id) != REQUIRED_MESSAGE_IDS:
            raise ValueError("response contract v1 message set is incomplete or unsupported")

        profile_by_name = _unique_profiles(self.profiles)
        expected_profiles = {profile.value for profile in ResponseProfileName}
        if set(profile_by_name) != expected_profiles:
            raise ValueError("response contract v1 profile set is incomplete or unsupported")

        for message in self.messages:
            expected_allowed = tuple(
                emoji
                for emoji, owners in EMOJI_OWNERS.items()
                if message.id in owners
            )
            if message.allowed_emojis != expected_allowed:
                raise ValueError(f"emoji ownership mismatch for message {message.id}")
            for text in message.user_facing_texts:
                max_chars = (
                    self.limits.simple_max_chars
                    if message.complexity == "simple"
                    else self.limits.compound_max_chars
                )
                if len(text) > max_chars:
                    raise ValueError(f"message {message.id} exceeds its character limit")
                symbols = _emoji_like_symbols(text)
                if symbols != set(message.allowed_emojis):
                    raise ValueError(f"emoji content mismatch for message {message.id}")

        message_ids = set(message_by_id)
        for profile in self.profiles:
            if (
                profile.clarification_message_id is not None
                and profile.clarification_message_id not in message_ids
            ):
                raise ValueError(
                    f"profile {profile.name} references an unknown clarification message"
                )
        return self

    def message(self, message_id: str) -> ServiceMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise KeyError(message_id)

    def profile(self, name: ResponseProfileName | str) -> ResponseProfile:
        profile_name = ResponseProfileName(name)
        for profile in self.profiles:
            if profile.name == profile_name:
                return profile
        raise KeyError(profile_name.value)


def load_response_contract(
    path: str | Path = RESPONSE_CONTRACT_PATH,
) -> ResponseContract:
    """Load the reviewed response contract without exposing invalid input in errors."""

    contract_path = Path(path)
    try:
        raw = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResponseContractError(
            f"Cannot read response contract: {contract_path}"
        ) from None
    try:
        return ResponseContract.model_validate(raw)
    except ValidationError:
        raise ResponseContractError(
            f"Invalid response contract: {contract_path}"
        ) from None


@lru_cache(maxsize=1)
def get_response_contract() -> ResponseContract:
    """Return the reviewed default contract without repeated filesystem reads."""

    return load_response_contract()


def contains_emoji_like_symbols(text: str) -> bool:
    """Return whether user-visible text contains emoji-like symbols."""

    return bool(_emoji_like_symbols(text))


def _unique_by_id(messages: tuple[ServiceMessage, ...]) -> dict[str, ServiceMessage]:
    result: dict[str, ServiceMessage] = {}
    for message in messages:
        if message.id in result:
            raise ValueError(f"duplicate service message id: {message.id}")
        result[message.id] = message
    return result


def _unique_profiles(
    profiles: tuple[ResponseProfile, ...],
) -> dict[str, ResponseProfile]:
    result: dict[str, ResponseProfile] = {}
    for profile in profiles:
        key = profile.name.value
        if key in result:
            raise ValueError(f"duplicate response profile: {key}")
        result[key] = profile
    return result


def _emoji_like_symbols(text: str) -> set[str]:
    symbols: set[str] = set()
    for character in text:
        codepoint = ord(character)
        if (
            unicodedata.category(character) == "So"
            or 0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or character in {"\u200d", "\ufe0f"}
        ):
            symbols.add(character)
    return symbols
