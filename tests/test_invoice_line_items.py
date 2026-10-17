"""The post-read rule that drops tax-ledger and round-off rows from a read.

No model, no database: these are the pure shapes from the real live runs (upload
6 of the rerun set), plus the controls that must NOT be dropped.
"""

from __future__ import annotations

from src.invoice_extract import extract as ex
from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import line_items as li
from src.invoice_extract import line_items


# ===========================================================================
# (a) the description half of the rule
# ===========================================================================


def test_a_tax_ledger_row_reads_like_a_ledger_row():
    for description in (
        "IGST Payable Account",
        "Output CGST - 9%",
        "Output SGST-9%",
        "CGST PAYABLE",
        "Less : Short and Excess",
        "Less : Short & Excess",
        "Round Off",
        "ROUNDING",
        "Cess",
    ):
        assert li.is_ledger_row(description), description


def test_a_plain_item_description_is_not_a_ledger_row():
    for description in (
        "Professional Fee",
        "Freight charges",
        "Airtel Postpaid - 9811047777",
        "Consultancy Services",
        "Less Discount",          # "less" alone is not enough
        "",
        None,
    ):
        assert not li.is_ledger_row(description), description


# ===========================================================================
# (b) the amount half of the rule
# ===========================================================================


def test_amounts_parse_indian_formats_and_negative_forms():
    assert str(li.parse_amount("1,275.84")) == "1275.84"
    assert str(li.parse_amount("12,34,567.89")) == "1234567.89"
    assert str(li.parse_amount("\u20b9 180.00")) == "180.00"
    assert str(li.parse_amount("(-)0.46")) == "-0.46"
    assert str(li.parse_amount("(0.46)")) == "-0.46"
    assert str(li.parse_amount("-0.46")) == "-0.46"
    assert li.parse_amount("-") is None
    assert li.parse_amount("Nil") is None
    assert li.parse_amount(None) is None


def test_an_amount_matches_a_head_to_the_paisa_and_round_off_ignores_sign():
    heads = {"igst_amount": "180.00", "cgst_amount": "1,275.84", "round_off": "0.46"}
    assert li.matches_a_printed_head("180", heads) == ("igst_amount", "180.00")
    assert li.matches_a_printed_head("1275.84", heads) == ("cgst_amount", "1,275.84")
    # Round-off is printed either way round and means the same thing.
    assert li.matches_a_printed_head("(-)0.46", heads) == ("round_off", "0.46")
    assert li.matches_a_printed_head("0.46", {"round_off": "-0.46"}) == ("round_off", "-0.46")
    # A paisa apart is a different number.
    assert li.matches_a_printed_head("180.02", heads) is None
    # A head the read does not carry cannot match anything.
    assert li.matches_a_printed_head("180", {"cess_amount": None}) is None
    # CGST and SGST are routinely the SAME number: the row's own words decide
    # which head it is reported against.
    equal = {"cgst_amount": "1,275.84", "sgst_amount": "1,275.84"}
    assert li.matches_a_printed_head("1275.84", equal, "Output SGST-9%")[0] == "sgst_amount"
    assert li.matches_a_printed_head("1275.84", equal, "Output CGST - 9%")[0] == "cgst_amount"


# ===========================================================================
# The whole rule: both halves, the controls, and the renumbering
# ===========================================================================


def _lines(**by_line) -> dict[str, str | None]:
    """``line.1.description=...`` from ``{'1': {'description': ..., ...}}``."""
    out: dict[str, str | None] = {}
    for number, values in by_line.items():
        for name, text in values.items():
            out[fv2.line_key(int(number), name)] = text
    return out


def test_doctor_185_shape_drops_the_igst_row_and_keeps_one_line():
    values = _lines(
        **{
            "1": {"description": "Professional Fee", "hsn_sac": "998212",
                  "quantity": "1", "rate": "1,000.00", "amount": "1,000.00"},
            "2": {"description": "IGST Payable Account", "amount": "180.00"},
        }
    )
    result = li.filter_lines(values, {"igst_amount": "180.00"})
    assert [d.line_no for d in result.dropped] == [2]
    assert result.dropped[0].matched_head == "igst_amount"
    assert result.dropped[0].head_value == "180.00"
    assert result.values[fv2.line_key(1, "description")] == "Professional Fee"
    assert all("line.2" not in key for key in result.values)


