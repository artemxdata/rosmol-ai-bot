from __future__ import annotations

import pytest


@pytest.fixture
def sample_chunks():
    from src.models import Chunk

    return [
        Chunk(chunk_id="ctx_1", text="Проезд на Машук участник оплачивает самостоятельно."),
        Chunk(chunk_id="ctx_2", text="Регистрация проходит на платформе ФГАИС."),
    ]
