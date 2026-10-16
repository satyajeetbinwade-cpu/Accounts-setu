"""The ONE place an upload's stage is derived — from FACTS, never a label.

The Files list used to label each row from the persisted chain state alone, so a
finished upload could still read "Working — 31 of 31 pages read" while the triage
run and the extraction had both finished: the chain row was stale (it still said
queued) and the label trusted it. Every number here instead comes from rows that
describe what actually happened:

  * the latest triage run: status, pages_ok / pages_total / pages_failed;
  * the active documents, by their extraction status (complete / failed /
    skipped / queued / running).

Stages
------
``queued``      nothing has started yet (or the chain is only queued).
``reading``     the triage run is reading pages → "Reading pages (x of N)".
``extracting``  all pages read, the values are being read → "Extracting (y of M documents done)".
``ready``       every page read and the extraction finished → "Ready to review".
``attention``   finished, but a person must act (failed pages, failed documents,
                a partial run, an interrupted chain) — still reviewable.
``failed``      nothing on the file could be read.

``derive_stage`` is pure: same facts in, same stage out, no database, no state.
"""

from __future__ import annotations

from dataclasses import dataclass

STAGE_QUEUED = "queued"
STAGE_READING = "reading"
STAGE_EXTRACTING = "extracting"
STAGE_READY = "ready"
STAGE_ATTENTION = "attention"
STAGE_FAILED = "failed"

#: Triage run statuses that mean the pages are being read right now.
RUN_READING = ("queued", "running")
#: Chain states that mean the pipeline is working.
CHAIN_READING = ("queued", "triage_running")
CHAIN_EXTRACTING = ("extraction_running", "triage_done_extraction_pending")
#: Triage run statuses that finish the read but leave something for a person.
RUN_NEEDS_A_PERSON = ("partial", "interrupted")


@dataclass(frozen=True)
class StageFacts:
    """Everything the stage is allowed to depend on. All counts are plain ints."""

    started: bool = False          # is there a triage run at all?
    run_status: str = ""           # the triage run's status
    chain_state: str = ""          # the PERSISTED chain state ("" = never chained)
    pages_ok: int = 0
    pages_total: int = 0
    pages_failed: int = 0
    documents: int = 0             # ACTIVE documents
    documents_terminal: int = 0    # complete + failed + skipped
    documents_complete: int = 0
    documents_failed: int = 0
    documents_running: int = 0     # a worker holds them right now
    documents_queued: int = 0      # waiting for a worker (cost cap / resume)

    @property
    def documents_pending(self) -> int:
        return self.documents_running + self.documents_queued


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    detail: str
    running: bool
    needs_attention: bool
    done: int = 0
    total: int = 0
    show_progress: bool = False

    @property
    def percent(self) -> int:
        """0-100, for the determinate bar. Never raises, never divides by zero."""
        if not self.show_progress or self.total <= 0:
            return 0
        return int(max(0, min(100, round(100.0 * self.done / self.total))))

    @property
    def counter(self) -> str:
        return f"{self.done} of {self.total}" if self.total else ""


def derive_stage(facts: StageFacts) -> Stage:
    """The upload's stage, from the facts. See the module docstring for the map."""
    pages_total = max(0, int(facts.pages_total or 0))
    documents = max(0, int(facts.documents or 0))
    terminal = max(0, int(facts.documents_terminal or 0))
    chain = str(facts.chain_state or "")
    run = str(facts.run_status or "")

    # 1. Nothing has begun.
    if not facts.started and chain not in CHAIN_READING + CHAIN_EXTRACTING:
        return Stage(
            STAGE_QUEUED, "Queued",
            "Waiting to start — the triage run reads every page, then the extraction follows.",
            running=True, needs_attention=False,
        )

    # 2. The pages are being read. The RUN row is the fact; the chain state is
    #    only consulted when the run row says nothing at all (a stale chain must
    #    not keep a finished file looking busy).
    reading = run in RUN_READING or (not run and chain in CHAIN_READING)
    if reading:
        return Stage(
            STAGE_READING, f"Reading pages ({facts.pages_ok} of {pages_total or '?'})",
            "Every page is read on its own; progress is saved as it goes.",
            running=True, needs_attention=False,
            done=max(0, int(facts.pages_ok or 0)), total=pages_total,
            show_progress=pages_total > 0,
        )

    # 3. The values are being read. A RUNNING extraction row is proof a worker
    #    holds the document; the chain row alone can be stale (that staleness is
    #    what showed a finished file as "Working").
    extracting = chain in CHAIN_EXTRACTING or int(facts.documents_running or 0) > 0
    if extracting:
        return Stage(
            STAGE_EXTRACTING, f"Extracting ({terminal} of {documents} documents done)",
            "Each document is read twice by two providers; progress is saved as it goes.",
            running=True, needs_attention=False,
            done=terminal, total=documents, show_progress=documents > 0,
        )

    # 4. Nothing on the file could be read.
    if run == "failed" and documents == 0:
        return Stage(
            STAGE_FAILED, "Failed",
            "Nothing on this file could be read. Start it again, or upload a better copy.",
            running=False, needs_attention=True,
        )

    # 5. Finished, but something needs a person — still reviewable.
    if (
        run in RUN_NEEDS_A_PERSON
        or chain == "interrupted"
        or int(facts.pages_failed or 0) > 0
        or int(facts.documents_failed or 0) > 0
        or facts.documents_pending > 0
    ):
        return Stage(
            STAGE_ATTENTION, "Needs attention",
            "The file is ready to review; the failed pages can be retried and an "
            "unfinished extraction resumed.",
            running=False, needs_attention=True,
        )

    # 6. Everything read, extraction finished. NOTE: the upload row's own status is
    #    deliberately NOT consulted — it can still say "queued" after both stages
    #    have finished, which is exactly the row that used to look unfinished.
    if terminal >= documents:
        return Stage(
            STAGE_READY, "Ready to review",
            "Every page has been read and every document extracted.",
            running=False, needs_attention=False,
            done=terminal, total=documents,
        )

    # 7. Nothing above fits: something is unfinished and no worker holds it.
    return Stage(
        STAGE_ATTENTION, "Needs attention",
        "Some documents are still waiting — resume the file.",
        running=False, needs_attention=True,
    )


def progress_banner(stages: list[Stage], filenames: list[str]) -> str:
    """One live line for the Files list, or "" when nothing is running.

    ``stages`` and ``filenames`` are parallel lists, in row order.
    """
    live = [(stage, name) for stage, name in zip(stages, filenames) if stage.running]
    if not live:
        return ""
    if len(live) == 1:
        stage, name = live[0]
        if stage.key == STAGE_QUEUED:
            return f"{name} is queued — the triage run reads every page, then the extraction follows."
        if stage.key == STAGE_READING:
            return f"{name}: {stage.label.lower()} — the extraction follows, and progress is saved."
        return f"{name}: {stage.label.lower()}."
    reading = sum(1 for stage, _n in live if stage.key == STAGE_READING)
    extracting = sum(1 for stage, _n in live if stage.key == STAGE_EXTRACTING)
    queued = sum(1 for stage, _n in live if stage.key == STAGE_QUEUED)
    parts = []
    if reading:
        parts.append(f"{reading} reading pages")
    if extracting:
        parts.append(f"{extracting} extracting values")
    if queued:
        parts.append(f"{queued} queued")
    return f"{len(live)} file(s) in progress — " + ", ".join(parts) + ". Progress is saved as it goes."