def test_doctor_186_shape_drops_cgst_sgst_and_short_excess_and_renumbers():
    values = _lines(
        **{
            "1": {"description": "Professional Fee", "amount": "6,825.00"},
            "2": {"description": "Output CGST - 9%", "amount": "1,275.84"},
            "3": {"description": "Output SGST-9%", "amount": "1,275.84"},
            "4": {"description": "Less : Short & Excess", "amount": "0.32"},
            "5": {"description": "Retainer Fee", "amount": "500.00"},
        }
    )
    heads = {"cgst_amount": "1,275.84", "sgst_amount": "1,275.84", "round_off": "-0.32"}
    result = li.filter_lines(values, heads)
    assert [(d.line_no, d.matched_head) for d in result.dropped] == [
        (2, "cgst_amount"), (3, "sgst_amount"), (4, "round_off"),
    ]
    # The two real lines are KEPT and numbered 1 and 2, with no gap.
    assert result.values[fv2.line_key(1, "description")] == "Professional Fee"
    assert result.values[fv2.line_key(2, "description")] == "Retainer Fee"
    assert sorted({fv2.parse_line_key(key)[0] for key in result.values}) == [1, 2]
    assert result.renumber[fv2.line_key(5, "description")] == fv2.line_key(2, "description")


def test_a_ledger_name_whose_amount_matches_nothing_printed_stays():
    """The control cases, one per half of the rule."""
    values = _lines(
        **{
            "1": {"description": "Freight IGST charges", "amount": "500.00"},
            "2": {"description": "Output", "amount": "42.00"},
            "3": {"description": "Professional Fee", "amount": "180.00"},
        }
    )
    result = li.filter_lines(values, {"igst_amount": "180.00"})
    assert result.dropped == []
    assert sorted(result.values) == sorted(values)
    # Every kept line keeps its ORIGINAL number when nothing was dropped.
    assert result.renumber[fv2.line_key(3, "amount")] == fv2.line_key(3, "amount")


def test_a_handwritten_or_empty_line_is_never_dropped():
    values = _lines(
        **{
            "1": {"description": "Output CGST - 9%"},            # no amount at all
            "2": {"amount": "180.00"},                            # no description
            "3": {"description": "IGST Payable Account", "amount": "180.00"},
        }
    )
    result = li.filter_lines(values, {"igst_amount": "180.00"})
    # Only the row with BOTH halves is dropped.
    assert [d.line_no for d in result.dropped] == [3]
    assert result.values[fv2.line_key(1, "description")] == "Output CGST - 9%"
    assert result.values[fv2.line_key(2, "amount")] == "180.00"


def test_a_document_with_no_line_items_is_untouched():
    assert li.filter_lines({}, {}).dropped == []
    assert li.filter_lines({"invoice_total": "1,180.00"}, {}).dropped == []


# ===========================================================================
# The prompt says it, in both places, and stays tight
# ===========================================================================


def test_the_prompt_rules_out_tax_rows_and_past_bill_tables():
    prompt = ex._SYSTEM_PROMPT
    # (i) a tax / round-off / subtotal / total row is not a line item.
    assert "is NOT a line item" in prompt
    assert "round-off or \"short and excess\" row" in prompt
    assert "read its amount into the matching tax, round-off or total field" in prompt
    # (ii) a past-bills / usage / payment-history table is not a line item either.
    assert "A table of past bills, usage history, payment history or earlier periods" in prompt
    assert "lines are only the charges of THIS document" in prompt
    # And the field list handed to the model says the same thing.
    field_list = fv2.prompt_field_list()
    assert "past-bill/history row is not a line" in field_list


def test_the_extraction_prompt_stays_inside_its_length_budget():
    """The prompt was 1477 chars; the two new sentences bring it to 1888, so the
    guard is set at 1900 — room for a wording fix, not for a new rule."""
    assert len(ex._SYSTEM_PROMPT) <= 1900, f"system prompt grew to {len(ex._SYSTEM_PROMPT)} chars"
    assert len(ex._SYSTEM_PROMPT) == 1888
