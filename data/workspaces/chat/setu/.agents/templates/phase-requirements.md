# Template: Phase Requirements

> Used by the **Product Manager** to define a phase before any engineer starts.
> Save as `requirements/phase-N-req.md`.

## Phase N: <name>

**Reference doc section:** Section 10, Phase N
**Pain points addressed:** (list from Section 2, e.g. "GST reconciliation issues")
**Depends on phases:** (e.g. "Phase 0 foundation")

### Goal
<1–3 sentences: what this phase delivers and why it matters>

### In scope
- <bullet list of concrete deliverables>

### Out of scope (explicit)
- <what is deliberately NOT built this phase, to prevent scope creep>

### Acceptance criteria (measurable — each must be checkable by QA)
- [ ] <criterion 1 — e.g. "GST matcher classifies ≥98% of the golden test set (≥50 cases) correctly on Matched/Not-in-Books">
- [ ] <criterion 2 — e.g. "`POST /api/exceptions/{id}/review` returns 200 and updates status; covered by a contract test">
- [ ] <criterion 3>
- [ ] All test suites green; lint + typecheck clean
- [ ] No secrets in repo; audit_trail immutability verified (if touched)

### Open questions / assumptions
- <anything the PM is assuming that the Team Lead or human must confirm>

### Risk notes
- <accuracy risks, integration risks, dependency risks>
