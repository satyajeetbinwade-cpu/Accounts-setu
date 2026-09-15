"""Phase-4 tests: FastAPI delivery surface + batch runner + M7 upgrades."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon_engine.api import app
from recon_engine.batch import run_batch
from recon_engine.sample_scenario import generate as gen_sample_docs
from recon_engine.sample_scenario import (
    PORTAL_INVOICES_RULES,
    SUPPLIERS,
    write_gstr2b_json,
    write_tally_csv,
)

SAMPLES = gen_sample_docs()
CLIENT_CONFIG = {"match_rules": {"materiality_amount": 50000}}


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# FastAPI: health
# ---------------------------------------------------------------------------
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# FastAPI: upload -> packet -> report URLs -> download
# ---------------------------------------------------------------------------
def test_recon_upload_full_flow(client, tmp_path):
    with open(SAMPLES["gstr2b_rules"], "rb") as g, \
         open(SAMPLES["tally"], "rb") as t:
        r = client.post(
            "/api/clients/acme/recon?period=2024-08&output_tax=120000&materiality=50000",
            files={
                "gstr2b": ("gstr2b_rules.json", g, "application/json"),
                "tally": ("tally.csv", t, "text/csv"),
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["client_id"] == "acme"
    assert body["period"] == "2024-08"
    pkt = body["packet"]
    # rules fixture numbers
    assert pkt["itc"]["eligible_itc"] == "25560.00"
    assert pkt["itc"]["ineligible_itc"] == "64440.00"
    assert pkt["itc"]["gstr3b_draft"]["net_payable"] == "94440.00"
    assert body["verification"]["pass"] is True
    # review summary present
    assert body["review"]["total"] == 11  # 9 portal results + 2 not-in-portal
    # report URLs download fine (round trip)
    html_url = body["reports"]["html"]
    assert html_url.endswith("gst_2b_reconciliation_report.html")
    h = client.get(html_url)
    assert h.status_code == 200
    assert "Reconciliation Report" in h.text
    # per-client pack on disk under reports/acme/2024-08/
    rel = Path("reports") / "acme" / "2024-08"
    assert (rel / "review_queue.json").exists()
    assert (rel / "pre_vs_post_reconciliation.md").exists()
    assert (rel / "gst_2b_reconciliation_report.html").exists()


def test_recon_smoke_excel_uploads(client):
    with open(SAMPLES["gstr2b_rules_xlsx"], "rb") as g, \
         open(SAMPLES["tally_xlsx"], "rb") as t:
        r = client.post(
            "/api/clients/xl/recon?output_tax=120000",
            files={
                "gstr2b": ("gstr2b_rules.xlsx", g,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "tally": ("tally.xlsx", t,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["packet"]["itc"]["eligible_itc"] == "25560.00"
    assert body["verification"]["pass"] is True


def test_recon_rejects_bad_extension(client):
    with open(SAMPLES["gstr2b_rules"], "rb") as g, \
         open(SAMPLES["tally"], "rb") as t:
        r = client.post(
            "/api/clients/bad/recon",
            files={
                "gstr2b": ("gstr2b.txt", g, "text/plain"),
                "tally": ("tally.csv", t, "text/csv"),
            },
        )
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"]


@pytest.mark.parametrize("name",
    ["pre_vs_post_reconciliation.md", "Pre_Post_Reconciliation_Report.xlsx",
     "pre_recon_open_items.csv", "post_recon_exceptions.csv",
     "review_queue.json"])
def test_get_report_files(client, name):
    url = f"/api/reports/acme/2024-08/{name}"
    r = client.get(url)
    assert r.status_code == 200
    if name.endswith(".json"):
        data = r.json()
        assert "items" in data


def test_get_report_missing_404(client):
    assert client.get("/api/reports/nope/nope/nope.html").status_code == 404


def test_get_report_path_traversal_blocked(client):
    r = client.get("/api/reports/acme/2024-08/..%2F..%2F..%2FREADME.md")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Batch runner: 2-client fixture -> aggregate + portfolio
# ---------------------------------------------------------------------------
@pytest.fixture()
def multi_client_inbox(tmp_path):
    """Two client folders with complete inputs + one broken (missing tally)."""
    inbox = tmp_path / "inbox"
    for cid in ("alpha", "beta"):
        folder = inbox / cid
        folder.mkdir(parents=True)
        write_gstr2b_json(folder / "gstr2b.json", PORTAL_INVOICES_RULES)
        write_tally_csv(folder / "tally.csv")
        (folder / "config.json").write_text(
            json.dumps(CLIENT_CONFIG), encoding="utf-8")
    # gamma: only gstr2b, no tally -> batch records failure, continues
    (inbox / "gamma").mkdir(parents=True)
    write_gstr2b_json(inbox / "gamma" / "gstr2b.json", PORTAL_INVOICES_RULES)
    return inbox


def test_batch_runs_all_clients_and_marks_missing(multi_client_inbox):
    agg = run_batch(multi_client_inbox, output_tax=120000)
    assert agg["clients"] == 3
    by_id = {r["client_id"]: r for r in agg["results"]}
    assert by_id["alpha"]["ok"] is True
    assert by_id["beta"]["ok"] is True
    assert by_id["gamma"]["ok"] is False
    assert "missing inputs" in by_id["gamma"]["error"]
    # alpha/beta passed both gates
    assert by_id["alpha"]["verification"] is True
    assert by_id["alpha"]["validation"] is True
    assert by_id["alpha"]["eligible_itc"] == "25560.00"
    # per-client packs written
    assert Path(by_id["alpha"]["reports_dir"]).exists()


def test_batch_aggregate_contains_portfolio(multi_client_inbox):
    agg = run_batch(multi_client_inbox, output_tax=120000)
    assert len(agg["portfolio"]) == 3
    passed = [p for p in agg["portfolio"]
              if p["verification"] and p["validation"]]
    assert len(passed) == 2
    assert agg["passed"] == 2 and agg["failed"] == 1
    assert all(p["client_id"] in ("alpha", "beta", "gamma")
               for p in agg["portfolio"])


def test_batch_main_exit_codes(multi_client_inbox, capsys):
    from recon_engine.batch import main
    # all-pass subset: alpha + beta only -> return 0
    good_inbox = multi_client_inbox.parent / "good"
    shutil.copytree(multi_client_inbox / "alpha",
                    good_inbox / "alpha")
    shutil.copytree(multi_client_inbox / "beta",
                    good_inbox / "beta")
    rc = main(["--inbox", str(good_inbox), "--output-tax", "120000"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FIRMWIDE" in out and "passed=2" in out
    # with gamma broken -> return 1
    rc = main(["--inbox", str(multi_client_inbox),
               "--output-tax", "120000"])
    assert rc == 1


def test_batch_missing_inbox_returns_2(tmp_path):
    from recon_engine.batch import main
    assert main(["--inbox", str(tmp_path / "no_such"),
                 "--output-tax", "120000"]) == 2


# ---------------------------------------------------------------------------
# M7: HTML report carries freshness + materiality/IMS columns
# ---------------------------------------------------------------------------
def test_html_report_has_freshness_and_columns(client):
    with open(SAMPLES["gstr2b_rules"], "rb") as g, \
         open(SAMPLES["tally"], "rb") as t:
        client.post(
            "/api/clients/m7/recon?period=2024-08&output_tax=120000&materiality=50000",
            files={
                "gstr2b": ("gstr2b.json", g, "application/json"),
                "tally": ("tally.csv", t, "text/csv"),
            },
        )
    html = (Path("reports") / "m7" / "2024-08"
            / "gst_2b_reconciliation_report.html").read_text(encoding="utf-8")
    # freshness chips for both sources
    assert "src-chip" in html
    assert "portal (GSTR-2B)" in html and "books (Tally)" in html
    # evidence links + materiality + IMS columns
    assert "evidence-link" in html
    assert "Materiality" in html and "IMS" in html
    assert "above" in html and "below" in html
    assert "Reject" in html and "Pending" in html and "Accept" in html
    # fixed canonical numbers still surfaced
    assert "25,560.00" in html
    # ---------- interactive surface (Phase-4 upgrade) ----------
    # tab bar with all six sections, role=tab
    assert "id='tab-exceptions'" in html
    assert "id='tab-review'" in html
    assert "role='tablist'" in html
    assert "role='tab' data-tab='overview'" in html
    # embedded payload carrying exceptions/jvs/review
    assert "window.RECON_DATA" in html
    assert '"review"' in html  # phase-3 queue attached
    # evidence modal + click-to-expand affordances
    assert "id='overlay'" in html and "id='modal-title'" in html
    assert "openModal" in html
    # search / filter / sort affordances
    assert "id='exc-search'" in html
    assert "id='chips'" in html
    assert 'class="sortable"' in html or 'sortable' in html
    # jv expansion + copy-narration
    assert "id='jv-box'" in html
    assert 'copy-narr' in html
    # kpi cards clickable -> filtered list
    assert "data-status='" in html and "role='button'" in html


# ---------------------------------------------------------------------------
# Interactive surface: the embedded RECON_DATA contract the JS consumes
# ---------------------------------------------------------------------------
def _extract_recon_data(html: str) -> dict:
    import re
    m = re.search(r"window\.RECON_DATA = (\{.*?\});\s*</script>", html, re.S)
    assert m, "RECON_DATA script payload missing"
    return json.loads(m.group(1))


def test_recon_data_payload_contract(client):
    with open(SAMPLES["gstr2b_rules"], "rb") as g, \
         open(SAMPLES["tally"], "rb") as t:
        client.post(
            "/api/clients/cdata/recon?period=2024-08&output_tax=120000&materiality=50000",
            files={
                "gstr2b": ("gstr2b.json", g, "application/json"),
                "tally": ("tally.csv", t, "text/csv"),
            },
        )
    html = (Path("reports") / "cdata" / "2024-08"
            / "gst_2b_reconciliation_report.html").read_text(encoding="utf-8")
    data = _extract_recon_data(html)

    # exceptions carry the full evidence contract for the modal
    # (9 portal-side results + 2 not-in-portal books-side items)
    assert len(data["exceptions"]) == 11
    first = data["exceptions"][0]
    for k in ("idx", "status", "confidence", "portal_invoice_no", "book_invoice_no",
              "gstin", "portal_value", "book_value", "edit_distance", "date_delta",
              "value_delta_pct", "match_score", "materiality", "ims", "reasons",
              "portal", "book", "evidence"):
        assert k in first, f"exception missing key {k}"
    # invoice evidence dicts carry the full tax breakdown
    assert first["portal"]["taxable_value"] is not None
    assert first["portal"]["cgst"] is not None
    assert first["portal"]["sgst"] is not None

    # jvs drain into cards with lines + balance
    assert len(data["jvs"]) >= 1
    jv = data["jvs"][0]
    assert jv["balanced"] is True
    assert jv["total_dr"] == jv["total_cr"]
    assert len(jv["lines"]) >= 2

    # review queue attached: batch-approvable items flagged for the UI
    assert data["review"] is not None
    rv_items = data["review"]["items"]
    assert any(it["batch_approvable"] for it in rv_items)
    batch_flags = [it for it in rv_items if it["batch_approvable"]]
    assert len(batch_flags) == 3  # below-materiality HIGH matched items

    # sources freshness + verification checks present
    assert "portal (GSTR-2B)" in data["sources"]
    assert data["verification"]  # non-empty checks list


def test_recon_data_no_review_when_absent(tmp_path):
    """write_html without a review object -> review tab shows 'not attached'."""
    from recon_engine.agent import _load_portal, _load_books
    from recon_engine.engine import ReconciliationEngine
    from recon_engine.prepost_report import build_post_state, build_pre_state
    from recon_engine.htmlreport import write_html

    portal = _load_portal(SAMPLES["gstr2b_rules"])
    books = _load_books(SAMPLES["tally"])
    packet = ReconciliationEngine().run_sync(
        client_id="x", period="2024-08", portal_invoices=portal,
        book_invoices=books, output_tax="120000")
    pre = build_pre_state(portal, books, [])
    post = build_post_state(packet)
    out = write_html(pre, post, packet, out_path=tmp_path / "out" / "r.html")
    data = _extract_recon_data(out.read_text())
    assert data["review"] is None
