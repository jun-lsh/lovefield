"""Challenge dataclass - the unit of work, one per Discord thread.

Discord-agnostic: nothing here imports `discord`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid runtime coupling; Channel is a structural protocol
    from .channel import Channel

# Lifecycle. The control plane drives transitions; dummies in phase 1 walk
# new -> dispatched -> solving -> (racing|paused)* -> candidate, or -> killed.
STATES = {
    "new",          # parsed, not yet routed
    "dispatched",   # lane picked, worker registered, run() about to start
    "solving",      # worker actively stepping
    "racing",       # escalated: multiple framings on the same thread
    "paused",       # control-paused; worker idles, holds state
    "candidate",    # produced a candidate flag (awaiting human submit)
    "needs_human",  # stuck in a way only a human resolves
    "solved",       # flag confirmed
    "killed",       # cancelled / stood down
}


@dataclass
class Challenge:
    """Everything a worker needs about the challenge it's solving.

    `channel` is the bound Channel impl for this thread (console in sim,
    Discord in prod) - workers narrate through it. `steers` is the running
    log of operator nudges folded in via `!steer`.
    """

    id: str
    name: str
    category: str
    description: str = ""
    thread_id: int | None = None
    channel: "Channel | None" = None
    files: list[str] = field(default_factory=list)
    box: str | None = None  # target host, when the challenge faces a live box
    hard: bool = False      # operator foresight at allocation -> deep_solver
    state: str = "new"
    steers: list[str] = field(default_factory=list)
    # Triage's advisory report (set when an agent calls triage_report; the
    # difficulty/technique/escalation_reason are carried into a specialist respawn
    # as the handoff). The report RECOMMENDS - the operator decides.
    difficulty: int = 0          # 1 (trivial) .. 5 (very hard); 0 = unassessed
    technique: str = ""          # named attack class the triage agent identified
    escalation_reason: str = ""  # the blockers / why it might need help
    gist: str = ""               # what the challenge is / what it wants
    recommendation: str = ""     # solo_finish|race|specialist|deep_solver|needs_human
    confidence: str = ""         # low|medium|high - how sure the cheap model is

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown challenge state: {self.state!r}")
