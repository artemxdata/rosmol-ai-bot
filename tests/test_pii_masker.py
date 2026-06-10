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
