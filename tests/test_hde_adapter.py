from __future__ import annotations

from src.channels.hde import HDEAdapter
from src.models import Channel


def test_hde_adapter_parses_nested_new_ticket_payload_and_strips_trigger_prefix() -> None:
    payload = {
        "event": "new_message",
        "chat_id": "ticket-123",
        "visitor": {"id": "user-456", "fields": {"name": "Test User"}},
        "message": {
            "kind": "visitor",
            "text": "slcb373n93f Как зарегистрироваться на форум?",
        },
    }

    message = HDEAdapter(trigger_prefix="slcb373n93f").parse(payload)

    assert message.user_id == "ticket-123"
    assert message.channel == Channel.HDE
    assert message.text == "Как зарегистрироваться на форум?"
    assert message.attachments == []


def test_hde_adapter_parses_nested_new_reply_payload_without_prefix() -> None:
    payload = {
        "event": "new_message",
        "chat_id": "ticket-123",
        "visitor": {"id": "user-456", "fields": {"name": "Test User"}},
        "message": {
            "kind": "visitor",
            "text": "Не пришло письмо по форуму, что делать?",
        },
    }

    message = HDEAdapter(trigger_prefix="slcb373n93f").parse(payload)

    assert message.user_id == "ticket-123"
    assert message.channel == Channel.HDE
    assert message.text == "Не пришло письмо по форуму, что делать?"


def test_hde_adapter_keeps_legacy_flat_payload_support() -> None:
    payload = {
        "ticket_id": "legacy-ticket",
        "text": "Передайте оператору",
        "attachments": {"id": "file-1"},
    }

    message = HDEAdapter().parse(payload)

    assert message.user_id == "legacy-ticket"
    assert message.channel == Channel.HDE
    assert message.text == "Передайте оператору"
    assert message.attachments == [{"id": "file-1"}]
