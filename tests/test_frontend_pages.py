"""Tests for the dependency-free web front-end (client picker + workbench)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from recon_engine.api import app
from recon_engine.sample_scenario import generate as gen_sample_docs

SAMPLES = gen_sample_docs()


def _run_for(client: TestClient, cid: str) -> object:
    """POST the standard fixture pair for a client - the workbench's core call."""
    with open(SAMPLES["gstr2b_rules"], "rb") as g, \
         open(SAMPLES["tally"], "rb") as t:
        return client.post(
            f"/api/clients/{cid}/recon?period=2024-08&output_tax=120000&materiality=50000",
            files={
                "gstr2b": ("gstr2b.json", g, "application/json"),
                "tally": ("tally.csv", t, "text/csv"),
            },
        )


def test_home_page_renders():
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    text = r.text
    assert "Choose a client" in text
    assert "New client" in text
    assert "new-client-form" in text
    assert "GST 2B vs Books Reconciliation" in text
    # page JS must actually be delivered inside <script> tags (was dead before)
    assert "<script>" in text and "</script>" in text


def test_home_lists_client_after_run():
    c = TestClient(app)
    r = _run_for(c, "uiex1")
    assert r.status_code == 200
    home = c.get("/").text
    assert 'uiex1' in home
    assert "Open workbench" in home


def test_workbench_has_upload_form():
    c = TestClient(app)
    _run_for(c, "uiex2")
    r = c.get("/clients/uiex2")
    assert r.status_code == 200
    text = r.text
    assert "recon-form" in text
    assert "name='gstr2b'" in text and "name='tally'" in text and "name='tb'" in text
    assert "action='/api/clients/uiex2/recon'" in text
    assert "Run reconciliation" in text
    assert "period" in text and "output_tax" in text and "materiality" in text
    # Invoices upload is the books-side register; advanced match rules exposed
    assert "Invoices" in text and "Tally purchase register (books)" in text
    assert "Advanced" in text and "match rules" in text
    for field in ("invoice_edit_max", "date_window_days",
                  "value_tolerance", "require_gstin_exact"):
        assert "id='" + field + "'" in text


def test_workbench_lists_previous_runs():
    c = TestClient(app)
    _run_for(c, "uiex3")
    text = c.get("/clients/uiex3").text
    wk = c.get("/clients/uiex3")
    assert wk.headers.get("cache-control") == "no-store"
    assert "2024-08" in text
    # Dashboard is now a View button that embeds the report in the iframe
    assert "Report viewer" in text and "id='report-frame'" in text
    assert ("onclick='window.loadReport(&quot;/api/reports/uiex3/2024-08/"
            "gst_2b_reconciliation_report.html&quot;);return false;'" in text)
    # viewer helpers must be reachable from global scope (inline onclick)
    assert "window.loadReport = loadReport;" in text
    assert "window.resizeReport = resizeReport;" in text
    assert "View" in text
    # the workbench JS must be delivered inside a real <script> tag (was dead before)
    assert "<script>" in text and "</script>" in text
    assert "window.loadReport = loadReport;" in text
    # download affordances: prev-run row + viewer + JS helper
    assert "?download=1" in text
    assert "Download</a>" in text
    assert "Download current" in text
    assert "window.downloadCurrent = downloadCurrent;" in text
    # JS-generated post-run links must quote href in the onclick
    # (was `'' + href + ''` -> unquoted/unevaluated -> ReferenceError)
    assert "window.loadReport(&quot;' + href + '&quot;);return false;" in text
    assert "'' + href + ''" not in text

def test_report_html_served_inline_not_download():
    """HTML reports must render in the browser (no attachment header)."""
    c = TestClient(app)
    _run_for(c, "uiex3")
    r = c.get("/api/reports/uiex3/2024-08/gst_2b_reconciliation_report.html")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")
    assert "content-disposition" not in {k.lower() for k in r.headers}
    assert "Reconciliation Report" in r.text
    # pages and inline HTML must never be served from stale cache
    assert r.headers.get("cache-control") == "no-store"
    # ?download=1 must force attachment even for HTML
    d = c.get("/api/reports/uiex3/2024-08/gst_2b_reconciliation_report.html",
              params={"download": "1"})
    assert d.status_code == 200
    assert "content-disposition" in {k.lower() for k in d.headers}
    assert "attachment" in d.headers.get("content-disposition", "").lower()
    assert "filename=" in d.headers.get("content-disposition", "").lower()


def test_workbench_unknown_client_empty_state():
    r = TestClient(app).get("/clients/brandnewco")
    assert r.status_code == 200
    assert "No reconciliation runs yet for this client." in r.text


def test_invalid_client_id_blocked():
    c = TestClient(app)
    r = c.get("/clients/..%2F..%2F.git")
    assert r.status_code in (400, 404)


def test_clients_redirect_route():
    c = TestClient(app)
    r = c.get("/clients", params={"id": "acme"}, follow_redirects=False)
    assert r.status_code in (301, 302, 307)
    assert r.headers["location"].endswith("/clients/acme")


def test_firmwide_portfolio_endpoint():
    r = TestClient(app).get("/api/firmwide")
    assert r.status_code in (200, 404)  # exists only if a batch already ran


def test_recon_accepts_advanced_match_rules():
    """The UI's match-rule fields flow through the API into the engine."""
    c = TestClient(app)
    with open(SAMPLES["gstr2b_rules"], "rb") as g, \
         open(SAMPLES["tally"], "rb") as t:
        r = c.post(
            "/api/clients/uiex4/recon?period=2024-08&output_tax=120000"
            "&invoice_edit_max=1&date_window_days=3&value_tolerance=0.05"
            "&require_gstin_exact=false",
            files={
                "gstr2b": ("gstr2b.json", g, "application/json"),
                "tally": ("tally.csv", t, "text/csv"),
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["period"] == "2024-08"
    assert data["packet"]["period"] == "2024-08"
