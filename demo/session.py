"""Ephemeral session + folder management for the demo module.

The demo deliberately abuses the existing folder-keyed storage model: the
``client`` is the synthetic constant ``_demo`` and the ``period`` is the
session id. This works because Units 1-5 never validate that ``client`` /
``period`` correspond to a real firm client — so the unmodified engine can
read a demo upload exactly as it would read a real client's file.

Nothing here persists beyond one browser session. Rows that ``execute_run``
writes into ``db/poc.db`` under client ``_demo`` are harmless clutter, not a
concern for this module.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.data_paths import DATA_ROOT, source_data_path

# The synthetic client every demo run is filed under. Deliberately not a
# real firm client — see the module docstring.
DEMO_CLIENT = "_demo"

# Where the demo's own scratch files live (staged uploads, AI cache).
DEMO_ROOT = DATA_ROOT / DEMO_CLIENT


def new_session_id() -> str:
    """A fresh, short, human-readable session id."""
    return f"demo_{uuid.uuid4().hex[:8]}"


def session_dir(session_id: str) -> Path:
    """The session's scratch directory. Not created here — call
    :func:`ensure_session_dir` when you actually need it on disk."""
    return DEMO_ROOT / session_id


def ensure_session_dir(session_id: str) -> Path:
    path = session_dir(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ai_cache_path(session_id: str) -> Path:
    """Where the AI insight narrative is cached for this session."""
    return ensure_session_dir(session_id) / "ai_cache.json"


def stage_file(session_id: str, source_type: str, filename: str, data: bytes) -> Path:
    """Write one uploaded file into the session's source folder.

    The file lands at ``data/_demo/<session_id>/<source_type>/<filename>`` —
    the exact layout ``load_source_file`` / the canonical ingestion layer
    expect — so the engine reads it with no special-casing whatsoever.

    ``source_data_path`` is used (rather than building the path by hand) so
    the folder layout and the source_type validation stay owned by the
    engine's own module.
    """
    directory = source_data_path(DEMO_CLIENT, session_id, source_type)
    path = directory / _safe_filename(filename)
    path.write_bytes(data)
    return path


def _safe_filename(filename: str) -> str:
    """Strip any directory component from an uploaded filename.

    Streamlit normally supplies a bare name, but a crafted name containing
    ``../`` must never be able to escape the session folder.
    """
    name = Path(str(filename)).name.strip()
    return name or "upload.csv"


def clear_session(session_id: str) -> None:
    """Delete the session's staged files. Best-effort — a missing folder is
    not an error, and a failure here must never break the UI."""
    try:
        shutil.rmtree(session_dir(session_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# AI insight cache (file-based, per session)
# ---------------------------------------------------------------------------


def read_ai_cache(session_id: str) -> dict[str, Any]:
    """The session's cached AI narratives, keyed by run_id. Returns {} when
    absent or unreadable — a broken cache must never break the report."""
    path = ai_cache_path(session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_ai_cache(session_id: str, cache: dict[str, Any]) -> None:
    """Persist the AI narrative cache. Best-effort."""
    try:
        ai_cache_path(session_id).write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Session state container
# ---------------------------------------------------------------------------


@dataclass
class DemoSession:
    """Everything one demo session holds.

    Stored in ``st.session_state`` by the app. Deliberately a plain dataclass
    rather than a Streamlit-aware object, so the pipeline can be exercised
    (and tested) without a running Streamlit runtime.
    """

    session_id: str = field(default_factory=new_session_id)
    # Raw uploaded files: [{filename, data, source_type, ...}] — kept so a
    # re-run or a re-classification doesn't need the user to re-upload.
    uploads: list[dict[str, Any]] = field(default_factory=list)
    # classify.classify_files() output, one entry per file.
    classifications: list[dict[str, Any]] = field(default_factory=list)
    # run_id per recon type, e.g. {"GST": 21, "TDS": 22}.
    run_ids: dict[str, int] = field(default_factory=dict)
    # AI narrative per recon type, e.g. {"GST": {...}}.
    insights: dict[str, Any] = field(default_factory=dict)
    # The generated report HTML, once built.
    report_html: Optional[str] = None
    # Chat transcript: [{role, text, kind}].
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Set when the pipeline is paused awaiting a clarifying answer.
    pending_question: Optional[dict[str, Any]] = None
    # True once the user has asked for the run to proceed.
    started: bool = False

    def reset(self) -> None:
        """Start over: clear staged files and every field, keeping nothing."""
        clear_session(self.session_id)
        self.session_id = new_session_id()
        self.uploads = []
        self.classifications = []
        self.run_ids = {}
        self.insights = {}
        self.report_html = None
        self.messages = []
        self.pending_question = None
        self.started = False


# ---------------------------------------------------------------------------
# Sample datasets — so the demo can be shown without hunting for files
# ---------------------------------------------------------------------------

# Each entry: label -> {description, files: [(source_type, path relative to
# the repo root)]}. These point at the sample exports already in data/, so
# the demo needs no bundled fixtures of its own.
#
# The descriptions are deliberately honest about what each set shows. The
# sample exports in this repo are a deliberately messy test fixture — they
# exist to exercise every difference type the engine can produce, so they
# carry a low match rate by design. Saying so up front is better than letting
# a prospect conclude the engine is broken.
SAMPLE_DATASETS: dict[str, dict[str, Any]] = {
    "GST only — books + GSTR-2B": {
        "description": "A purchase register and a GSTR-2B. Runs GST reconciliation "
                       "and states plainly that TDS was not run. This sample is a "
                       "deliberately messy month, so expect a low match rate — it "
                       "shows the engine catching every kind of difference.",
        "files": [
            ("tally", "data/AcmeTextiles/2025-06/tally/gst_purchase_register_v2_final_corrected.csv"),
            ("gstr2b", "data/AcmeTextiles/2025-06/gstr2b/gstr2b_v3_downloaded_20jul_final.csv"),
        ],
    },
    "GST + TDS + IMS — the full picture": {
        "description": "Books, GSTR-2B, Form 26AS and an IMS export. Runs both "
                       "reconciliations and carries IMS into the report as reference.",
        "files": [
            # This books export carries BOTH the GST and the TDS columns, which
            # is what lets one file feed both reconciliations. A TDS-only books
            # export cannot be used here — see the note on _dedupe_key in the
            # engine's normalizer.
            ("tally", "data/testcorp/2026-08/tally/tally_export.csv"),
            ("gstr2b", "data/testcorp/2026-08/gstr2b/gstr2b_export.csv"),
            ("form26as", "data/acme/2026-08/form26as/form26as_export.csv"),
            ("ims", "data/AcmeTextiles/2025-06/ims/ims_v3_final_action_19jul.csv"),
        ],
    },
    "Two GSTR-2B downloads — which one?": {
        "description": "Two files that both look like a GSTR-2B, so the demo asks "
                       "which is the right one instead of silently picking.",
        "files": [
            ("tally", "data/AcmeTextiles/2025-06/tally/gst_purchase_register_v2_final_corrected.csv"),
            ("gstr2b", "data/AcmeTextiles/2025-06/gstr2b/gstr2b_v1_downloaded_05jul.csv"),
            ("gstr2b", "data/AcmeTextiles/2025-06/gstr2b/gstr2b_v3_downloaded_20jul_final.csv"),
        ],
    },
}


def load_sample_dataset(name: str, repo_root: Path) -> list[dict[str, Any]]:
    """Read a sample dataset's files into memory as upload dicts.

    Returns the same shape the Streamlit uploader produces, so the pipeline
    treats a sample exactly like a real upload.
    """
    spec = SAMPLE_DATASETS.get(name)
    if not spec:
        return []
    uploads: list[dict[str, Any]] = []
    for source_type, rel_path in spec["files"]:
        path = repo_root / rel_path
        if not path.exists():
            continue
        uploads.append({
            "filename": path.name,
            "data": path.read_bytes(),
            "source_type": source_type,
            "origin": "sample",
        })
    return uploads
