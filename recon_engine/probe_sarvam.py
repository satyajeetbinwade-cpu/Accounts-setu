"""Probe the Sarvam-105B judgement layer: config check -> live ping -> full run.

Validates that the wiring in sarvam.py / engine._llm_layer works against the
real API before you commit the key to production.

Usage:
    # 1) config + wiring check only (no key needed)
    python -m recon_engine.probe_sarvam

    # 2) live judgement-layer probe + full agent run with LLM on
    SARVAM_API_KEY=sk_your_key_here python -m recon_engine.probe_sarvam --run-agent
"""
from __future__ import annotations

import argparse
import asyncio
import time

from .config import Settings
from .engine import ReconciliationEngine
from .sarvam import SarvamClient


def probe(settings: Settings) -> dict:
    """Check configuration and, if a key is present, live-ping the model."""
    result = {
        "model": settings.model,
        "base_url": settings.sarvan_base_url,
        "max_tokens": settings.sarvan_max_tokens,
        "llm_enabled": settings.llm_enabled,
        "has_api_key": settings.has_api_key,
        "available": False,
        "live": None,
    }
    if not settings.has_api_key:
        result["note"] = (
            "No SARVAM_API_KEY set. Export it to enable the judgement layer:\n"
            '  export SARVAM_API_KEY=sk_...   (optionally SARVAM_MAX_TOKENS, SARVAM_MODEL)'
        )
        return result
    if not settings.llm_enabled:
        result["note"] = "SETU_LLM_ENABLED=0 - LLM layer switched off in settings."
        return result

    # Live ping: prove auth + endpoint + model work
    async def _ping(sarvam: SarvamClient):
        t0 = time.perf_counter()
        reply = await sarvam.chat(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            temperature=0.0,
            max_tokens=64,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        return reply, latency_ms

    try:
        reply, latency_ms = asyncio.run(_ping(SarvamClient(settings)))
        result["available"] = True
        result["live"] = {
            "status": "OK",
            "reply": reply,
            "latency_ms": round(latency_ms, 1),
        }
    except Exception as e:  # network/auth/model error
        result["live"] = {"status": "ERROR", "detail": f"{type(e).__name__}: {e}"}
    return result


def run_agent_with_llm(settings: Settings) -> dict:
    """Full pipeline with the judgement layer on: ai_summary + narrations."""
    from .agent import auto_reconcile

    res = auto_reconcile(
        gstr2b_path="sample_data/gstr2b_sample_full.json",
        tally_path="sample_data/tally_purchase_sample_full.csv",
        tb_path="sample_data/SampleTB-AI.xlsx",
        client_id="acme", period="2024-08", output_tax="120000",
        settings=settings,
    )
    packet = res["packet"]
    return {
        "llm_used": res.get("llm_used"),
        "ai_summary": packet.ai_summary,
        "jv_narrations": {
            jv.exception_ref: jv.narration for jv in packet.drafted_jvs
        },
        "verification_pass": res["verification"].get("pass"),
        "reports": list(res["reports"].keys()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe Sarvam-105B judgement layer")
    ap.add_argument("--run-agent", action="store_true",
                    help="also run the full agent end-to-end with the LLM enabled")
    args = ap.parse_args()

    settings = Settings()
    print("=" * 74)
    print("SARVAM-105B JUDGEMENT LAYER PROBE")
    print("=" * 74)
    print(f"  model      : {settings.model}")
    print(f"  base_url   : {settings.sarvan_base_url}")
    print(f"  max_tokens : {settings.sarvan_max_tokens}")
    print(f"  llm_enabled: {settings.llm_enabled}   key present: {bool(settings.sarvan_api_key)}")
    print("-" * 74)

    result = probe(settings)
    if result.get("note"):
        print(f"  {result['note']}")
        if not settings.has_api_key:
            print("  (deterministic Tier-A path remains fully functional without it)")
            return 0

    live = result.get("live")
    if live and live["status"] == "OK":
        print(f"  LIVE PING  : OK   latency {live['latency_ms']} ms")
        print(f"  reply      : {live['reply']!r}")
        print("  -> Sarvam-105B endpoint + auth verified.")
        if args.run_agent:
            print("-" * 74)
            print("  Running full agent with judgement layer...")
            run = run_agent_with_llm(settings)
            print(f"  llm_used           : {run['llm_used']}")
            print(f"  ai_summary         : {run['ai_summary'][:500]}")
            print(f"  verification pass  : {run['verification_pass']}")
            print(f"  reports            : {', '.join(run['reports'])}")
        return 0
    if live and live["status"] == "ERROR":
        print(f"  LIVE PING  : ERROR")
        print(f"  detail     : {live['detail']}")
        print("  -> Check SARVAM_API_KEY / SARVAM_BASE_URL / network / billing.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
