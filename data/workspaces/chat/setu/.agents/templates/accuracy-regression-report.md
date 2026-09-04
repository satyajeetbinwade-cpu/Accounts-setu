# Template: Accuracy Regression Report

> Used by the **Testing / QA Agent** after any change to a rule/engine/matcher.
> Save as `docs/accuracy-regression-phase-N.md` (phase-end) or
> `docs/accuracy-regression-<change-id>.md` (per-change).

## Change
- **What changed:** <PR/commit summary>
- **Engine(s) affected:** <gst_2b / tds_chain / 26as / bank / vendor / opening / loan / salary / gate / jv_drafter>
- **Date:** <YYYY-MM-DD>

## Golden test set
- **Engine:** <name>
- **Set size:** <N> cases
- **Source:** <real anonymized cases / domain-expert-confirmed / synthetic-with-known-answer>
- **Path:** `tests/golden/<engine>/`

## Results

| Metric | Baseline (prior) | After change | Delta | Pass threshold |
|--------|------------------|--------------|-------|----------------|
| Overall accuracy | <%> | <%> | <+/- %> | ≥98% primary classification |
| Precision (per exception type) | | | | no drop |
| Recall (per exception type) | | | | no drop |
| Matched precision | | | | |
| Not-in-Books precision | | | | |
| Not-in-Portal precision | | | | |
| Amount-Diff precision | | | | |

## Per-type confusion (if applicable)
| Actual \ Predicted | Matched | Not-in-Books | Not-in-Portal | Amount-Diff |
|--------------------|---------|--------------|---------------|-------------|
| Matched | | | | |
| Not-in-Books | | | | |
| Not-in-Portal | | | | |
| Amount-Diff | | | | |

## Verdict
- [ ] No regression — merge allowed
- [ ] Regression detected — merge BLOCKED; list failing cases below

## Failing cases (if any)
- Case <id>: expected <X>, got <Y>, root cause <...>

## Drafter/Verifier checks (for LLM-output code paths)
- [ ] JV drafter: Verifier independently checks debits==credits + valid ledgers + narration matches exception
- [ ] Audit drafter: Verifier checks consistency with prior TB version
- [ ] All Verifier checks pass on the golden set
