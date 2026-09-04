# Template: Task Breakdown

> Used by the **Team Lead** to decompose a phase into specialist tasks after PM
> requirements are finalized. Save as `requirements/phase-N-tasks.md`.

## Phase N task breakdown

**Source requirements:** `requirements/phase-N-req.md`

### Tasks

| ID | Task | Owner role | Inputs | Outputs | Done-definition | Acceptance criterion |
|----|------|-----------|--------|---------|-----------------|----------------------|
| N.1 | <task> | Backend / Frontend / Domain / Security / QA / DevOps | <files> | <files> | <checkable> | <from req> |
| N.2 | ... | | | | | |

### Dispatch order (respect dependencies)
1. <ordered list — which tasks must finish before others start>

### Review gates (when in this phase)
- Gate A: after <milestone> — Security reviews <X>, QA runs <Y>
- Gate B: phase end — full gate checklist + human sign-off

### Drafter/Verifier triggers in this phase
- <list which outputs need a second-agent review, per role files>

### Architecture decisions to record
- <non-trivial choices → append to docs/adl.md with rationale>
