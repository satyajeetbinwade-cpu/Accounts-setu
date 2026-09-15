"""Setu Reconciliation Engine (Module 2A - GST 2B vs Books).

Pipeline: ingest -> normalise -> match -> classify -> compute ITC -> review.
F5 integrity validation gates every run. Sarvam-105B is used for judgement
(summaries, narrations) only -- never for arithmetic or matching.
"""
from .engine import ReconciliationEngine
from .config import MatchRules, Settings
from .models import (
    Confidence, Invoice, MatchResult, MatchStatus,
    ITCComputation, DraftedJV, ReviewPacket,
)
from .normalizer import (
    make_invoice, norm_gstin, norm_invoice_no, norm_date_str,
    norm_amount, validate_gstin, validate_invoice,
)
from .matcher import match_engine, levenshtein
from .parsers import parse_gstr2b_json, parse_tally_purchase_rows
from .itc import compute_itc

__all__ = [
    "ReconciliationEngine",
    "MatchRules", "Settings",
    "Confidence", "Invoice", "MatchResult", "MatchStatus",
    "ITCComputation", "DraftedJV", "ReviewPacket",
    "make_invoice", "norm_gstin", "norm_invoice_no", "norm_date_str",
    "norm_amount", "validate_gstin", "validate_invoice",
    "match_engine", "levenshtein",
    "parse_gstr2b_json", "parse_tally_purchase_rows",
    "compute_itc",
]
