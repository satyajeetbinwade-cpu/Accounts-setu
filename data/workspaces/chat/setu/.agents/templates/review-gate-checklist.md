# Template: Review Gate Checklist

> Used by the **Team Lead** at each gate. Each section is signed by the named role.
> A phase does not advance until all sections are signed AND a human approves.

## Gate: Phase N — <milestone or "phase end">

Date: <YYYY-MM-DD>

### 1. PM — scope & acceptance
- [ ] All in-scope items delivered
- [ ] All acceptance criteria have a passing test mapped to them
- [ ] No undocumented scope creep
Signed: PM ☐

### 2. Backend Engineer — build quality
- [ ] pytest / mypy / ruff clean
- [ ] Every public function documented + unit-tested
- [ ] No LLM doing arithmetic/rate-application (Tier-A rule verified)
- [ ] LLM-output code paths have Drafter/Verifier companions
Signed: Backend ☐

### 3. Frontend Engineer — UI quality (if phase touches UI)
- [ ] tsc + lint clean; vite build succeeds
- [ ] Every AI-decision UI shows confidence + evidence
- [ ] No write action auto-submits; mobile + dark mode verified
Signed: Frontend ☐

### 4. Domain-Accounting — rule correctness ⭐
- [ ] Every encoded rule has a cited source in docs/rule-source.md
- [ ] Unit tests cover happy path + edge cases; all pass
- [ ] Rules requiring CA confirmation flagged and escalated
Signed: Domain ☐  Human-CA review required for: <list> ☐

### 5. Security & Compliance
- [ ] No full ledgers/PII sent to Sarvam (only reasoning snippets)
- [ ] audit_trail immutability verified (UPDATE/DELETE rejected)
- [ ] No secrets in repo; RBAC negative tests pass; webhooks verified
Signed: Security ☐  (see docs/security-review.md)

### 6. Testing / QA — accuracy
- [ ] All suites green; lint/typecheck clean
- [ ] Accuracy-regression report attached; no metric drop vs baseline
- [ ] Golden sets sourced from real cases / domain-confirmed (not invented)
Signed: QA ☐  (see docs/accuracy-regression-phase-N.md)

### 7. DevOps — environment
- [ ] docker compose up healthy on clean machine
- [ ] Migrations reversible; CI passes end-to-end
Signed: DevOps ☐

### 8. Team Lead — consolidation & sign-off
- [ ] Conflicts resolved; ADL updated
- [ ] Open risks listed with owners
- [ ] Phase sign-off note written (docs/phase-N-signoff.md)
Signed: Team Lead ☐

### Human approval (required to advance)
- [ ] Approved by: <name> on <date>
Notes: <any conditions>
