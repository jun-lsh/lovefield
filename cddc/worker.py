"""Worker - the live control loop. Phase 1 ships DummyWorker (scripted steps).

This is the heart of phase 1: it proves the human<->orchestration control plane
(proactive progress, `!status` polls, free-form `!steer`, and control:
race/pause/resume/kill) with a scripted loop, so phase 4 just swaps the
per-step logic for a real agent loop and inherits all the plumbing for free.

Nothing here imports `discord` - it only ever touches a `Channel`.
"""

from __future__ import annotations

import asyncio
import time

from .challenge import Challenge
from .channel import Channel
from .lanes.base import Lane


def summary(
    post: str,
    findings: list[str],
    *,
    flag: str | None = None,
    needs_human: bool = False,
) -> str:
    """The FINDINGS / CANDIDATE FLAG / NEEDS HUMAN block.

    Discord-agnostic string builder - bot.py and the console both post the same
    block. Kept here (not in bot.py) so it's testable without a token.
    """
    lines = [post.strip(), "", "**FINDINGS**"]
    lines += [f"  - {f}" for f in findings] or ["  - (none)"]
    if flag:
        lines += ["", f"**CANDIDATE FLAG** -> `{flag}`  (human submits)"]
    if needs_human:
        lines += ["", "**NEEDS HUMAN** - stuck in a way a human must resolve"]
    return "\n".join(lines)


