"""Deterministic Tier-A normalisation + structural validation (F5).

Everything here is rule-based code -- no LLM. This is the layer that resolves
~30% of false mismatches caused by formatting differences.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import Invoice


class NormalisationError(ValueError):
    """Raised when a record fails F5 structural validation."""


# ---------------------------------------------------------------------------
# Field normalisers
# ---------------------------------------------------------------------------
def norm_gstin(gstin: str) -> str:
    """Trim + uppercase a GSTIN. Returns '' for blank input."""
    if not gstin:
        return ""
    return re.sub(r"\s+", "", gstin).upper()


def norm_invoice_no(invoice_no: str) -> str:
    """Strip whitespace/control chars and normalise case for matching."""
    if not invoice_no:
        return ""
    return re.sub(r"\s+", "", str(invoice_no)).upper()


def norm_date_str(raw: str, fmt: str | None = None) -> date:
    """Parse common Indian/ISO date formats deterministically."""
    raw = str(raw).strip()
    for candidate in ([fmt] if fmt else ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
                                          "%m/%d/%Y", "%d-%b-%Y", "%d/%b/%Y",
                                          "%Y%m%d"]):
        if not candidate:
            continue
        try:
            return datetime.strptime(raw, candidate).date()
        except ValueError:
            continue
    # Last resort: ISO from date/datetime objects
    if isinstance(raw, date):
        return raw
    raise NormalisationError(f"Cannot parse date: {raw!r}")


def _as_bool(v) -> bool:
    """Coerce RCM / §17(5) flag cells to bool, tolerating portal vocabulary.

    Truthy: y/yes/true/1/blocked/notavail. Falsy: n/no/false/0/avail/blank.
    """
    if isinstance(v, bool):
        return v
    s = re.sub(r"[^a-z0-9]", "", str(v).strip().lower())
    return s in ("y", "yes", "true", "1", "blocked", "notavail", "notavailable")


def norm_amount(raw) -> Decimal:
    """Parse an amount string/number to Decimal. Handles commas, currency."""
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    s = str(raw).strip().replace(",", "").replace("₹", "").replace("INR ", "")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise NormalisationError(f"Cannot parse amount: {raw!r}")


# ---------------------------------------------------------------------------
# F5 structural validation
# ---------------------------------------------------------------------------
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")


def validate_gstin(gstin: str) -> str | None:
    """Return None if valid (after normalisation), else an error message."""
    g = norm_gstin(gstin)
    if not g:
        return "GSTIN is blank"
    if len(g) != 15:
        return f"GSTIN has wrong length ({len(g)} != 15): {g}"
    if not _GSTIN_RE.match(g):
        return f"GSTIN fails structural pattern: {g}"
    return None


def validate_invoice(inv: Invoice) -> list[str]:
    """Run F5 structural checks on a normalised invoice. Returns error list."""
    errors: list[str] = []
    if not inv.gstin:
        errors.append("missing GSTIN")
    if not inv.invoice_no:
        errors.append("missing invoice number")
    if inv.date.year < 1900 or inv.date.year > 2100:
        errors.append(f"impossible date {inv.date}")
    if inv.taxable_value < 0:
        errors.append("negative taxable value")
    if inv.tax_amount != inv.cgst + inv.sgst + inv.igst:
        errors.append(
            f"tax breakdown mismatch: total={inv.tax_amount} "
            f"cgst+sgst+igst={inv.cgst + inv.sgst + inv.igst}"
        )
    return errors


# ---------------------------------------------------------------------------
# Record constructors (ingest helpers)
# ---------------------------------------------------------------------------
def make_invoice(
    gstin: str,
    invoice_no: str,
    date: str | date,
    taxable_value,
    cgst=0,
    sgst=0,
    igst=0,
    source: str = "",
    status: str = "",
    reverse_charge=False,
    blocked_credit=False,
    reference: str = "",
) -> Invoice:
    """Build an Invoice, applying normalisation to every field."""
    return Invoice(
        gstin=norm_gstin(gstin),
        invoice_no=norm_invoice_no(invoice_no),
        date=norm_date_str(date) if isinstance(date, str) else date,
        taxable_value=norm_amount(taxable_value),
        cgst=norm_amount(cgst),
        sgst=norm_amount(sgst),
        igst=norm_amount(igst),
        tax_amount=norm_amount(cgst) + norm_amount(sgst) + norm_amount(igst),
        source=source,
        status=status,
        reverse_charge=_as_bool(reverse_charge),
        blocked_credit=_as_bool(blocked_credit),
        reference=reference,
    )
