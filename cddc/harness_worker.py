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
from .worker import FLAG_RE, Worker, declared_flag, extract_flag, summary

# Flag validation (what counts as a real flag) lives in worker.py so AgentWorker
# and HarnessWorker agree - see worker.extract_flag / is_placeholder_flag.

# --- TUI cleanup: turn a raw claude/codex screen capture into readable text ---
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX_CHARS = set("─│╭╮╰╯┌┐└┘├┤┬┴┼━┃┏┓┗┛═║╔╗╚╝▌▐█▏▕▶▲▼◀◆●○•✻✶✽✢·»❯⏵⏺⎿◢◣◤◥")
# Whole lines that are pure TUI chrome (status bar / banner / spinner) - dropped.
_CHROME_RE = re.compile(
    r"claude code v\d|welcome back|bypass permissions|esc to interrupt|"
    r"esc to cancel|enter to confirm|shift\+tab|tips for getting|for shortcuts|"
    r"churning|thinking…|[↑↓] .*tokens",
    re.IGNORECASE,
)


def _clean(raw: str) -> str:
    """Reduce a TUI screen capture to readable text.

    Strips ANSI escapes + box-drawing glyphs, drops status-bar/banner/spinner
    chrome, and collapses blank runs + adjacent duplicate lines. Best-effort -
    the bigger win is the (model) summarizer that will sit on top; this makes the
    raw stream legible and gives that summarizer clean input.
    """
    out: list[str] = []
    for line in _ANSI_RE.sub("", raw or "").splitlines():
        line = "".join(ch for ch in line if ch not in _BOX_CHARS).strip()
        if not line:
            if out and out[-1] == "":
                continue
            out.append("")
            continue
        if _CHROME_RE.search(line):
            continue
        if out and out[-1] == line:  # dedup adjacent identical lines
            continue
        out.append(line)
    return "\n".join(out).strip()


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
        summarizer=None,
        summarize_every: float = 20.0,
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
        # Optional cheap ModelClient that turns the raw TUI output into a clean
        # 1-line Discord update (the cheap model narrating the deep agent). None
        # -> post the cleaned screen deltas directly (no composition).
        self.summarizer = summarizer
        self.summarize_every = summarize_every
        self._sum_buf: list[str] = []   # cleaned output awaiting summarization
        self._last_summary = 0.0        # monotonic ts of last summary emit
        self._summary_tokens = 0
        self._last_capture = ""        # for tail-diffing the agent's screen
        self._seen_flags: set[str] = set()   # declared flags already announced (dedup)
        self._candidates: list[str] = []     # declared flags, in order
        self._spotted: set[str] = set()      # unconfirmed on-screen tokens (trace only)
        # Operator-supplied fakes + (seeded in run()) the prompt's own example
        # tokens. Format placeholders are handled by worker.is_placeholder_flag,
        # so they don't need to be listed here.
        self._flag_blacklist: set[str] = set(flag_blacklist or [])
        self._cap_secs = max(1.0, max_minutes * 60.0)  # grows on !continue
        self._started_at = 0.0

    def status(self) -> dict:
        s = super().status()
        s["cli"] = self.cli
        s["role"] = self.role
        s["candidates"] = list(self._candidates)
        if self.summarizer is not None:
            s["summary_tokens"] = self._summary_tokens
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
        self._flag_blacklist.update(FLAG_RE.findall(prompt))
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

            # capture the agent's screen, clean off TUI chrome
            new = _clean(self._delta(await self.session.capture()))
            if new:
                self.current_step += 1
                self.findings.append(new[:200])
                # On-screen tokens are UNCONFIRMED - the agent prints test/example
                # flags too (the canary false-positive). Record for !trace only;
                # never announce. The sentinel file is the sole authoritative path.
                tok = self._extract_flag(new, skip_seen=False)
                if tok and tok not in self._spotted and tok not in self._seen_flags:
                    self._spotted.add(tok)
                    self.findings.append(f"unconfirmed token on screen: {tok}")
                if self.summarizer is not None:
                    self._sum_buf.append(new)  # composed into a line on the cadence below
                else:
                    # No summarizer: post the cleaned delta directly. Channel splits
                    # long posts; cap only to bound a pathological dump.
                    await self._post(f"[{self.cli}] {new[:4000]}")
                    if self.checkpoint_every and self.current_step % self.checkpoint_every == 0:
                        await self._post(self._checkpoint())

            # cadence: have the cheap model compose ONE clean line from the buffer
            if self.summarizer is not None and self._sum_buf:
                if time.monotonic() - self._last_summary >= self.summarize_every:
                    await self._emit_summary()

            # AUTHORITATIVE flag declaration: the .cddc_solution sentinel the agent
            # writes once it has VERIFIED the real flag (see harness._KICKOFF). The
            # agent declared it explicitly, so we trust ANY prefix (NCO26{...} etc.)
            # via declared_flag, not the CDDC-shaped screen scrape.
            declared = declared_flag(await self.session.read_solution())
            if declared and declared not in self._seen_flags:
                self._seen_flags.add(declared)
                self._candidates.append(declared)
                self.findings.append(f"declared flag: {declared}")
                if self.halt_on_flag:
                    # Opt-in classic behavior: HALT and wait for validation.
                    kind = await self._halt(
                        summary(f"[done] **{self.name}** declared a flag",
                                self.findings[-6:], flag=declared),
                        candidate=True,
                    )
                    if kind == "cancelled":
                        return await self._exit_killed()
                    if kind == "solved":
                        return await self._exit_solved()
                    # !continue: tell the agent to overwrite the sentinel when right.
                    self.chall.state = "solving"
                    await self.session.send(
                        f"The operator rejected {declared}. Keep going; overwrite "
                        f".cddc_solution only once you have the correct flag."
                    )
                else:
                    # Default: announce (pings #status via the CANDIDATE FLAG block)
                    # but keep the agent running. Operator stands it down with
                    # !solved / !kill if it's the real one.
                    await self.channel.post(summary(
                        f"[candidate] **{self.name}** declared a flag "
                        f"(still running - `!solved` to confirm, `!kill` to stop)",
                        self.findings[-6:], flag=declared,
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
    async def _emit_summary(self) -> None:
        """Compose the buffered raw output into ONE clean Discord line and post it."""
        buf = "\n".join(self._sum_buf).strip()
        self._sum_buf = []
        self._last_summary = time.monotonic()
        if not buf:
            return
        line = await self._summarize(buf)
        if line:
            await self._post(f"[{self.cli}] {line}")

    async def _summarize(self, text: str) -> str:
        """Cheap model turns raw agent terminal output into a 1-line status.

        The cheap churn model narrating the expensive CLI agent. Best-effort: on
        any error fall back to the last cleaned line so the feed is never silent.
        Never raises into the watch loop.
        """
        sys_prompt = (
            "You narrate a CTF solving agent's progress for a team Discord feed. "
            "Given the agent's recent terminal output, reply with ONE concise line "
            "(<=200 chars, present tense): what it is doing, what it found, or where "
            "it is stuck. No preamble, no markdown headers. If nothing meaningful "
            "happened, reply exactly: working."
        )
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text[-6000:]},
        ]
        try:
            reply = await self.summarizer.chat(msgs, [])
            self._summary_tokens += getattr(reply, "tokens", 0) or 0
            return (reply.content or "").strip().replace("\n", " ")[:280]
        except Exception as e:
            await self._post(f"[summarizer] failed: {e!r} (posting raw tail)")
            tail = [ln for ln in text.splitlines() if ln.strip()]
            return tail[-1][:280] if tail else ""

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

    def _extract_flag(self, text: str, *, skip_seen: bool = True) -> str | None:
        """First REAL flag in `text` via the shared worker.extract_flag.

        Does NOT mark it seen - callers decide: the authoritative sentinel path
        records it in _seen_flags + _candidates; the unconfirmed on-screen path
        tracks it in _spotted instead (trace only, never announced).
        """
        return extract_flag(
            text,
            blacklist=self._flag_blacklist,
            seen=self._seen_flags if skip_seen else (),
        )

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
