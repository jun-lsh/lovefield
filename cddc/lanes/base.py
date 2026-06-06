"""Lane = strategy/config, NOT a runner. The Worker does the running.

A lane is a light bundle: a name, a default mode (solo / solo->race / ...),
and - for phase 1 - a `dummy_script` the DummyWorker walks step by step. Later
phases hang the real per-lane helper set + system prompt off the same object;
`tools` is the seam for that (e.g. the phase-3 `web_search`/`triage_*` helpers)
so adding them is config, not a refactor.

Nothing here imports `discord`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Modes a dispatcher can pick = a lane's ESCALATION POLICY, not a headcount.
#   solo        -> 1 agent, not eligible to race (racing a guessy stego won't help).
#   solo->race  -> dispatch as 1 agent; *eligible* to fan out to a 2-3 model race
#                  IF it stalls / blows budget. It is NOT inherently >1 - it's
#                  "1 now, allowed to become 2-3 later." The fan-out itself is
#                  phase 5; in phase 1 `!race` just flips one worker's state to
#                  `racing` (a marker), it does not yet spawn extra racers.
#   specialist  -> the Windows VM lane (on-demand).
#   human-led   -> agent best-effort, human is primary (guess/RF/physical).
#   raw         -> unharnessed escape hatch for trivial challs.
#
# The LIVE agent count is a *separate axis*: len(registry.workers(thread_id)).
# That list grows for unrelated reasons (race escalation, deep-solver in
# parallel, on-site+fleet both joining) - which is why registry is a list.
#
# Modes are MUTABLE - expect this set to change. Keep dispatch logic from
# hard-branching on exact strings beyond the routing decision, so adding or
# renaming a mode stays a one-line edit.
MODES = {"solo", "solo->race", "specialist", "human-led", "raw"}


@dataclass(frozen=True)
class Lane:
    name: str
    default_mode: str
    dummy_script: tuple[str, ...]  # scripted steps the DummyWorker walks
    race_capable: bool = True
    # Seam (deferred): per-lane helper tool names the real worker loads in
    # phase 3+. Empty in phase 1 - dummies don't call tools.
    tools: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.default_mode not in MODES:
            raise ValueError(f"lane {self.name!r}: unknown mode {self.default_mode!r}")
