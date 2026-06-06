"""HarnessWorker - drive a real CLI coding agent (claude / codex) in tmux.

An alternative to `AgentWorker`: instead of our own model tool-loop, this watches
a full CLI harness (Claude Code / Codex) running in a `HarnessSession` (see
harness.py) and bridges it to the phase-1 control plane. It reuses the ENTIRE
Worker surface - steer-fold, pause/resume, kill, the #status posts - and only
changes the per-step logic: poll the agent's screen, post the new output, forward
operator steers as keystrokes, and announce candidate flags.

Candidate flags do NOT halt the agent by default: the prompt/skills text echoes
the flag FORMAT all over the screen, and the CLI keeps working after printing a
guess, so blocking on every match would stall constantly. Instead we filter out
known-fake flags (a maintained blacklist + the prompt's own placeholders) and
ANNOUNCE real ones while the agent keeps running. Flip halt_on_flag for the old
validation-halt behavior.

Discord-agnostic: talks to a Channel and a HarnessSession, nothing else.
"""

from __future__ import annotations

import asyncio
import re
import time

from .agent_worker import load_system
from .challenge import Challenge
from .channel import Channel
from .harness import HarnessSession
from .lanes.base import Lane
from .worker import Worker, summary

# A flag-shaped token printed anywhere on the agent's screen.
_FLAG_RE = re.compile(r"CDDC\{[^}\n]*\}")
# Always-fake tokens: the placeholder we (and the skills docs) use for "a flag".
_PLACEHOLDER_FLAGS = {"CDDC{...}", "CDDC{…}", "CDDC{FLAG}", "CDDC{flag}"}


