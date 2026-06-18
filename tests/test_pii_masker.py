from __future__ import annotations

from src.security.pii_masker import PIIMasker


def test_masks_email_phone_passport_and_date() -> None:
    masker = PIIMasker()
    text = (
        "Мой email test@example.com, телефон +7 999 123-45-67, "
        "паспорт 1234 567890, дата 10.06.2026"
    )

    masked, mapping = masker.mask(text)

    assert "test@example.com" not in masked
    assert "+7 999 123-45-67" not in masked
    assert "1234 567890" not in masked
    assert "10.06.2026" not in masked
    assert mapping["email"] == ["test@example.com"]
    assert mapping["phone"] == ["+7 999 123-45-67"]
    assert mapping["passport"] == ["1234 567890"]
    assert mapping["date"] == ["10.06.2026"]


def test_masks_plain_russian_phone_without_plus() -> None:
    masker = PIIMasker()

    masked, mapping = masker.mask("Позвоните 79833384190 или 89991234567")

    assert "79833384190" not in masked
    assert "89991234567" not in masked
    assert masked == "Позвоните [ТЕЛЕФОН] или [ТЕЛЕФОН]"
    assert mapping["phone"] == ["79833384190", "89991234567"]
