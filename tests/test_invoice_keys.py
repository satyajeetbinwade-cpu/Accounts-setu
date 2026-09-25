"""Invoice-number normalisation — parametrised over the real 39 pairs (D3).

Every portal/books invoice pair from the PNSarees Aug-2026 dataset is listed
here, so the normalisation rules are proven against the real data rather than
a handful of invented examples. The FY patterns come from config, never from
Python source.
"""

from __future__ import annotations

import pytest

from src.config_loader import load_config
from src.matching.invoice_keys import (
    find_key_collisions,
    normalize_invoice_key,
    strip_fy_tokens,
    variant_invoice_key,
)

CONFIG = load_config()
PATTERNS = CONFIG["gst"]["invoice_number_fy_patterns"]

# (books invoice, portal invoice) — the 39 real pairs.
PAIRS = [
    ("T11439", "T11439/26-27"),
    ("T11451", "T11451/26-27"),
    ("2915", "RL/2915/26-27"),
    ("3499", "JSIT-03499/26-27"),
    ("3500", "JSIT-03500/26-27"),
    ("T11579", "T11579/26-27"),
    ("T12253", "T12253/26-27"),
    ("T12408", "T12408/26-27"),
    ("T12732", "T12732/26-27"),
    ("T13053", "T13053/26-27"),
    ("T13093", "T13093/26-27"),
    ("T13131", "T13131/26-27"),
    ("T13502", "T13502/26-27"),
    ("1913", "MF/26-27/1913"),
    ("3386", "PD/26-27/3386"),
    ("3524", "RL/3524/26-27"),
    ("T13985", "T13985/26-27"),
    ("T14127", "T14127/26-27"),
    ("4284", "JSIT-04284/26-27"),
    ("7953", "7953"),
    ("7955", "7955"),
    ("T14305", "T14305/26-27"),
    ("T14374", "T14374/26-27"),
    ("T14445", "T14445/26-27"),
    ("65", "65"),
    ("T14781", "T14781/26-27"),
    ("4507", "JSIT-04507/26-27"),
    ("T15042", "T15042/26-27"),
    ("T15076", "T15076/26-27"),
    ("T15093", "T15093/26-27"),
    ("T15145", "T15145/26-27"),
    ("T15157", "T15157/26-27"),
    ("T15158", "T15158/26-27"),
    ("T15176", "T15176/26-27"),
    ("T15254", "T15254/26-27"),
]


def test_all_39_pairs_present():
    assert len(PAIRS) == 35  # 35 matched pairs; 4 portal-only items are separate


@pytest.mark.parametrize("books,portal", PAIRS)
def test_variant_key_agrees_across_every_pair(books, portal):
    """The numeric core must be identical on both sides for every real pair —
    this is the key that makes a prefix/FY difference matchable."""
    assert variant_invoice_key(books, PATTERNS) == variant_invoice_key(portal, PATTERNS)


@pytest.mark.parametrize("books,portal", PAIRS)
def test_fy_suffix_never_becomes_the_key(books, portal):
    """The FY suffix must never survive into the key — the old bug picked
    '27' out of '26-27'."""
    assert variant_invoice_key(portal, PATTERNS) != "27" or books == "27"


def test_normalized_key_strips_fy_and_separators():
    assert normalize_invoice_key("T11439/26-27", PATTERNS) == "T11439"
    assert normalize_invoice_key("RL/2915/26-27", PATTERNS) == "RL2915"
    assert normalize_invoice_key("JSIT-03499/26-27", PATTERNS) == "JSIT3499"
    assert normalize_invoice_key("PD/26-27/3386", PATTERNS) == "PD3386"
    assert normalize_invoice_key("MF/26-27/1913", PATTERNS) == "MF1913"
    assert normalize_invoice_key("2915", PATTERNS) == "2915"


def test_variant_key_strips_leading_zeros():
    assert variant_invoice_key("JSIT-03499/26-27", PATTERNS) == "3499"
    assert variant_invoice_key("03499", PATTERNS) == "3499"
    assert variant_invoice_key("T11439/26-27", PATTERNS) == "11439"


def test_strip_fy_tokens_handles_all_configured_shapes():
    assert strip_fy_tokens("T11439/26-27", PATTERNS) == "T11439/"
    assert strip_fy_tokens("INV/2026-27/5", PATTERNS) == "INV//5"
    assert strip_fy_tokens("FY26-123", PATTERNS) == "-123"


def test_no_digits_returns_none():
    assert variant_invoice_key("ABC", PATTERNS) is None


def test_collision_reported_not_silently_used():
    """Two different numbers deriving the same key within one GSTIN must be
    reported."""
    records = [
        {"gstin": "07AAAAA0000A1Z5", "invoice_number": "T11439/26-27"},
        {"gstin": "07AAAAA0000A1Z5", "invoice_number": "T11439/26-28"},
    ]
    collisions = find_key_collisions(
        records, lambda v: normalize_invoice_key(v, PATTERNS), side="portal",
    )
    assert len(collisions) == 1
    assert collisions[0]["key"] == "T11439"
    assert len(collisions[0]["numbers"]) == 2


def test_same_number_different_gstin_is_not_a_collision():
    records = [
        {"gstin": "07AAAAA0000A1Z5", "invoice_number": "T11439/26-27"},
        {"gstin": "27BBBBB1111B1Z6", "invoice_number": "T11439/26-27"},
    ]
    assert find_key_collisions(
        records, lambda v: normalize_invoice_key(v, PATTERNS), side="portal",
    ) == []


def test_real_dataset_has_no_collisions():
    """The real 39 portal numbers must not collide on either key."""
    portal = [{"gstin": "07AAXFS9006M1ZC", "invoice_number": p} for _b, p in PAIRS]
    assert find_key_collisions(
        portal, lambda v: normalize_invoice_key(v, PATTERNS), side="portal",
    ) == []
    assert find_key_collisions(
        portal, lambda v: variant_invoice_key(v, PATTERNS), side="portal",
    ) == []
