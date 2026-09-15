"""Unit tests for the normalisation layer."""
from decimal import Decimal

from recon_engine.normalizer import (
    norm_gstin, norm_invoice_no, norm_date_str, norm_amount, validate_gstin,
)
import pytest
from recon_engine.normalizer import NormalisationError


def test_norm_invoice_no():
    assert norm_invoice_no("  INV-1002 ") == "INV-1002"


def test_norm_gstin():
    assert norm_gstin(" 29ab cde1234f1z5") == "29ABCDE1234F1Z5"


def test_norm_date_formats():
    assert norm_date_str("05-08-2026") == norm_date_str("2026-08-05")
    assert norm_date_str("05/08/2026") == norm_date_str("05-08-2026")


def test_norm_amount():
    assert norm_amount("₹1,234.50") == Decimal("1234.50")
    assert norm_amount("INR 500") == Decimal("500.00")


def test_validate_gstin():
    assert validate_gstin("29ABCDE1234F1Z5") is None
    assert validate_gstin("bogus") is not None


def test_bad_date_raises():
    with pytest.raises(NormalisationError):
        norm_date_str("not-a-date")
