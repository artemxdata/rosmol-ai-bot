from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.graph.response_profiles import (
    chunk_has_event_date_evidence,
    detect_response_profiles,
    response_has_cross_aspect_drift,
    response_has_cross_aspect_drift_for_profiles,
)
from src.response_contract import ResponseProfileName


def _published_seed_record(chunk_id: str) -> dict:
    seed_path = Path(__file__).resolve().parents[1] / "data" / "knowledge_base_seed.json"
    records = json.loads(seed_path.read_text(encoding="utf-8"))
    return next(record for record in records if record.get("chunk_id") == chunk_id)


@pytest.mark.parametrize(
    "chunk_id",
    [
        "yonote_api_rfgkfljjld_s0017_registraciya",
        "yonote_api_gkby3eml8d_s0002_registraciya",
        "yonote_api_gnu4bmbubc_s0002_registraciya",
        "yonote_api_bhjm352dd6_s0003_registraciya",
        "yonote_api_fd4f7puzzj_s0003_registraciya",
        "yonote_api_jzxsccnczf_s0006_etapy",
        "yonote_api_2rhqotnyz9_s0009_kanal_v_tg_t_me_villagedon",
    ],
)
def test_registration_and_selection_seed_chunks_are_not_event_date_evidence(
    chunk_id: str,
) -> None:
    record = _published_seed_record(chunk_id)

    assert not chunk_has_event_date_evidence(record["text_clean"], record)


@pytest.mark.parametrize(
    "chunk_id",
    [
        "yonote_api_lrcgc2vl3g_s0005_dedlayn_podachi_zayavki_na_smenu",
        "yonote_api_cljqo2rlvk_s0027_festival_molodogo_iskusstva",
    ],
)
def test_mixed_seed_chunks_keep_real_event_range_evidence(chunk_id: str) -> None:
    record = _published_seed_record(chunk_id)

    assert chunk_has_event_date_evidence(record["text_clean"], record)


def test_response_aspect_detector_distinguishes_event_date_from_deadline() -> None:
    assert detect_response_profiles("Форум пройдёт 8 августа.") == {
        ResponseProfileName.DATES
    }
    deadline_profiles = detect_response_profiles(
        "Подача заявок проходит до 8 августа."
    )
    assert ResponseProfileName.APPLICATION in deadline_profiles
    assert ResponseProfileName.DATES not in deadline_profiles


def test_single_profile_guard_rejects_event_date_for_travel_question() -> None:
    assert response_has_cross_aspect_drift(
        ResponseProfileName.TRAVEL,
        "Форум пройдёт 8 августа.",
    )


def test_multi_profile_guard_requires_every_requested_aspect() -> None:
    expected = {
        ResponseProfileName.APPLICATION,
        ResponseProfileName.TRAVEL,
    }

    assert response_has_cross_aspect_drift_for_profiles(
        expected,
        "Заявку подают через личный кабинет.",
    )
    assert not response_has_cross_aspect_drift_for_profiles(
        expected,
        "Заявку подают через личный кабинет. Проезд участник оплачивает сам.",
    )
