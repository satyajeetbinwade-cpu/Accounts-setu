"""Setu GST 2B vs Books Reconciliation Engine (Module 2A).

Follows the architecture's 6-step flow:
  1. Ingest      - GSTR-2B JSON + Tally purchase register  (parsers)
  2. Normalise   - normalizer (Tier-A)
  3. Match       - fuzzy deterministic matcher (Tier-A)
  4. Classify    - 4-way classification (Tier-A)
  5. Compute ITC - compute_itc (Tier-A)
  6. Review      - review_packet + Sarvam-105B explanation (judgement only)

F5 integrity checks gate the whole run: nothing is trusted unless the
control-total and reconciliation-of-the-reconciliation checks pass.
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import MatchRules, Settings
from .integrity import run_all_validation
from .itc import build_summary, compute_itc
from .matcher import match_engine
from .models import Invoice, ReviewPacket
from .parsers import parse_gstr2b_json, parse_tally_purchase_rows
from .review_packet import build_review_packet
from .sarvam import SarvamClient


class ReconciliationEngine:
    """Orchestrates a single GST 2B vs Books reconciliation run."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.sarvam = SarvamClient(self.settings)

    # ------------------------------------------------------------------
    # Run from files (typical flow)
    # ------------------------------------------------------------------
    async def run_files(
        self,
        client_id: str,
        period: str,
        gstr2b_path: str | Path,
        tally_rows: list[Any],
        client_config: dict | None = None,
        portal_ct: tuple[int, Decimal] | None = None,
        tally_ct: tuple[int, Decimal] | None = None,
        output_tax: Decimal | str = "0",
    ) -> ReviewPacket:
        portal = parse_gstr2b_json(gstr2b_path)
        books = parse_tally_purchase_rows(tally_rows)
        return await self.run(
            client_id=client_id,
            period=period,
            portal_invoices=portal,
            book_invoices=books,
            client_config=client_config,
            portal_ct=portal_ct,
            tally_ct=tally_ct,
            output_tax=output_tax,
        )

    # ------------------------------------------------------------------
    # Run from already-normalised invoice lists
    # ------------------------------------------------------------------
    def run_deterministic(
        self,
        client_id: str,
        period: str,
        portal_invoices: list[Invoice],
        book_invoices: list[Invoice],
        client_config: dict | None = None,
        portal_ct: tuple[int, Decimal] | None = None,
        tally_ct: tuple[int, Decimal] | None = None,
        output_tax: Decimal | str = "0",
    ) -> ReviewPacket:
        """Steps 1-5 + packet assembly: fully deterministic, no event loop.

        Matching, classification, ITC and F5 gating are Tier-A code; this is
        the only path the sync runner (and therefore the FastAPI/TestClient
        sync boundary) touches. The async `run` adds the optional LLM layer.
        """
        rules = MatchRules.from_client_config(client_config)

        # Step 1-2: normalise already done at ingest; run structural validation
        # Step 3-4: match + classify
        results = match_engine(portal_invoices, book_invoices, rules)

        # Step 5: ITC
        itc = compute_itc(results, Decimal(str(output_tax)))
        summary = build_summary(results)

        # F5 gates the packet -- a failed integrity check still produces a
        # packet, but it is flagged as BLOCKED so no downstream stage trusts it.
        validation = run_all_validation(
            portal_invoices, book_invoices, results, portal_ct, tally_ct)

        packet = build_review_packet(
            client_id=client_id,
            period=period,
            results=results,
            itc=itc,
            summary=summary,
            validation=validation,
            voucher_date=None,
            llm_narrations={},
        )
        return packet

    async def run(
        self,
        client_id: str,
        period: str,
        portal_invoices: list[Invoice],
        book_invoices: list[Invoice],
        client_config: dict | None = None,
        portal_ct: tuple[int, Decimal] | None = None,
        tally_ct: tuple[int, Decimal] | None = None,
        output_tax: Decimal | str = "0",
    ) -> ReviewPacket:
        """Full pipeline: deterministic core + optional Sarvam judgement layer."""
        packet = self.run_deterministic(
            client_id=client_id,
            period=period,
            portal_invoices=portal_invoices,
            book_invoices=book_invoices,
            client_config=client_config,
            portal_ct=portal_ct,
            tally_ct=tally_ct,
            output_tax=output_tax,
        )
        # Step 6: LLM explanation (judgement only, graceful degradation).
        # Rebuild with LLM narrations layered on the deterministic packet.
        llm_narrations = await self._llm_layer(packet)
        rebuilt = build_review_packet(
            client_id=client_id,
            period=period,
            results=packet.exceptions,
            itc=packet.itc,
            summary=packet.summary,
            validation=packet.validation,
            voucher_date=None,
            llm_narrations=llm_narrations,
        )
        # _llm_layer already gated the AI summary (draft + verifier, or the
        # deterministic fallback). Rebuilding the packet would reset it to "",
        # so carry it over explicitly.
        rebuilt.ai_summary = packet.ai_summary
        return rebuilt

    # ------------------------------------------------------------------
    # LLM layer (Drafter/Verifier; never blocks the pipeline)
    # ------------------------------------------------------------------
    async def _llm_layer(self, packet: ReviewPacket) -> dict[str, str]:
        if not self.sarvam.available():
            return {}

        async def with_fallback(coro, default):
            try:
                return await asyncio.wait_for(coro, timeout=300)
            except Exception:
                return default

        narrations = {}

        # Draft the plain-English summary
        draft = await with_fallback(self.sarvam.draft_summary(packet), "")
        if draft:
            ok = await with_fallback(self.sarvam.verify_summary(draft, packet), True)
            if ok:
                packet.ai_summary = draft
            else:
                # Verifier rejected: fall back to a deterministic summary instead
                s = packet.summary
                packet.ai_summary = (
                    f"{s.get('matched')} invoices matched, {s.get('not_in_books')} "
                    f"not in books, {s.get('not_in_portal')} not in portal, "
                    f"{s.get('amount_diff')} with amount differences."
                )

        # Draft narrations for a capped batch of 'Not in Books' items (LLM is
        # for narration text only; amounts remain Tier-A).
        pending = [r for r in packet.exceptions
                   if r.status.value == "not_in_books"][:10]
        for r in pending:
            inv = r.portal_invoice
            narr = await with_fallback(self.sarvam.draft_jv(r), "")
            if narr:
                narrations[inv.invoice_no] = narr
        return narrations

    # ------------------------------------------------------------------
    # Sync convenience
    # ------------------------------------------------------------------
    def run_sync(self, *args, **kwargs) -> ReviewPacket:
        """Run the pipeline synchronously (deterministic core only).

        Never spins up an event loop - safe to call from FastAPI handlers,
        the TestClient, notebooks and scripts alike. The optional Sarvam
        judgement layer is applied by the async `run` path only.
        """
        return self.run_deterministic(*args, **kwargs)
