"""Setu demo — Streamlit chat entrypoint.

A standalone, client-agnostic demo of the reconciliation engine. Upload any
mix of GST/TDS source files with no prior setup; the module identifies what
was uploaded, runs it through the real engine, narrates progress like a chat
agent, and produces one polished HTML report.

Run with::

    streamlit run demo/app.py

This file is presentation + orchestration ONLY. Every piece of reconciliation
logic lives in the Phase 1 engine, which is imported and never modified.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# The demo lives in a subfolder, so the repo root must be importable before
# anything under src/ can be reached.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import streamlit as st  # noqa: E402

from src import queries  # noqa: E402
from src.config_loader import load_config  # noqa: E402
from src.runner import RunExecutionError, execute_run  # noqa: E402
from src.ui.design_tokens import COLOR, FONT_FAMILY, LAYOUT  # noqa: E402

from demo import ai_insights, classify, report  # noqa: E402
from demo import session as demo_session  # noqa: E402

st.set_page_config(
    page_title="Setu — Reconciliation Demo",
    page_icon="S",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# Source types that make each reconciliation possible. The books side is
# shared; the portal side differs.
_GST_PORTAL_TYPES = ("gstr2b", "ims")
_TDS_PORTAL_TYPES = ("form26as", "tds")

_LABELS = {
    "tally": "books / purchase register",
    "gstr2b": "GSTR-2B",
    "ims": "IMS export",
    "form26as": "Form 26AS",
    "tds": "TDS return / challan",
}


# ---------------------------------------------------------------------------
# Styling — custom CSS is expected here (this is the one client-facing screen)
# ---------------------------------------------------------------------------


def _inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{ background: {COLOR['page_background']}; }}
        .block-container {{ max-width: 780px; padding-top: 2.4rem; padding-bottom: 5rem; }}
        html, body, [class*="css"] {{ font-family: '{FONT_FAMILY}', -apple-system, sans-serif; }}

        /* Chat bubbles */
        [data-testid="stChatMessage"] {{
          background: {COLOR['surface']};
          border: 1px solid {COLOR['border']};
          border-radius: {LAYOUT['card_radius']}px;
          box-shadow: {LAYOUT['card_shadow']};
          padding: 14px 18px;
          margin-bottom: 10px;
        }}
        [data-testid="stChatMessage"] p {{ font-size: 13.5px; line-height: 1.6; margin-bottom: .5rem; }}
        [data-testid="stChatMessage"] p:last-child {{ margin-bottom: 0; }}

        /* Header */
        .demo-header {{ margin-bottom: 26px; }}
        .demo-brand {{ display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }}
        .demo-mark {{
          width: 28px; height: 28px; border-radius: 9px; background: {COLOR['accent']};
          display: inline-flex; align-items: center; justify-content: center;
          color: #fff; font-weight: 700; font-size: 14px;
        }}
        .demo-name {{ font-weight: 700; font-size: 15px; letter-spacing: .2px; }}
        .demo-title {{ font-size: 27px; font-weight: 700; letter-spacing: -.4px; margin: 0 0 6px; }}
        .demo-sub {{ color: {COLOR['text_secondary']}; font-size: 13px; }}

        /* Buttons */
        .stButton > button {{
          border-radius: 10px; font-weight: 700; font-size: 13px;
          border: 1px solid {COLOR['border']}; padding: .5rem 1.1rem;
        }}
        .stButton > button[kind="primary"] {{
          background: {COLOR['accent']}; border-color: {COLOR['accent']}; color: #fff;
        }}
        .stDownloadButton > button {{
          border-radius: 10px; font-weight: 700; font-size: 13px;
          background: {COLOR['accent']}; border-color: {COLOR['accent']}; color: #fff;
        }}

        /* Status line inside a chat bubble */
        .demo-status {{ color: {COLOR['text_secondary']}; font-size: 12.5px; }}
        .demo-chip {{
          display: inline-block; padding: 2px 9px; border-radius: 999px;
          font-size: 11px; font-weight: 600; margin-right: 6px;
        }}
        .demo-chip-ok {{ background: {COLOR['resolved']}; color: #fff; }}
        .demo-chip-ai {{ background: rgba(232,163,61,.16); color: {COLOR['ai_suggested_on']}; }}
        .demo-chip-warn {{ background: rgba(226,87,76,.13); color: {COLOR['danger']}; }}
        .demo-chip-neutral {{ background: {COLOR['neutral_bg']}; color: {COLOR['text_secondary']}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def _get_session() -> demo_session.DemoSession:
    if "demo_session" not in st.session_state:
        st.session_state["demo_session"] = demo_session.DemoSession()
    return st.session_state["demo_session"]


def _say(sess: demo_session.DemoSession, text: str, *, kind: str = "text") -> None:
    """Append an assistant message to the transcript."""
    sess.messages.append({"role": "assistant", "text": text, "kind": kind})


def _render_transcript(sess: demo_session.DemoSession) -> None:
    for message in sess.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["text"], unsafe_allow_html=message.get("kind") == "html")


# ---------------------------------------------------------------------------
# Orchestration — which reconciliations are possible
# ---------------------------------------------------------------------------


def _plan_reconciliations(classifications: list[dict[str, Any]]) -> dict[str, dict]:
    """Decide which reconciliations can run, from what was classified.

    Returns ``{recon_type: {"books": entry, "portal": entry}}`` for each
    reconciliation that has BOTH sides present. A reconciliation missing a
    side is simply absent — the caller narrates that plainly rather than
    erroring.
    """
    by_type: dict[str, list[dict]] = {}
    for entry in classifications:
        source_type = entry.get("source_type")
        if source_type:
            by_type.setdefault(source_type, []).append(entry)

    plan: dict[str, dict] = {}
    books_files = by_type.get("tally", [])

    for recon_type, portal_types in (("GST", _GST_PORTAL_TYPES), ("TDS", _TDS_PORTAL_TYPES)):
        portal = None
        for portal_type in portal_types:
            candidates = by_type.get(portal_type) or []
            if not candidates:
                continue
            # When the user answered a clarifying question, honour their
            # choice; otherwise take the first candidate.
            chosen = next(
                (c for c in candidates if c.get("_selected")), candidates[0]
            )
            portal = chosen
            break
        if portal is None or not books_files:
            continue

        # Pick the books file that actually fits THIS recon type's schema.
        # A session can hold both a GST purchase register and a TDS register
        # in the same slot, and each belongs to a different run.
        books = max(
            books_files,
            key=lambda entry: classify.books_coverage(
                _upload_data(entry), entry["filename"], recon_type
            ),
        )
        plan[recon_type] = {"books": books, "portal": portal}

    return plan


def _upload_data(entry: dict) -> bytes:
    """The raw bytes behind a classification entry."""
    return entry.get("_data") or b""


def _missing_side_message(classifications: list[dict[str, Any]], plan: dict) -> str | None:
    """A plain-language note about a reconciliation that could NOT run.

    Names the missing side and what it would have enabled, so the user is
    never left to infer from the report that something was skipped.
    """
    present = {c.get("source_type") for c in classifications if c.get("source_type")}
    notes: list[str] = []

    if "tally" not in present:
        if present & set(_GST_PORTAL_TYPES) or present & set(_TDS_PORTAL_TYPES):
            notes.append(
                "I have the portal side but no books / purchase register, so I "
                "can't reconcile anything against it."
            )
        return " ".join(notes) or None

    if "GST" not in plan:
        if present & set(_GST_PORTAL_TYPES):
            notes.append(
                "The GST reconciliation couldn't run — the GSTR-2B or IMS file "
                "wasn't usable."
            )
        else:
            notes.append(
                "Only your books were identified — I need a GSTR-2B (or an IMS "
                "export) to run GST reconciliation, so it wasn't run."
            )

    if "TDS" not in plan:
        if present & set(_TDS_PORTAL_TYPES):
            notes.append(
                "The TDS reconciliation couldn't run — the Form 26AS or TDS "
                "return wasn't usable."
            )
        else:
            notes.append(
                "I didn't find any TDS files (a Form 26AS or TDS return), so "
                "TDS reconciliation wasn't run."
            )

    return " ".join(notes) or None


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _run_pipeline(sess: demo_session.DemoSession, *, use_llm: bool) -> None:
    """Classify, stage, reconcile, analyse, report — narrating as it goes.

    Every stage appends to the transcript so the user sees the work happen.
    A failure at any stage is narrated in plain language; the demo never
    dead-ends on an error.
    """
    uploads = sess.uploads
    if not uploads:
        return

    # --- Stage 1: classification -------------------------------------------
    _say(sess, f"Got {len(uploads)} file{'s' if len(uploads) != 1 else ''}. "
               f"Let me see what these are…")
    with st.spinner("Identifying your files…"):
        classifications = classify.classify_files(uploads, use_llm=use_llm)

    # Carry the raw bytes alongside each classification so later stages can
    # stage the file and score it without re-reading the uploader.
    by_name = {u["filename"]: u for u in uploads}
    for entry in classifications:
        upload = by_name.get(entry["filename"])
        entry["_data"] = (upload or {}).get("data") or b""

    sess.classifications = classifications

    recognized = [c for c in classifications if c.get("source_type")]
    unrecognized = [c for c in classifications if not c.get("source_type")]

    if recognized:
        found = ", ".join(
            f"{_LABELS.get(c['source_type'], c['source_type'])} ({c['filename']})"
            for c in recognized
        )
        _say(sess, f"Found: {found}.")
    if unrecognized:
        names = ", ".join(c["filename"] for c in unrecognized)
        _say(
            sess,
            f"I couldn't identify {names}. I've left "
            f"{'it' if len(unrecognized) == 1 else 'them'} out rather than guessing — "
            f"{'it is' if len(unrecognized) == 1 else 'they are'} listed in the report.",
        )

    # --- Stage 2: a clarifying question, if one is genuinely needed --------
    if _needs_clarification(classifications):
        sess.pending_question = _build_question(classifications)
        return

    _continue_pipeline(sess, use_llm=use_llm)


def _needs_clarification(classifications: list[dict[str, Any]]) -> bool:
    """True when two files claim the same portal slot and the header row
    cannot settle which is which."""
    for recon_type, portal_types in (("GST", _GST_PORTAL_TYPES), ("TDS", _TDS_PORTAL_TYPES)):
        for portal_type in portal_types:
            same = [c for c in classifications if c.get("source_type") == portal_type]
            if len(same) > 1:
                return True
    return False


def _build_question(classifications: list[dict[str, Any]]) -> dict:
    """The single clarifying question the chat asks, with its options."""
    for portal_type in ("gstr2b", "ims", "form26as", "tds"):
        same = [c for c in classifications if c.get("source_type") == portal_type]
        if len(same) > 1:
            return {
                "kind": "duplicate_portal",
                "source_type": portal_type,
                "options": [c["filename"] for c in same],
                "prompt": (
                    f"Two of your files both look like a "
                    f"{_LABELS.get(portal_type, portal_type)}. Which one should I "
                    f"reconcile against?"
                ),
            }
    return {}


def _continue_pipeline(sess: demo_session.DemoSession, *, use_llm: bool) -> None:
    """Stage the files, run the reconciliations, analyse, and build the report."""
    classifications = sess.classifications
    plan = _plan_reconciliations(classifications)

    # --- Stage 3: stage the files where the engine expects them ------------
    staging_notes: list[str] = []
    with st.spinner("Preparing your files…"):
        for entry in classifications:
            if not entry.get("source_type"):
                continue
            try:
                demo_session.stage_file(
                    sess.session_id, entry["source_type"], entry["filename"], entry["_data"]
                )
            except Exception as exc:  # noqa: BLE001
                staging_notes.append(f"{entry['filename']}: {exc}")

        # Register each file's column mapping against its shape, so the engine
        # reads the columns correctly without spending a model call of its own.
        staging_notes.extend(
            classify.preseed_trusted_shapes(
                sess.session_id, classifications, sess.uploads, client=demo_session.DEMO_CLIENT
            )
        )

    if not plan:
        note = _missing_side_message(classifications, plan)
        _say(sess, note or "I couldn't find a complete pair of files to reconcile.")
        _finish_without_runs(sess)
        return

    # --- Stage 4: run each reconciliation ----------------------------------
    config = load_config()
    runs: dict[str, dict] = {}

    for recon_type, sides in plan.items():
        _say(sess, f"Running {recon_type} reconciliation…")
        selected = {
            "tally": sides["books"]["filename"],
            sides["portal"]["source_type"]: sides["portal"]["filename"],
        }
        try:
            with st.spinner(f"Reconciling {recon_type}…"):
                run_id = execute_run(
                    demo_session.DEMO_CLIENT,
                    sess.session_id,
                    recon_type,
                    None,
                    config,
                    selected_files=selected,
                    actor="demo",
                )
        except RunExecutionError as exc:
            _say(
                sess,
                f"I couldn't complete the {recon_type} reconciliation: {exc}",
            )
            continue
        except Exception as exc:  # noqa: BLE001
            _say(sess, f"The {recon_type} reconciliation failed unexpectedly: {exc}")
            continue

        sess.run_ids[recon_type] = run_id
        summary = queries.get_run_summary(run_id)
        run_row = queries.get_run(run_id) or {}

        total = summary.get("total_results", 0)
        matched = (summary.get("by_classification") or {}).get("Matched", 0)
        needs_look = total - matched
        _say(
            sess,
            f"{matched} of {total} records matched cleanly. "
            + (f"{needs_look} need a look." if needs_look else "Nothing needs your attention."),
        )

        runs[recon_type] = {
            "run_id": run_id,
            "summary": summary,
            "caveats": run_row.get("caveats") or [],
            "warnings": [],
        }

    if not runs:
        _finish_without_runs(sess)
        return

    # State plainly which reconciliation did NOT run, and why. This is an
    # acceptance requirement: a GST-only upload must say TDS wasn't run
    # rather than leaving the user to infer it from the report.
    missing_note = _missing_side_message(classifications, plan)
    if missing_note:
        _say(sess, missing_note)

    # --- Stage 5: AI insights ----------------------------------------------
    _say(sess, "Generating insights…")
    for recon_type, run in runs.items():
        try:
            with st.spinner(f"Reading the {recon_type} results…"):
                insight = ai_insights.generate_insights(
                    run["run_id"], recon_type, sess.session_id
                )
            run["insight"] = insight
            sess.insights[recon_type] = insight
            teaser = ai_insights.teaser(insight)
            if teaser:
                _say(sess, teaser)
        except ai_insights.AIInsightsError as exc:
            # The run itself is unaffected — say so, and carry on to the report.
            reason = str(exc).split("|")[-1]
            _say(
                sess,
                f"I couldn't generate the written analysis for {recon_type} "
                f"({reason}). The reconciliation results themselves are complete "
                f"and are in the report.",
            )
        except Exception as exc:  # noqa: BLE001
            _say(sess, f"The written analysis for {recon_type} was unavailable: {exc}")

    # --- Stage 6: the report ------------------------------------------------
    sess.report_html = report.build_report(sess.session_id, classifications, runs)
    _say(sess, "Report ready.")
    sess.started = True


def _finish_without_runs(sess: demo_session.DemoSession) -> None:
    """Build a report even when nothing could be reconciled.

    A report that honestly says "nothing ran, and here's why" is more useful
    than no report at all — and it keeps the demo from ending in a dead end.
    """
    sess.report_html = report.build_report(sess.session_id, sess.classifications, {})
    _say(sess, "I've put together a report of what I found.")
    sess.started = True


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def _render_header() -> None:
    st.markdown(
        """
        <div class="demo-header">
          <div class="demo-brand">
            <span class="demo-mark">S</span>
            <span class="demo-name">Setu</span>
          </div>
          <div class="demo-title">Reconciliation, without the setup</div>
          <div class="demo-sub">
            Drop in your GST and TDS files. Setu works out what they are,
            reconciles them, and tells you what needs attention.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_uploader(sess: demo_session.DemoSession) -> None:
    """The upload control plus the sample-dataset picker."""
    st.markdown("**Upload your files**")
    uploaded = st.file_uploader(
        "Upload your files",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key=f"uploader_{sess.session_id}",
    )

    with st.expander("…or try a sample dataset"):
        st.caption(
            "Loads a set of sample exports so you can see the whole flow "
            "without hunting for files."
        )
        choice = st.selectbox(
            "Sample dataset",
            list(demo_session.SAMPLE_DATASETS.keys()),
            key=f"sample_{sess.session_id}",
        )
        st.caption(demo_session.SAMPLE_DATASETS[choice]["description"])
        if st.button("Load sample", key=f"load_sample_{sess.session_id}"):
            sess.uploads = demo_session.load_sample_dataset(choice, REPO_ROOT)
            sess.messages = []
            sess.classifications = []
            sess.run_ids = {}
            sess.insights = {}
            sess.report_html = None
            sess.pending_question = None
            sess.started = False
            st.rerun()

    if uploaded:
        sess.uploads = [
            {"filename": f.name, "data": f.getvalue(), "origin": "upload"}
            for f in uploaded
        ]

    use_llm = st.toggle(
        "Use AI to identify unfamiliar files",
        value=True,
        key=f"use_llm_{sess.session_id}",
        help=(
            "On: files whose columns don't match a known layout are identified by "
            "the model. Off: only the built-in header matching is used, which is "
            "faster but leaves unfamiliar files unidentified."
        ),
    )

    ready = bool(sess.uploads)
    if st.button(
        "Run reconciliation",
        type="primary",
        disabled=not ready,
        key=f"run_{sess.session_id}",
    ):
        sess.messages = []
        sess.classifications = []
        sess.run_ids = {}
        sess.insights = {}
        sess.report_html = None
        sess.pending_question = None
        sess.started = False
        _run_pipeline(sess, use_llm=use_llm)
        st.rerun()

    if not ready:
        st.caption("Upload at least one file, or load a sample dataset.")


