# Contributing

Thanks for contributing to the Setu GST 2B vs Books Reconciliation Engine.
The project follows the reconciled Setu architecture; please keep the core
design rules intact (see README → "Design rules").

## Local development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the full test suite (must stay green):

```bash
pytest -q
```

Run the app locally to try changes in the UI:

```bash
uvicorn recon_engine.api:app --port 8124
# open http://localhost:8124/
```

## Architecture guardrails

| Rule | Why |
|---|---|
| **LLM = judgement only** | Matching, classification, ITC and every arithmetic step must stay deterministic Tier-A code. The Sarvam layer (`sarvam.py`) may only *narrate* pre-computed results. No new LLM dependency may appear in `matcher.py`, `itc.py`, `integrity.py`, `normalizer.py`, `parser*`. |
| **F5 gate stays mandatory** | `integrity.py` gates every run; a failing check must produce a `BLOCKED` packet, never a silent pass. |
| **Graceful degradation** | No API key / API down must never block the pipeline — deterministic fallbacks only. |
| **Independent verification** | Any engine behavior change should be mirrored in `verification.py` (a separate implementation) and cross-checked by `tests/test_verification.py`. |

## Adding a parser or intake format

1. Add the parser in `recon_engine/parsers/` returning `list[Invoice]` via the
   normalisation layer (`normalizer.make_invoice`) — never raw dicts.
2. Header-tolerant column mapping goes through `parsers/excel_common.py`
   (`locate_header`/`iter_rows`).
3. Wire the extension into `agent.py::_load_portal`/`_load_books` and
   `intake.py` classification, and update the API allow-lists in `api.py`
   (`ALLOWED_PORTAL` / `ALLOWED_TALLY`).
4. Add parity + intake tests; keep sample fixtures in `sample_data/`.

## Pull-request checklist

- [ ] `pytest -q` passes (91 tests today)
- [ ] Deterministic core untouched by any LLM/API dependency
- [ ] New parsers go through the normaliser + parser tests
- [ ] F5 gate and independent verification still PASS on the sample fixtures
- [ ] README updated if user-facing flows change (upload formats, match rules,
      report outputs)
