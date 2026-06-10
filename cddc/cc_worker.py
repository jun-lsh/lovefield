"""CCWorker - drive headless Claude Code (claude -p, JSON event stream) as a worker.

The clean sibling of HarnessWorker (tmux scraper, untouched): instead of capturing
a TUI and diffing the screen, it consumes the STRUCTURED event stream from
`headless.HeadlessClaude` - assistant prose, tool calls, the final result - and maps
them straight to Channel posts. No screen-scrape, no summarizer model needed.

One `claude -p` invocation is ONE autonomous turn (the agent runs its own tool loop
to completion). After each turn we HALT for the operator - which is also the natural
COST gate: nothing auto-loops, so an expensive tier can't run away. `!continue`
resumes the SAME session (`--resume <id>`); `!steer` resumes with the steer as the
next message; `!escalate`/`!kill` stand it down. The flag is DECLARED via the
.cddc_solution sentinel (authoritative), same as the tmux harness.

Per-TIER model is set entirely by the HeadlessClaude env profile the dispatcher
builds (DeepSeek flash/pro, or Opus 4.8 on the subscription) - this worker is
backend-agnostic. Discord-agnostic: talks to a Channel + a HeadlessClaude only.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

from .challenge import Challenge
from .channel import Channel
from .headless import KICKOFF, TRIAGE_KICKOFF
from .lanes.base import Lane
from .worker import FLAG_RE, Worker, _md_escape, declared_flag, summary

# Where the triage tier writes its assessment (read host-side off the workdir).
TRIAGE_REL = os.path.join(".cddc", "triage.md")
# The steer !triage injects to force any tier to stop and file a report.
FORCE_TRIAGE_STEER = (
    "STOP what you are doing. Do NOT keep solving. Write a triage assessment to "
    ".cddc/triage.md with labeled lines (gist, category, technique, difficulty 1-5, "
    "blockers, recommendation: solve_now|escalate|needs_human), then STOP."
)


class CCWorker(Worker):
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
        session,                      # headless.HeadlessClaude (or FakeHeadless)
        role: str = "specialist",
        decompiler_url: str = "",
        workdir: str = "",
        halt_on_flag: bool = True,
        flag_blacklist: list[str] | None = None,
        checkpoint_every: int = 8,
        turn_cap_secs: int = 0,
    ) -> None:
        super().__init__(
            lane, chall, channel,
            id=id, name=name, location=location, operator=operator,
            on_candidate=on_candidate,
        )
        self.session = session
        self.role = role
        self.decompiler_url = decompiler_url
        self.workdir = workdir
        self.halt_on_flag = halt_on_flag
        self.checkpoint_every = checkpoint_every
        # Backstop wall-clock cap on a SINGLE turn (0 = none). Used for the triage tier
        # so a flash turn that ignores "don't grind" still stops and halts for the
        # operator instead of running forever. Solver tiers run uncapped (operator
        # controls via !steer/!kill); a !continue resumes for another cap's worth.
        self.turn_cap_secs = turn_cap_secs
        self._flag_blacklist: set[str] = set(flag_blacklist or [])
        self._seen_flags: set[str] = set()
        self._candidates: list[str] = []
        self._turn = 0
        self._cost = 0.0
        self._tokens = 0
        self._last_triage = ""  # dedup the triage.md report across turns

    def status(self) -> dict:
        s = super().status()
        s["role"] = self.role
        s["model"] = "claude-code"
        s["candidates"] = list(self._candidates)
        s["turns"] = self._turn
        s["tokens"] = self._tokens
        s["cost_usd"] = round(self._cost, 4)
        return s

    # --- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        self.chall.state = "solving"
        await self._post(
            f"[start] **{self.name}** ({self.location}) {self.role} Claude Code on "
            f"`{self.lane.name}`"
        )
        self._write_box_config()  # .mcp.json + CLAUDE.md so MCP/doctrine load (headless + by hand)
        self._flag_blacklist.update(FLAG_RE.findall(self._initial_prompt()))
        try:
            await self._turn_loop()
        finally:
            await self.session.stop()
            await self._post(f"[cc] **{self.name}** session closed", force=True)

    def _write_box_config(self) -> None:
        """Drop .mcp.json (the decompiler MCP, with the FastMCP Host-header fix) and a
        CLAUDE.md doctrine into the box workdir, so BOTH headless turns and an operator
        running `docker exec -it <box> claude` by hand get MCP + the playbook."""
        if not self.workdir:
            return
        try:
            os.makedirs(self.workdir, exist_ok=True)
            if self.decompiler_url:
                port = urlparse(self.decompiler_url).port or 8000
                mcp = {"mcpServers": {"decompiler": {
                    "type": "http",
                    "url": self.decompiler_url,
                    # FastMCP rejects a container-name Host (DNS-rebind protection),
                    # so present an allowed one (the Host-header workaround).
                    "headers": {"Host": f"localhost:{port}"},
                }}}
                with open(os.path.join(self.workdir, ".mcp.json"), "w", encoding="utf-8") as f:
                    json.dump(mcp, f, indent=2)
            with open(os.path.join(self.workdir, "CLAUDE.md"), "w", encoding="utf-8") as f:
                f.write(self._claude_md())
        except OSError:
            pass

    def _claude_md(self) -> str:
        thread = self.chall.thread_id
        mcp_line = (
            f"- DECOMPILER (`decompiler` MCP, headless Ghidra + GolangAnalyzer): a SHARED "
            f"service in a SEPARATE container - it does NOT see your box's local files. It "
            f"reads the shared challenge mount, where your `/challenge/<file>` appears as "
            f"`/files/{thread}/<file>` (that path is ALSO visible in your box - "
            f"`ls /files/{thread}`). Call `import_binary` with `/files/{thread}/<file>` - "
            f"NEVER /challenge/... or /tmp/.... Then list/decompile functions through the "
            f"MCP by name (GolangAnalyzer gives `main.*` names for Go).\n"
            if self.decompiler_url else ""
        )
        tier = (
            "You are the **TRIAGE** tier: a FAST first look, NOT a solver. Assess the "
            "challenge, write .cddc/triage.md, and STOP - do not grind on a solution "
            "(the operator escalates to a stronger solver). "
            if self.role == "triage" else
            "You are a **SOLVER** tier: actually solve the challenge and declare the "
            "flag via .cddc_solution. "
        )
        return (
            f"# CTF challenge box - {self.lane.name} lane\n\n"
            f"{tier}This box has the full toolchain (compilers, debuggers, "
            "pwntools/angr, sage, decompilers, forensics, etc.).\n\n"
            "## Work fast, don't flail\n"
            f"- READ YOUR LANE PLAYBOOK FIRST: `/opt/cddc-skills/lanes/ctf-{self.lane.name}/"
            "SKILL.md` (+ the technique docs in that folder) and `/opt/cddc-skills/"
            "common.md` - curated, competition-tested approaches for this lane. Use them.\n"
            "- Then `ls -la` and read the ACTUAL challenge files. Never guess filenames.\n"
            "- This is JEOPARDY CTF: craft ONE precise approach from the files + "
            "description. Do NOT scan ports, sweep the network, brute-force, or fuzz - "
            "there is nothing to discover that way.\n"
            "- Connect to a remote ONLY if the description gives an explicit host:port. "
            "If it doesn't, there is no remote - do not go hunting for one.\n"
            "- If something is missing or odd (an expected file / remote / tool isn't "
            "there, output makes no sense), STOP and say so plainly, then end your "
            "turn. Do NOT rabbit-hole or invent busywork - the operator is watching and "
            "will steer you. Asking beats flailing.\n\n"
            "## Tools & flag\n"
            "- USE THE TOOLCHAIN - do NOT re-implement what's already here. Crypto attacks: "
            "`sage` / fpylll / sympy / `/opt/RsaCtfTool`, not hand-rolled number theory. "
            "Decompile: the `decompiler` MCP. A service: build + run its docker. Reach for "
            "the right installed tool first (and install one if it's missing).\n"
            "- This box has a DEEP toolchain (crypto: `sage`, sympy, pycryptodome, "
            "z3, fpylll, openssl, /opt/RsaCtfTool/RsaCtfTool.py; pwn: pwntools, angr, "
            "gdb+gef, pwninit; rev: the decompiler MCP, jadx, ilspycmd; etc.). Before "
            "you decide a tool is missing, run `command -v <tool>` and check the exact "
            "installed set in the per-area manifests: `cat /opt/cddc-*.txt` "
            "(recon/crypto/web/stego/pwn/rev/forens). Don't claim it can't be done here "
            "without checking.\n"
            "- Missing a tool? INSTALL it - `pip install <x>` / `uv pip install --system <x>`, "
            "`gem install <x>`, or `sudo apt-get update && sudo apt-get install -y <pkg>` "
            "(you have passwordless sudo). Don't get stuck on a missing package.\n"
            "- SEARCH THE WEB (WebSearch / WebFetch) for CVEs, library/CTF-tool versions, "
            "error strings, attack names, and writeups - a quick search beats guessing or "
            "rabbit-holing. (If a web tool ever errors as unsupported, say so and proceed.)\n"
            "- DECLARE the flag by writing ONLY the verified flag to `.cddc_solution` "
            "(`printf '%s' 'CDDC{...}' > .cddc_solution`). Never write a test / example "
            "/ placeholder flag there.\n"
            f"{mcp_line}"
            "- `docker` works (host socket): if the challenge ships a Dockerfile/"
            "compose, BUILD AND RUN it - that is its real environment. (`docker build` "
            "uses BuildKit/buildx; if a docker command errors, READ the message - it's "
            "usually a real error, not permissions.)\n"
            "- If `.cddc/dossier.md` exists, read it first and build on prior work.\n"
        )

    def _initial_prompt(self) -> str:
        files = ", ".join(os.path.basename(f) for f in self.chall.files) or "(none)"
        head = TRIAGE_KICKOFF if self.role == "triage" else KICKOFF
        return (
            f"{head}\n\n# Challenge: {self.chall.name}\nCategory: {self.chall.category}\n"
            f"Files in /challenge: {files}\n\n## Description\n{self.chall.description}"
        )

    def _read_triage(self) -> dict | None:
        """Parse the agent's .cddc/triage.md (labeled lines) into a report dict, or
        None if absent/unchanged since last turn. Robust to extra prose."""
        try:
            with open(os.path.join(self.workdir, TRIAGE_REL), encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            return None
        if not raw or raw == self._last_triage:
            return None
        self._last_triage = raw
        out: dict = {"_raw": raw}
        for line in raw.splitlines():
            k, sep, v = line.partition(":")
            if not sep:
                continue
            k, v = k.strip().lower(), v.strip()
            if k in ("gist", "category", "technique", "blockers", "recommendation"):
                out[k] = v
            elif k == "difficulty":
                m = re.search(r"[1-5]", v)
                if m:
                    out["difficulty"] = int(m.group())
        return out

    # --- the loop -----------------------------------------------------------
    async def _turn_loop(self) -> None:
        while True:
            if self._cancelled:
                return await self._exit_killed()
            await self._resume.wait()  # blocks while paused
            if self._cancelled:
                return await self._exit_killed()
            if self._solved:
                return await self._exit_solved()

            # Build this turn's prompt: turn 0 = the task brief (+ any pre-queued
            # handoff/operator notes); later turns = the steer, or a plain continue.
            steers = self._collect_steers()
            for s in steers:
                self.chall.steers.append(s)
                self.tried.append(f"steer:{s}")
            if self._turn == 0:
                prompt = self._initial_prompt()
                if steers:
                    prompt += "\n\n[handoff / operator notes]\n" + "\n".join(f"- {s}" for s in steers)
            elif steers:
                prompt = "Operator steer(s): " + " | ".join(steers)
            else:
                prompt = ("Continue working on the challenge. Overwrite .cddc_solution "
                          "only once you have the verified flag.")

            outcome = await self._run_one_turn(prompt)
            if self._cancelled:
                return await self._exit_killed()
            self._turn += 1
            if outcome == "interrupted":
                # Operator !steer mid-turn: don't halt - loop straight back and resume
                # the SAME session with the steer (drained by _collect_steers above).
                self.chall.state = "solving"
                continue
            cap = " (hit time cap)" if outcome == "timeout" else ""

            # After a turn, look for what the agent produced: a declared flag (sentinel)
            # and/or a triage report (.cddc/triage.md). Then HALT for the operator -
            # which is also the cost gate.
            declared = declared_flag(await self.session.read_solution())
            new_flag = bool(declared) and declared not in self._seen_flags
            if new_flag:
                self._seen_flags.add(declared)
                self._candidates.append(declared)
                self.findings.append(f"declared flag: {declared}")
            report = self._read_triage()
            if report:
                # Carry the triage read onto the challenge so !escalate's handoff +
                # the dossier inherit it.
                if report.get("difficulty"):
                    self.chall.difficulty = report["difficulty"]
                self.chall.technique = report.get("technique") or self.chall.technique
                self.chall.gist = report.get("gist") or self.chall.gist
                self.chall.recommendation = report.get("recommendation") or self.chall.recommendation
                self.chall.escalation_reason = report.get("blockers") or self.chall.escalation_reason
                self.findings.append(f"triage: {report.get('gist', '-')}")

            if new_flag:
                kind = await self._halt(
                    summary(f"[done] **{self.name}** declared a flag (turn {self._turn}){cap}",
                            self.findings[-6:], flag=declared),
                    candidate=True,
                )
            elif report:
                kind = await self._halt(summary(
                    f"[triage] **{self.name}** filed a triage report (turn {self._turn}){cap}",
                    self.findings[-5:],
                    report={"gist": report.get("gist"), "difficulty": report.get("difficulty", 0),
                            "technique": report.get("technique"), "blockers": report.get("blockers"),
                            "recommendation": report.get("recommendation"), "confidence": None},
                    menu="`!escalate` (hand to a solver) | `!continue` (solve it here) | `!kill`"))
            else:
                kind = await self._halt(summary(
                    f"[done] **{self.name}** finished turn {self._turn}{cap} - no flag, no report",
                    self.findings[-6:], needs_human=True,
                    menu="`!continue` (resume) | `!triage` (force a report) | `!escalate` | `!kill`"))

            if kind == "cancelled":
                return await self._exit_killed()
            if kind == "solved":
                return await self._exit_solved()
            self.chall.state = "solving"  # !continue -> loop, resuming the session

    async def _run_one_turn(self, prompt: str) -> str:
        """Run one `claude -p` turn (resumes the session if we have an id) and narrate
        its events live. Returns "completed" | "cancelled" | "interrupted" | "timeout":
          - cancelled: operator !kill -> stand down.
          - interrupted: operator !steer mid-turn -> stop, the caller resumes the same
            session with the steer (stop-and-steer).
          - timeout: the per-turn cap fired -> the caller halts for the operator."""
        t0 = time.monotonic()
        async for ev in self.session.run_turn(prompt):
            if self._cancelled:
                await self.session.stop()
                return "cancelled"
            if self._inbox:
                # A steer arrived mid-turn: interrupt cleanly and let the loop resume
                # the (same) session with it. session_id is preserved -> keeps context.
                await self.session.stop()
                await self._post("[cc] steer received - interrupting the turn to apply it")
                return "interrupted"
            if self.turn_cap_secs and (time.monotonic() - t0) > self.turn_cap_secs:
                await self.session.stop()
                await self._post(f"[cc] turn hit its {self.turn_cap_secs // 60}m cap - halting")
                return "timeout"
            if ev.kind == "idle":
                # No output for a while - prove we're alive (slow model vs hung CLI)
                # and update budget so !status moves. `!kill` if it sits here too long.
                secs = int(time.monotonic() - t0)
                self.budget_used = round(min(1.0, secs / 600.0), 2)
                await self._post(f"[cc] ...working ({secs}s, no output yet - `!kill` if stuck)")
                continue
            if ev.kind == "init":
                await self._post(f"[cc] session up ({ev.text})")
            elif ev.kind == "text":
                self.current_step += 1
                self.findings.append(ev.text[:200])
                await self._post(f"[cc] {_md_escape(ev.text)[:4000]}")
                if self.checkpoint_every and self.current_step % self.checkpoint_every == 0:
                    await self._post(self._checkpoint())
            elif ev.kind == "tool":
                self.tried.append(f"{ev.tool} {ev.tool_input[:80]}")
                await self._post(f"[cc] tool `{ev.tool}`: {ev.tool_input[:300]}")
            elif ev.kind == "tool_result":
                if ev.is_error:
                    await self._post(f"[cc] tool error: {ev.text[:300]}")
            elif ev.kind == "retry":
                await self._post(f"[cc] {ev.text}")
            elif ev.kind == "error":
                self.findings.append(f"error: {ev.text[:200]}")
                await self._post(f"[cc] error: {ev.text[:500]}", force=True)
            elif ev.kind == "result":
                self._cost += ev.cost_usd
                self._tokens += ev.tokens
                tail = f" - {_md_escape(ev.text)[:300]}" if ev.text else ""
                await self._post(f"[cc] turn done (${self._cost:.4f}, {ev.tokens} tok){tail}")

    # --- helpers (mirror HarnessWorker's control-plane bits) ----------------
    def _checkpoint(self) -> str:
        actions = [t for t in self.tried if not t.startswith("steer:")]
        lines = [
            f"[checkpoint] **{self.name}** turn {self._turn} step {self.current_step} "
            f"(${self._cost:.4f}, {self._tokens} tok)",
            "  recent: " + (" | ".join(a[:80] for a in actions[-4:]) or "-"),
        ]
        return "\n".join(lines)

    async def _halt(self, announcement: str, *, candidate: bool = False) -> str:
        """Announce + HALT for operator validation; returns the verdict. Arms the gate
        BEFORE announcing so a fast verdict can't race the reset (mirrors the others)."""
        self._validation.clear()
        self._validation_kind = None
        self._steer_event.clear()
        self.chall.state = "candidate" if candidate else "needs_human"
        await self.channel.post(announcement)
        if candidate and self._on_candidate is not None:
            await self._on_candidate(self, "")
        # Wake on a verdict (!solved / !continue / !kill) OR a bare !steer - so steering
        # works AT the halt too (resume + fold the steer in next loop), not only mid-turn.
        v = asyncio.create_task(self._validation.wait())
        s = asyncio.create_task(self._steer_event.wait())
        try:
            await asyncio.wait({v, s}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            v.cancel()
            s.cancel()
        if self._cancelled:
            return "cancelled"
        return self._validation_kind or "continue"

    async def _exit_killed(self) -> None:
        self.chall.state = "killed"
        await self._post(f"[kill] **{self.name}** killed", force=True)

    async def _exit_solved(self) -> None:
        self.chall.state = "solved"
        await self._post(f"[solved] **{self.name}** - confirmed, standing down", force=True)
