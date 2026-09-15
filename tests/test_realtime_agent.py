"""Tests for the realtime reconciliation agent + official-file adapters."""
import shutil
import tempfile
from pathlib import Path

import pytest

from recon_engine.adapters.official_files import core_inv
from recon_engine.realtime_agent import build_report, detect_role, sniff_role

REAL_DIR = Path.home() / "Desktop" / "Accounts setu dump"


def test_core_inv_normalisation():
    cases = {
        "JSIT-00023/25-26": "23", "JSIT-00248/25-26": "248",
        "T36/25-26": "T36", "RL/16/25-26": "16", "G/61/25-26": "61",
        "25-26/059": "59", "CD-12/04-2025": "12", "CD-197/04-2025": "197",
        "SRI-111/25-26": "111", "JSISC25-26-00142": "142",
        "26071C0000002683": "26071C0000002683", "5.08E+13": "5.08E+13",
        "16": "16",
    }
    for raw, expect in cases.items():
        assert core_inv(raw) == expect, f"{raw} -> {core_inv(raw)} != {expect}"


@pytest.mark.parametrize("name,role", [
    ("042025_07AAACP9472A1ZJ_GSTR2B_14052025 (1).xlsx", "portal_2b"),
    ("IMS_B2B_07AAACP9472A1ZJ_19052025 (1).csv", "portal_ims"),
    ("Purchase details.xlsx", "books_purchase"),
    ("Credit Note.xlsx", "books_credit"),
    ("random.pdf", None),
])
def test_detect_role(name, role):
    assert detect_role(name) == role


def test_sniff_role_real_files():
    if not REAL_DIR.exists():
        pytest.skip("real dump folder not present on this machine")
    for name in ["042025_07AAACP9472A1ZJ_GSTR2B_14052025 (1).xlsx",
                 "Purchase details.xlsx", "Credit Note.xlsx"]:
        p = REAL_DIR / name
        if p.exists():
            assert sniff_role(p) is not None, name


def _real_file_set():
    if not REAL_DIR.exists():
        return None
    files, missing = {}, []
    for name, role in [
        ("042025_07AAACP9472A1ZJ_GSTR2B_14052025 (1).xlsx", "portal_2b"),
        ("IMS_B2B_07AAACP9472A1ZJ_19052025 (1).csv", "portal_ims"),
        ("Purchase details.xlsx", "books_purchase"),
        ("Credit Note.xlsx", "books_credit"),
    ]:
        p = REAL_DIR / name
        if not p.exists():
            missing.append(name)
        else:
            files[role] = p
    if missing:
        return None
    return files


def test_build_report_deterministic_real_files():
    files = _real_file_set()
    if files is None:
        pytest.skip("real dump folder incomplete on this machine")
    tmp = Path(tempfile.mkdtemp(prefix="agent_test_"))
    try:
        res = build_report(files, client_id="testagent", period="2025-04",
                           llm=False, out_root=tmp)
        assert res["f5"]["pass"] is True
        assert res["verification"]["pass"] is True
        assert Path(res["report_path"]).exists()
        assert res["counts"]["portal"] > 0
        assert res["counts"]["books"] > 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
