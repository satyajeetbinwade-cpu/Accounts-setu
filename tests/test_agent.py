"""Tests for the automation agent entry point."""
import copy, json, os, tempfile

import pytest

from recon_engine.agent import auto_reconcile

G2B = "sample_data/gstr2b_sample_full.json"
TALLY = "sample_data/tally_purchase_sample_full.csv"


def test_agent_clean_run(tmp_path):
    res = auto_reconcile(gstr2b_path=G2B, tally_path=TALLY,
                         tb_path="sample_data/SampleTB-AI.xlsx",
                         output_tax="120000", period="2024-08")
    assert res["packet"].validation["pass"] is True
    assert res["verification"]["pass"] is True
    assert res["packet"].itc.eligible_itc == 64260
    for p in res["reports"].values():
        assert p.exists()


def test_agent_blocks_duplicate_invoice(tmp_path):
    data = json.load(open(G2B))
    doc = copy.deepcopy(data["docdata"]["doclist"][0])  # duplicate first row
    data["docdata"]["doclist"].append(doc)
    bad = tmp_path / "bad2b.json"
    bad.write_text(json.dumps(data), encoding="utf-8")

    res = auto_reconcile(gstr2b_path=str(bad), tally_path=TALLY,
                         output_tax="120000", period="2024-08")
    # F5 structural gate must BLOCK the packet
    assert res["packet"].validation["pass"] is False
    gate = res["packet"].validation["checks"][0]
    assert gate["pass"] is False
    assert "duplicate" in str(gate["portal"]["errors"])


def test_agent_missing_files_raise():
    with pytest.raises(FileNotFoundError):
        auto_reconcile(gstr2b_path="nope.json", tally_path=TALLY)
