"""Sarvam-105B client for the reconciliation engine.

Reconciled design: sarvam-105b is the ONLY model. It is used strictly for
judgement -- plain-English summaries, classification rationales, and JV
narrations. It NEVER does arithmetic or matching (that's Tier-A code).

A Drafter/Verifier pattern guards write-path LLM output: we draft, then
have a second 105b call verify the draft for hallucination/consistency.
If the API key is missing or the call fails, the engine degrades gracefully
(no LLM summary / rationale), which never blocks the gate.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings


class SarvamClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    def available(self) -> bool:
        return self.settings.llm_enabled and self.settings.has_api_key

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------
    async def chat(self, messages: list[dict], temperature: float = 0.3,
                   max_tokens: int | None = None) -> str:
        """Call Sarvam-105B (OpenAI-compatible endpoint).

        max_tokens defaults to Settings.sarvan_max_tokens (generous 2048) so
        that Sarvam-105B's built-in thinking mode never eats the whole budget
        and returns an empty `content`. If the model returns no content we
        raise a ValueError so callers hit the graceful-fallback path.
        """
        url = f"{self.settings.sarvan_base_url}".rstrip("/") + "/v1/chat/completions"
        headers = {
            "api-subscription-key": self.settings.sarvan_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None
                          else self.settings.sarvan_max_tokens,
        }
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content:
                # thinking mode consumed everything / empty reply -> treat as failure
                raise ValueError("Sarvam-105B returned empty content (reasoning ate the budget)")
            return content

    # ------------------------------------------------------------------
    # Judgement tasks
    # ------------------------------------------------------------------
    async def draft_summary(self, packet: "ReviewPacket") -> str:
        """Plain-English explanation of a reconciliation run for reviewers."""
        if not self.available():
            return ""
        s = packet.summary
        prompt = (
            "You are a senior accountant's assistant at a CA firm. Write a short, "
            "plain-English summary (max 120 words) of this GST 2B vs Books "
            "reconciliation result for a human reviewer. DO NOT invent figures -- "
            "use only the numbers provided. No arithmetic.\n\n"
            f"Matched: {s.get('matched')}, Not in Books: {s.get('not_in_books')}, "
            f"Not in Portal: {s.get('not_in_portal')}, Amount Diff: {s.get('amount_diff')}.\n"
            f"Totals (tax amounts): {s.get('totals_tax_amount')}.\n"
            "ITC: " + json.dumps(packet.itc.to_dict() if packet.itc else {}) + "\n\n"
            "Summary:"
        )
        try:
            return (await self.chat([{"role": "user", "content": prompt}])).strip()
        except Exception:
            return ""

    async def draft_jv(self, exception: "MatchResult") -> str:
        """Draft a narrative (not numbers) for a corrective JV."""
        if not self.available():
            return ""
        e = exception.portal_invoice
        prompt = (
            "You are drafting a journal voucher narration for a CA firm. The engine "
            "already verified the amounts and balance -- you only write the narration. "
            "Keep it to 2 sentences, professional, mentioning the supplier and invoice.\n"
            f"GSTIN: {e.gstin}, Invoice: {e.invoice_no}, Date: {e.date}, "
            f"Taxable: {e.taxable_value}.\n"
            "Narration:"
        )
        try:
            return (await self.chat([{"role": "user", "content": prompt}], temperature=0.7)).strip()
        except Exception:
            return ""

    async def verify_summary(self, summary: str, packet: "ReviewPacket") -> bool:
        """Verifier leg: check the draft summary didn't invent numbers."""
        if not self.available() or not summary:
            return True
        s = packet.summary
        prompt = (
            "Verify that the accountant's summary below uses ONLY these facts and "
            "invents no figures. Answer with a single word: YES or NO.\n\n"
            f"Facts: matched={s.get('matched')}, not_in_books={s.get('not_in_books')}, "
            f"not_in_portal={s.get('not_in_portal')}, amount_diff={s.get('amount_diff')}.\n"
            f"ITC: {json.dumps(packet.itc.to_dict() if packet.itc else {})}\n\n"
            f"Summary to verify:\n{summary}\n\n"
            "Verdict (YES/NO):"
        )
        try:
            reply = (await self.chat([{"role": "user", "content": prompt}], temperature=0.0)).strip().upper()
            return reply.startswith("YES")
        except Exception:
            return True