class HarnessWorker(Worker):
    def __init__(
        self,
        lane: Lane,
        chall: Challenge,
        channel: Channel,
        *,
        id: str,
        name: str,
        location: str = "offsite",
        operator: str | None = None,
        on_candidate=None,
        session: HarnessSession,
        cli: str = "cli",
        max_minutes: float,
        poll_interval: float = 5.0,
        role: str = "triage",
        checkpoint_every: int = 8,
        halt_on_flag: bool = False,
        flag_blacklist: list[str] | None = None,
    ) -> None:
        super().__init__(
            lane, chall, channel,
            id=id, name=name, location=location, operator=operator,
            on_candidate=on_candidate,
        )
        self.session = session
        self.cli = cli
        self.role = role
        self.max_minutes = max_minutes
        self.poll_interval = poll_interval
        self.checkpoint_every = checkpoint_every
        # Candidate flags announce but don't block by default; flip to halt+validate.
        self.halt_on_flag = halt_on_flag
        self._last_capture = ""        # for tail-diffing the agent's screen
        self._seen_flags: set[str] = set()   # already-announced real flags (dedup)
        self._candidates: list[str] = []     # real flags found, in order
        # Maintained blacklist of fakes: built-in placeholders + operator-supplied
        # ones + (seeded in run()) the prompt's own example tokens.
        self._flag_blacklist: set[str] = set(_PLACEHOLDER_FLAGS) | set(flag_blacklist or [])
        self._cap_secs = max(1.0, max_minutes * 60.0)  # grows on !continue
        self._started_at = 0.0

    def status(self) -> dict:
        s = super().status()
        s["cli"] = self.cli
        s["role"] = self.role
        s["candidates"] = list(self._candidates)
        return s

    def trace_text(self) -> str:
        head = super().trace_text()
        return (
            f"{head}\n\n=== last screen capture ({self.cli}) ===\n"
            f"{self._last_capture or '(none)'}"
        )

    # --- the loop -----------------------------------------------------------
    async def run(self) -> None:
        self.chall.state = "solving"
        await self._post(
            f"[start] **{self.name}** ({self.location}) {self.role} harness on "
            f"`{self.lane.name}` - CLI `{self.cli}`"
        )
        prompt = self._initial_prompt()
        # The prompt + skills docs spell out the flag FORMAT (and may give example
        # flags); blacklist every flag-shaped token in them so we never announce
        # the agent's own instructions back as a candidate.
        self._flag_blacklist.update(_FLAG_RE.findall(prompt))
        try:
            await self.session.start(prompt)
            await self._post(f"[harness] `{self.cli}` session up - watching")
        except Exception as e:
            await self._post(f"[harness] failed to start: {e!r} - standing down", force=True)
            return await self._exit_killed()
        try:
            await self._watch_loop()
        finally:
            await self.session.stop()
            await self._post(f"[harness] `{self.cli}` session closed", force=True)

    def _initial_prompt(self) -> str:
        """The task brief handed to the CLI (stacked skills prompt + challenge)."""
        import os

        files = ", ".join(os.path.basename(f) for f in self.chall.files) or "(none)"
        return (
            f"{load_system(self.lane.name, self.role)}\n\n"
            f"---\n# Challenge: {self.chall.name}\nCategory: {self.chall.category}\n"
            f"Files in your working directory: {files}\n\n"
            f"## Description\n{self.chall.description}\n"
        )

    async def _watch_loop(self) -> None:
        self._started_at = time.monotonic()
        while True:
            if self._cancelled:
                return await self._exit_killed()
            await self._resume.wait()  # blocks while paused
            if self._cancelled:
                return await self._exit_killed()
            if self._solved:  # !solved arrived mid-run
                return await self._exit_solved()

            # fold operator steers into the live agent as keystrokes
            for s in self._collect_steers():
                self.chall.steers.append(s)
                self.tried.append(f"steer:{s}")
                await self.session.send(s)
                await self._post(f"[steer] forwarded to `{self.cli}`: {s}")

            # wall-clock budget guard - ask the human instead of running forever
            elapsed = time.monotonic() - self._started_at
            self.budget_used = round(min(1.0, elapsed / self._cap_secs), 2)
            if elapsed >= self._cap_secs:
                kind = await self._halt(
                    summary(
                        f"[budget] **{self.name}** hit its "
                        f"{self._cap_secs / 60:.0f}m wall-clock cap",
                        self.findings[-5:],
                        needs_human=True,
                    )
                )
                if kind == "cancelled":
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                self._cap_secs += max(1.0, self.max_minutes * 60.0)  # !continue grants more
                self.chall.state = "solving"
                continue

            # capture the agent's screen, post only the new tail
            new = self._delta(await self.session.capture())
            if new.strip():
                self.current_step += 1
                self.findings.append(new.strip()[:200])
                # No hard truncation - the channel splits long posts. Cap only to
                # bound a pathological dump.
                await self._post(f"[{self.cli}] {new.strip()[:8000]}")
                if self.checkpoint_every and self.current_step % self.checkpoint_every == 0:
                    await self._post(self._checkpoint())

                flag = self._find_flag(new)
                if flag:
                    self._candidates.append(flag)
                    self.findings.append(f"candidate flag: {flag}")
                    if self.halt_on_flag:
                        # Opt-in classic behavior: HALT and wait for validation.
                        kind = await self._halt(
                            summary(f"[done] **{self.name}** candidate",
                                    self.findings[-6:], flag=flag),
                            candidate=True,
                        )
                        if kind == "cancelled":
                            return await self._exit_killed()
                        if kind == "solved":
                            return await self._exit_solved()
                        # !continue: reason folded as a steer; tell the agent.
                        self.chall.state = "solving"
                        await self.session.send(
                            f"The operator rejected the flag {flag}. Re-examine and keep going."
                        )
                    else:
                        # Default: announce (pings #status via the CANDIDATE FLAG
                        # block) but keep the agent running. Operator stands it down
                        # with !solved / !kill if it's the real one.
                        await self.channel.post(summary(
                            f"[candidate] **{self.name}** found a flag "
                            f"(still running - `!solved` to confirm, `!kill` to stop)",
                            self.findings[-6:], flag=flag,
                        ))

            # the CLI exited on its own (finished, crashed, or hit its own cap)
            if not await self.session.alive():
                kind = await self._halt(
                    summary(
                        f"[done] **{self.name}** - `{self.cli}` exited without a "
                        f"confirmed flag",
                        self.findings[-6:],
                        needs_human=True,
                    )
                )
                if kind == "cancelled":
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                # Nothing left to resume - the process is gone. Stand down.
                await self._post(
                    f"[harness] `{self.cli}` already exited; standing down", force=True
                )
                return await self._exit_killed()

            await asyncio.sleep(self.poll_interval)

    # --- helpers ------------------------------------------------------------
    def _delta(self, text: str) -> str:
        """The new tail since the last capture (append-mostly scrollback)."""
        if text == self._last_capture:
            return ""
        if self._last_capture and text.startswith(self._last_capture):
            new = text[len(self._last_capture):]
        else:
            new = text  # a redraw/reset - repost the whole screen
        self._last_capture = text
        return new

    def _find_flag(self, text: str) -> str | None:
        for m in _FLAG_RE.finditer(text):
            flag = m.group(0)
            inner = flag[len("CDDC{"):-1]
            if not inner.strip(" .\t…"):       # empty / ellipsis placeholder
                continue
            if flag in self._flag_blacklist or flag in self._seen_flags:
                continue
            self._seen_flags.add(flag)
            return flag
        return None

    def _checkpoint(self) -> str:
        """Consolidated 'what's come out so far' rollup (no extra cost)."""
        actions = [t for t in self.tried if not t.startswith("steer:")]
        steers = [t[len("steer:"):] for t in self.tried if t.startswith("steer:")]
        lines = [
            f"[checkpoint] **{self.name}** step {self.current_step} "
            f"- budget {self.budget_used} via `{self.cli}`",
            "  latest findings: "
            + (" | ".join(f[:140] for f in self.findings[-3:]) or "-"),
        ]
        if steers:
            lines.append("  steers folded in: " + " | ".join(steers[-3:]))
        return "\n".join(lines)

    async def _halt(self, announcement: str, *, candidate: bool = False) -> str:
        """Announce + HALT for operator validation. Returns the verdict.

        Arms the gate BEFORE announcing so a fast verdict can't race the reset.
        Mirrors AgentWorker._halt.
        """
        self._validation.clear()
        self._validation_kind = None
        if candidate:
            self.chall.state = "candidate"
        await self.channel.post(announcement)
        if candidate and self._on_candidate is not None:
            await self._on_candidate(self, "")
        await self._validation.wait()
        if self._cancelled:
            return "cancelled"
        return self._validation_kind or "continue"

    async def _exit_killed(self) -> None:
        self.chall.state = "killed"
        await self._post(f"[kill] **{self.name}** killed", force=True)

    async def _exit_solved(self) -> None:
        self.chall.state = "solved"
        await self._post(f"[solved] **{self.name}** - confirmed, standing down", force=True)