class Worker:
    """Base worker: identity, control flags, status, and the control plane.

    Subclasses implement `run()`. The control methods (steer/race/pause/resume/
    cancel) and `status()` are lane-agnostic and final - every worker, dummy or
    real, on-site or fleet, exposes the same surface so the registry can treat
    them uniformly.
    """

    def __init__(
        self,
        lane: Lane,
        chall: Challenge,
        channel: Channel,
        *,
        id: str,
        name: str,
        location: str = "offsite",  # "onsite" | "offsite" - seam for phase 6
        operator: str | None = None,
        tick: float = 0.05,         # seconds between scripted steps (sim pacing)
        min_post_interval: float = 0.0,  # rate-limit for heartbeat posts
        on_candidate=None,          # async hook(worker, flag) fired on a flag
    ) -> None:
        if location not in ("onsite", "offsite"):
            raise ValueError(f"location must be onsite|offsite, got {location!r}")
        self.lane = lane
        self.chall = chall
        self.channel = channel
        # Identity (seam for multi-agent / phase 6 unification + dashboard reg).
        self.id = id
        self.name = name
        self.location = location
        self.operator = operator

        # Progress snapshot (what `!status` reports).
        self.current_step = 0
        self.budget_used = 0.0  # 0..1 fraction of this worker's budget
        self.tried: list[str] = []
        self.findings: list[str] = []

        # Control flags.
        self._racing = False
        self._cancelled = False
        self._resume = asyncio.Event()
        self._resume.set()  # set == not paused
        self._inbox: list[str] = []  # direct steers (registry broadcast/target)
        self._prev_state = "new"     # to restore after a pause

        # Flag-validation handshake: a candidate flag does NOT insta-kill. It
        # halts the worker pending the operator's verdict (!solved / !continue).
        self._on_candidate = on_candidate
        self._validation = asyncio.Event()
        self._validation_kind: str | None = None  # "solved" | "continue"
        self._continue_reason: str | None = None
        self._solved = False  # hard stop -> solved (set by !solved)

        # Race-decision halt: at a stall the worker HOLDS (indefinitely) until an
        # operator decides with !race / !solo (or !kill). No timeout - a halt the
        # team is pinged about, not a silent auto-continue.
        self._race_gate = asyncio.Event()
        self._race_choice: str | None = None  # "race" | "solo"

        # Composable seam (deferred): a worker may later spawn sub-workers.
        self._sub_workers: list[Worker] = []

        # Posting cadence.
        self._tick = tick
        self._min_post_interval = min_post_interval
        self._last_post_t = 0.0
        self._last_post_state: str | None = None
        self._asked = False  # stall-ask fires at most once in the dummy

    # --- status / introspection (queryable seam for agent-to-agent later) ---
    def status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "location": self.location,
            "operator": self.operator,
            "lane": self.lane.name,
            "state": self.chall.state,
            "current_step": self.current_step,
            "budget_used": round(self.budget_used, 2),
            "racing": self._racing,
            "tried": list(self.tried),
            "steers": list(self.chall.steers),
            "findings": list(self.findings),
        }

    # --- control surface (registry routes !commands here) -------------------
    def steer(self, msg: str) -> None:
        """Fold a steer in via the direct inbox (registry broadcast/target).

        Operator `!steer` text from Discord normally arrives via
        `channel.drain_steer()`; this method is the direct path the registry
        uses to target one worker. Both are merged at the top of each loop.
        """
        self._inbox.append(msg)

    def race_now(self) -> None:
        self._racing = True
        if self.chall.state not in ("candidate", "solved", "killed"):
            self.chall.state = "racing"
        self._race_choice = "race"
        self._race_gate.set()  # release a worker holding at a race-ask

    def go_solo(self) -> None:
        """Inverse of race_now - decline / undo a race, drop back to solo."""
        self._racing = False
        if self.chall.state == "racing":
            self.chall.state = "solving"
        self._race_choice = "solo"
        self._race_gate.set()  # release a worker holding at a race-ask

    def pause(self) -> None:
        if self.chall.state in ("candidate", "solved", "killed"):
            return
        self._prev_state = self.chall.state
        self.chall.state = "paused"
        self._resume.clear()

    def resume(self) -> None:
        # restore racing if we were racing, else solving
        self.chall.state = "racing" if self._racing else "solving"
        self._resume.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._resume.set()      # unblock a paused loop so it can exit cleanly
        self._validation.set()  # unblock a worker halted at a candidate flag
        self._race_gate.set()   # unblock a worker holding at a race-ask

    def mark_solved(self) -> None:
        """Operator confirmed the candidate flag (`!solved`) - stand down."""
        self._solved = True
        self._validation_kind = "solved"
        self._validation.set()
        self._resume.set()  # unblock if it was paused by the halt

    def continue_with(self, reason: str) -> None:
        """Operator rejected the candidate (`!continue <why>`) - re-open.

        The reason is folded in as a steer so the worker knows what was wrong.
        Also lifts a pause (a halted racer resumes).
        """
        self._continue_reason = reason
        self._validation_kind = "continue"
        if reason:
            self._inbox.append(reason)
        self._validation.set()
        self._resume.set()
        if self.chall.state == "paused":
            self.chall.state = "racing" if self._racing else "solving"

    async def spawn(self, sub_lane: Lane, subtask: str) -> "Worker":
        """Composable seam - deferred. A worker could later run sub-workers.

        Intentionally unimplemented in phase 1; present so `run()` can call it
        once recursive subagents are actually needed (deep solver, phase 5+).
        """
        raise NotImplementedError("sub-worker spawning is deferred (leave the seam)")

    # --- internals ----------------------------------------------------------
    def _drain_inbox(self) -> list[str]:
        out, self._inbox = self._inbox, []
        return out

    def _collect_steers(self) -> list[str]:
        """Merge both steering paths: channel (Discord/console) + direct inbox."""
        return list(self.channel.drain_steer()) + self._drain_inbox()

    async def _post(self, content: str, *, force: bool = True) -> None:
        """Post on meaningful change (state change) or after min interval.

        Step posts pass force=True (each scripted step IS a meaningful change).
        The interval guard is for low-signal heartbeats so we don't spam.
        """
        now = time.monotonic()
        changed = self.chall.state != self._last_post_state
        if force or changed or (now - self._last_post_t) >= self._min_post_interval:
            await self.channel.post(content)
            self._last_post_t = now
            self._last_post_state = self.chall.state

    def _should_ask(self) -> bool:
        """Stall heuristic (tune later). Phase-1 arm: >=50% budget, no candidate.

        Wired here so phase 4's real loop inherits the trigger. Full heuristic:
        (no new finding in ~3-5 steps) OR (same approach retried twice) OR
        (>=50% budget spent with no candidate).
        """
        if self._asked or self._racing:
            return False
        return self.budget_used >= 0.5 and self.chall.state != "candidate"