def _render_pending_question(sess: demo_session.DemoSession) -> None:
    """The one clarifying question the chat asks, rendered inline."""
    question = sess.pending_question
    if not question:
        return
    with st.chat_message("assistant"):
        st.markdown(question["prompt"])
        answer = st.radio(
            "Which file?",
            question["options"],
            key=f"clarify_{sess.session_id}",
            label_visibility="collapsed",
        )
        if st.button("Use this file", key=f"clarify_go_{sess.session_id}"):
            chosen = answer
            # Keep only the chosen file for that portal slot; the others stay
            # classified but are no longer candidates for the run.
            for entry in sess.classifications:
                if entry.get("source_type") == question["source_type"]:
                    entry["_selected"] = entry["filename"] == chosen
            sess.pending_question = None
            _say(sess, f"Using {chosen}.")
            _continue_pipeline(sess, use_llm=True)
            st.rerun()


def _render_report(sess: demo_session.DemoSession) -> None:
    if not sess.report_html:
        return
    st.markdown("---")
    st.markdown("### Your report")
    st.caption("This is the file you'd send to a client — it opens on its own.")
    st.download_button(
        "Download report (HTML)",
        data=sess.report_html,
        file_name=f"setu_reconciliation_{sess.session_id}.html",
        mime="text/html",
        key=f"download_{sess.session_id}",
    )
    # The report is generated by this module from the engine's own output, and
    # every value in it is HTML-escaped — so it is trusted content, not
    # user-supplied HTML.
    st.iframe(sess.report_html, height=760)


def main() -> None:
    _inject_css()
    sess = _get_session()

    _render_header()

    # The transcript, then the live controls.
    _render_transcript(sess)

    if sess.pending_question:
        _render_pending_question(sess)
    elif not sess.started:
        _render_uploader(sess)

    _render_report(sess)

    if sess.started or sess.messages:
        st.markdown("---")
        if st.button("Start over", key=f"reset_{sess.session_id}"):
            sess.reset()
            st.rerun()


main()