"""Load a run's Review model — the ONE path every surface shares.

The Review screen and all three export formats (§3) must agree exactly on
every headline number, so none of them is allowed to assemble the model
itself. This module resolves the C1 thresholds and the 2A eligible-credit
figure for a run and hands back ``review.build_review_model``'s output.

Pure reads: no writes, no matching, no classification changes.
"""

from __future__ import annotations

from typing import Any, Optional

from src import queries
from src.reconciliation import review


def resolve_client_id(folder: str, *, db_path=None) -> Optional[int]:
    """Map a run's client FOLDER to its client_id, tolerantly (the folder name
    and the registered legal name differ in punctuation and spacing)."""
    def norm(name: str) -> str:
        return "".join(ch for ch in str(name).lower() if ch.isalnum())

    try:
        from src.clients import service as clients

        candidates = clients.list_clients(include_inactive=True)
    except Exception:  # noqa: BLE001
        return None
    target = norm(folder)
    if not target:
        return None
    for c in candidates:
        if norm(c["legal_name"]) == target:
            return c["client_id"]
    for c in candidates:
        n = norm(c["legal_name"])
        if target and (n.startswith(target) or target.startswith(n)):
            return c["client_id"]
    return None


def client_gstin_for(client_id: Optional[int], *, db_path=None) -> str:
    """The SELECTED CLIENT's own GSTIN (its primary branch), or "".

    This is client master data — deliberately NOT derived from any run's
    transaction rows, where the most frequent GSTIN is a supplier's (the
    counterparty on the most invoices). The report header must carry the
    client's own registration, never a supplier's.
    """
    if not client_id:
        return ""
    try:
        from src.clients import service as clients

        branches = clients.list_branches(client_id, include_inactive=False, db_path=db_path)
    except Exception:  # noqa: BLE001
        return ""
    primary = next((b for b in branches if b.get("is_primary")), None)
    chosen = primary or (branches[0] if branches else None)
    return str((chosen or {}).get("gstin") or "").strip()


def _effective_number(key: str, default: float, *, client_id: Optional[int]) -> float:
    try:
        from src.rules import service as rules

        v = rules.get_effective_number(key, client_id=client_id, default=default)
        return float(v) if v is not None else default
    except Exception:  # noqa: BLE001
        return default


def itc_figures_for_run(run: dict[str, Any], *, client_id: Optional[int]) -> dict[str, float]:
    """The 2A eligible-credit figures for a run's period.

    ``blocked_credit_itc`` (§17(5)) and ``reverse_charge_itc`` are the run's
    ACTUAL eligibility markers — the ITC split chart is built from these, not
    from (claimed − eligible), which measures something else entirely.
    """
    empty = {
        "eligible": 0.0, "claimed": 0.0, "blocked": 0.0,
        "reverse_charge": 0.0, "at_risk": 0.0,
    }
    if (run.get("recon_type") or "GST") != "GST" or not client_id:
        return {**empty, "available": False}
    try:
        from src.module2 import service as m2

        credit = m2.eligible_credit_for_client(client_id, period=run.get("period"))
    except Exception:  # noqa: BLE001
        return {**empty, "available": False}
    if not credit:
        return {**empty, "available": False}
    return {
        "available": True,
        "eligible": float(credit.get("eligible_credit") or 0.0),
        "claimed": float(credit.get("total_itc_claimed") or 0.0),
        "blocked": float(credit.get("blocked_credit_itc") or 0.0),
        "reverse_charge": float(credit.get("reverse_charge_itc") or 0.0),
        "at_risk": float(credit.get("at_risk_itc") or 0.0),
    }


def load_run_model(
    run_id: int, *, db_path=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(run, model)`` for a run — the same model the Review screen renders.

    Raises ``ValueError`` when the run does not exist.
    """
    run = queries.get_run(run_id, db_path=db_path)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    rows = queries.get_results(run_id, db_path=db_path).to_dict("records")
    folder = run.get("client") or ""
    recon_type = run.get("recon_type") or "GST"
    client_id = resolve_client_id(folder, db_path=db_path)

    materiality_key = {
        "GST": "materiality.gst", "TDS": "materiality.tds", "OTHER": "materiality.other",
    }.get(recon_type, "materiality.gst")
    materiality = _effective_number(materiality_key, review.DEFAULT_MATERIALITY, client_id=client_id)
    tolerance = _effective_number(
        "gst.amount_tolerance.absolute", review.DEFAULT_TOLERANCE, client_id=client_id
    )
    rounding = _effective_number(
        "gst.rounding_tolerance", review.DEFAULT_ROUNDING_TOLERANCE, client_id=client_id
    )

    itc = itc_figures_for_run(run, client_id=client_id)

    model = review.build_review_model(
        run, rows,
        materiality=materiality, tolerance=tolerance, rounding_tolerance=rounding,
        itc_eligible=itc["eligible"] if itc["available"] else None,
        itc_claimed=itc["claimed"] if itc["available"] else None,
        itc_blocked=itc["blocked"] if itc["available"] else None,
        itc_reverse_charge=itc["reverse_charge"] if itc["available"] else None,
    )
    model["client_id"] = client_id
    model["materiality"] = materiality
    # §2 — the client's OWN GSTIN, from the client record (never a transaction
    # row), so the report header can bind to it unambiguously.
    model["client_gstin"] = client_gstin_for(client_id, db_path=db_path)
    return run, model