class DummyWorker(Worker):
    """Phase-1 worker: walks the lane's scripted steps, no real solving.

    Lifecycle: walk steps -> emit a candidate flag -> HALT for validation. A
    flag does not end the worker; the operator's verdict does:
      - !solved   -> stand down (state=solved).
      - !continue -> the reason is folded as a steer, the worker re-opens and
                     re-derives (loops back), emitting a fresh candidate.
    Honors steer / pause / resume / race / kill throughout.
    """

    async def run(self) -> None:
        self.chall.state = "solving"
        await self._post(
            f"[start] **{self.name}** ({self.location}) on lane `{self.lane.name}` "
            f"[{self.lane.default_mode}] - starting"
        )

        script = self.lane.dummy_script
        start = 0
        round_no = 0
        while True:
            outcome = await self._walk(start)
            if outcome == "cancelled":
                self.chall.state = "killed"
                await self._post(f"[kill] **{self.name}** killed", force=True)
                return
            if outcome == "solved":
                await self._finish_solved()
                return

            # outcome == "done": emit a candidate flag and HALT for validation.
            # Arm the gate BEFORE announcing the flag, so a fast verdict can't
            # race the reset and deadlock the wait.
            self._validation.clear()
            self._validation_kind = None
            round_no += 1
            flag = f"CDDC{{dummy_{self.chall.id}_r{round_no}}}"
            self.chall.state = "candidate"
            await self.channel.post(
                summary(f"[done] **{self.name}** candidate (dummy)", self.findings, flag=flag)
            )
            if self._on_candidate is not None:
                await self._on_candidate(self, flag)  # orchestration halts the thread

            kind = await self._await_validation()
            if kind == "cancelled":
                self.chall.state = "killed"
                await self._post(f"[kill] **{self.name}** killed", force=True)
                return
            if kind == "solved":
                await self._finish_solved()
                return

            # kind == "continue": re-open and re-derive the last couple of steps.
            self.chall.state = "solving"
            start = max(0, len(script) - 2)

    async def _walk(self, start: int) -> str:
        """Walk scripted steps from `start`. Returns done|cancelled|solved."""
        script = self.lane.dummy_script
        n = max(len(script), 1)
        for i in range(start, len(script)):
            if self._cancelled:
                return "cancelled"
            if self._solved:
                return "solved"
            await self._resume.wait()  # blocks here while paused
            if self._cancelled:
                return "cancelled"
            if self._solved:
                return "solved"

            # (a) fold pending steers before the step, reflect in narration.
            for s in self._collect_steers():
                self.chall.steers.append(s)
                self.tried.append(f"steer:{s}")
                await self._post(f"[steer] adjusting for: {s}")

            # (b) advance one scripted step.
            self.current_step = i + 1
            self.budget_used = (i + 1) / n
            self.tried.append(script[i])
            self.findings.append(f"step {i+1}: {script[i]}")
            tag = "[race] racing" if self._racing else "-"
            await self._post(f"[{i+1}/{n}] {tag} {script[i]}")

            # decision point: stall -> HALT and hold for the operator. No
            # timeout - the [ask] post pings the team; we wait until !race /
            # !solo (or !kill). This is the miniature of phase-4 self-escalation.
            if self._should_ask():
                self._asked = True
                self._race_gate.clear()
                self._race_choice = None
                await self._post(
                    f"[ask] stuck after {i+1} steps on `{self.lane.name}` - race 3 "
                    f"subagents? `!race` to escalate, `!solo` to stay solo "
                    f"(holding until you decide)"
                )
                await self._race_gate.wait()  # holds here until a decision
                if self._cancelled:
                    return "cancelled"
                if self._race_choice == "race":
                    await self._post("[race] escalating to race (operator confirmed)")
                else:
                    await self._post("[cont] staying solo (operator)")

            await asyncio.sleep(self._tick)
        return "done"

    async def _await_validation(self) -> str:
        """Block until the operator resolves the candidate. Returns the verdict.

        The gate is armed (cleared) in run() before the flag is announced, so we
        only ever wait here - never reset, to avoid racing an early verdict.
        """
        await self._validation.wait()
        if self._cancelled:
            return "cancelled"
        return self._validation_kind or "continue"

    async def _finish_solved(self) -> None:
        self.chall.state = "solved"
        await self._post(f"[solved] **{self.name}** - flag confirmed, standing down", force=True)
