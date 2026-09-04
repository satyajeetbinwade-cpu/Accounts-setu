# Setu AI Build Team

A team of 8 AI subagent roles that *build* the Setu accounting pipeline. These are
**build-time agents** (they construct the product), distinct from Setu's **runtime
agents** (the Lead + 6 sub-agents that operate the finished pipeline).

All agents run on **Sarvam-105B** (the sole current Sarvam chat model; sarvam-30b is
deprecated). The broader Sarvam API family (Vision, Translation, etc.) is baked *into*
the Setu product itself — see `Setu_Implementation_Reference.md` Section 8.

## Accuracy philosophy (read first)

Setu is a financial-accuracy product. The team's process mirrors the product's process:

1. **Deterministic Tier-A code first.** Anything rule-based (date math, amount
   comparison, GSTIN/HSN validation, TDS rates, balance checks, Levenshtein matching,
   ITC eligibility) is written as pure Python/SQL — **the LLM never does arithmetic.**
2. **Review-gated, not free-flowing.** Specialists don't merge work directly. The
   Team Lead collects, reviews, integrates, and signs off — the build-time analogue
   of Setu's pipeline gates.
3. **Drafter/Verifier on LLM-output code.** When an engineer writes a function that
   *invokes* sarvam-105b at runtime (e.g. JV drafting), QA runs a second independent
   review pass on the prompt + output schema.
4. **Human-in-the-loop at every gate.** Full autonomy is the wrong goal for a
   financial product. The human (you / the CA) approves at each phase gate.

## The roster

| # | Role file | Sarvam model | Owns |
|---|---|---|---|
| 1 | `orchestrator.md` | 105b | Team Lead — dispatch, review gates, sign-off |
| 2 | `product-manager.md` | 105b | Phase requirements + acceptance criteria |
| 3 | `backend-engineer.md` | 105b | FastAPI, engines, Tally, Sarvam clients, Celery |
| 4 | `frontend-engineer.md` | 105b | React + Tailwind, 4 screens, evidence UI |
| 5 | `domain-accounting.md` ⭐ | 105b | GST/TDS/ITC/TB/depreciation rules (accuracy lynchpin) |
| 6 | `security-compliance.md` | 105b | DPDP, audit trail, RBAC, data residency |
| 7 | `testing-qa.md` | 105b | Golden test sets, accuracy regression, Drafter/Verifier |
| 8 | `devops.md` | 105b | Docker, Alembic, env, CI, reproducible builds |

⭐ Domain-Accounting is where accuracy is won or lost — every deterministic rule must
be encoded correctly and validated against the SOP (reference doc Section 3).

## How to use this team (Option A — manual role-switching)

1. Start a Cline session in the `setu/` folder.
2. **Load a role:** tell Cline *"Operate as the role defined in `.agents/<role>.md` for
   the following task: ..."*. Cline reads the file and adopts that persona, scope, and
   constraints for the task.
3. **Hand-off:** each role's output lands as a file in the workspace (e.g. PM writes
   `requirements/phase-0-req.md`). The next role reads that artifact as input.
4. **Switch roles** for the next task. One role per task per session keeps scope clean.
5. **Review gates:** before a phase advances, the Team Lead runs the
   `templates/review-gate-checklist.md` and a human approves.

### Typical phase flow

```
PM ──► requirements/phase-N-req.md
         │
Team Lead ──► requirements/phase-N-tasks.md  (decomposition)
         │
Backend / Frontend / Domain ──► src/** , frontend/**  (parallel where independent)
         │
Security ──► reviews anything touching data/auth/Tally/external
         │
QA ──► tests/** + golden sets + accuracy-regression-report
         │
Team Lead ──► review-gate-checklist + sign-off
         │
Human ──► approve ──► next phase
```

## Templates

- `templates/phase-requirements.md` — PM output format
- `templates/task-breakdown.md` — Team Lead dispatch format
- `templates/review-gate-checklist.md` — per-role gate criteria
- `templates/accuracy-regression-report.md` — QA regression output format

## Scope guard

These roles build Setu. They do **not** implement Setu's runtime Lead/sub-agents (that
is product code the Backend/Domain engineers write *during* the phases). Keep the two
layers separate to avoid confusion.
