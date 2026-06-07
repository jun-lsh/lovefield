"""AgentWorker - a real LLM tool-loop, drop-in for DummyWorker.

It reuses the ENTIRE phase-1 control plane from Worker: steer-fold, pause/resume,
kill, the candidate-flag validation halt, and the #status pings. Only the
per-step logic changes - instead of walking a script, it drives a DeepSeek
tool-loop (run_shell / read_file / write_file / fetch_url / submit_flag) in the
challenge workdir.

Discord-agnostic: talks to a Channel and a ModelClient, nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import time

from .challenge import Challenge
from .channel import Channel
from .lanes.base import Lane
from .models import ModelClient, Reply, assistant_message
from .tools import Toolbox, tool_specs
from .worker import Worker, _fence, _md_escape, is_placeholder_flag, summary

# Per-agent alignment lives in markdown, NOT here - teammates edit cddc/skills/
# without touching Python. The system prompt is STACKED from four parts so the
# triage "move fast" bias never poisons a specialist (see skills/README.md):
#   common.md + env.md + roles/<role>.md + lanes/<lane>.md
SKILLS_DIR = pathlib.Path(__file__).parent / "skills"

# Operational heartbeat -> the bot's terminal (NOT Discord). Model-call + tool
# timing so the operator can tell "thinking hard" from "hung".
_log = logging.getLogger("cddc.agent")

# Fallback only (used if cddc/skills/ is somehow missing entirely).
SYSTEM_FALLBACK = """You are a {role} CTF agent for CDDC 2026 on the {lane} lane.
Recon first (list files, read them, classify the challenge), try the cheapest
win, and if you stall STOP and say so plainly. Use your tools. Verify before
submit_flag; never guess. Flags look like CDDC{{...}}."""


def _read(path: pathlib.Path) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def load_system(lane_name: str, role: str = "triage") -> str:
    """Compose the system prompt by stacking the skills/ markdown layers.

    common + env (universal/neutral) + role doctrine + lane playbook. Each
    layer is optional and degrades gracefully; if NONE are present we fall back
    to a minimal built-in string so the loop never breaks.
    """
    layers = [
        (_read(SKILLS_DIR / "common.md"), None),
        (_read(SKILLS_DIR / "env.md"), None),
        (_read(SKILLS_DIR / "roles" / f"{role}.md"), None),
        (_read(SKILLS_DIR / "lanes" / f"ctf-{lane_name}" / "SKILL.md"), f"# Lane playbook: {lane_name}"),
    ]
    parts: list[str] = []
    for body, header in layers:
        if body is None:
            continue
        parts.append(f"---\n{header}\n\n{body}" if header else body)
    if not parts:
        return SYSTEM_FALLBACK.format(lane=lane_name, role=role)
    parts.append(f"---\nYou are the **{role}** agent on the **{lane_name}** lane.")
    return "\n\n".join(parts)


# Tools always offered regardless of a lane's allowlist - they're handled in
# the agent (not the Toolbox), so every agent can submit a flag or escalate.
_ALWAYS_TOOLS = {"submit_flag", "triage_report", "solve_ready"}

# Broadly granted even on gated lanes: googling a CVE / version / attack name and
# reading a public writeup helps EVERY lane (unlike fetch_url, which is gated to
# live-target lanes). The worker still drops these from the advertised specs when
# no provider is configured (search) or always keeps read_url (Jina needs no key).
_BROAD_TOOLS = {"web_search", "read_url"}


def _specs_for_lane(lane: Lane) -> list[dict]:
    """Filter tool_specs() to the lane's allowed set.

    An empty `lane.tools` means "offer all" (back-compat for `raw` / unset lanes).
    `submit_flag` / `triage_report` and the broad web tools are always offered.
    """
    specs = tool_specs()
    if not lane.tools:
        return specs
    allowed = _ALWAYS_TOOLS | _BROAD_TOOLS
    return [
        s for s in specs
        if s["function"]["name"] in lane.tools or s["function"]["name"] in allowed
    ]


# How much of a single thought / tool result we post before a visible trim. The
# channel splits this across several Discord messages; this only stops a
# pathological dump from flooding the thread (the full text is always in !trace).
MAX_POST = 12000


def _brief(name: str, args: dict) -> str:
    """A one-line view of what a tool call is about to do. NOT truncated - the
    channel chunks long posts and !trace keeps the complete record; we want to
    SEE the full shell command, not a clipped fragment."""
    if name == "run_shell":
        return "$ " + str(args.get("command", "")).replace("\n", " ")
    if name == "write_file":
        return f"write {args.get('path', '?')} ({len(str(args.get('content', '')))} chars)"
    if name == "read_file":
        return f"read {args.get('path', '?')}"
    if name == "fetch_url":
        return f"fetch {args.get('url', '?')}"
    parts = [f"{k}={str(v)}".replace("\n", " ") for k, v in args.items()]
    return "(" + ", ".join(parts) + ")"


class AgentWorker(Worker):
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
        model: ModelClient,
        workdir: str,
        sandbox=None,
        max_steps: int,
        max_tokens: int,
        shell_timeout: int = 30,
        checkpoint_every: int = 8,
        role: str = "triage",
        searcher=None,
        reader=None,
    ) -> None:
        super().__init__(
            lane, chall, channel,
            id=id, name=name, location=location, operator=operator,
            on_candidate=on_candidate,
        )
        self.role = role
        self.model = model
        self.sandbox = sandbox
        self.toolbox = Toolbox(
            workdir, shell_timeout, skills_dir=str(SKILLS_DIR),
            searcher=searcher, reader=reader,
        )
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.checkpoint_every = checkpoint_every
        self._tokens = 0
        # The live OpenAI-format message log - exposed so !trace can dump it.
        self.messages: list[dict] = []

    def status(self) -> dict:
        s = super().status()
        s["tokens"] = self._tokens
        s["model"] = getattr(self.model, "model", "?")
        s["role"] = self.role
        return s

    async def run(self) -> None:
        self.chall.state = "solving"
        await self._post(
            f"[start] **{self.name}** ({self.location}) {self.role} agent on "
            f"`{self.lane.name}` - model {getattr(self.model, 'model', '?')}"
        )
        if self.sandbox is not None:
            try:
                await self.sandbox.start()
                sock = " + docker socket" if getattr(self.sandbox, "docker_sock", None) else ""
                await self._post(f"[sandbox] container `{self.sandbox.name}` up{sock}")
            except Exception as e:
                await self._post(f"[sandbox] failed to start: {e!r} - standing down", force=True)
                return await self._exit_killed()
        try:
            await self._run_loop()
        finally:
            if self.sandbox is not None:
                await self.sandbox.teardown()
                await self._post(f"[sandbox] container `{self.sandbox.name}` removed", force=True)

    async def _run_loop(self) -> None:
        files = ", ".join(os.path.basename(f) for f in self.chall.files) or "(none)"
        # self.messages is the live log (mutated in place below) so !trace can
        # read it mid-run; `messages` is just a local alias to keep run() tidy.
        messages = self.messages
        messages.extend([
            {"role": "system", "content": load_system(self.lane.name, self.role)},
            {
                "role": "user",
                "content": (
                    f"Challenge: {self.chall.name}\nCategory: {self.chall.category}\n"
                    f"Files in your workdir: {files}\n\nDescription:\n{self.chall.description}"
                ),
            },
        ])
        specs = _specs_for_lane(self.lane)
        # Drop web tools the host hasn't configured (no search provider -> no
        # web_search; reader is keyless so it normally stays).
        if self.toolbox.searcher is None:
            specs = [s for s in specs if s["function"]["name"] != "web_search"]
        if self.toolbox.reader is None:
            specs = [s for s in specs if s["function"]["name"] != "read_url"]
        budget_bonus = 0   # extra STEP runway granted by !continue at the cap
        token_bonus = 0    # extra TOKEN runway too - else a token-cap !continue re-halts at once

        while True:
            if self._cancelled:
                return await self._exit_killed()
            await self._resume.wait()  # blocks while paused
            if self._cancelled:
                return await self._exit_killed()
            if self._solved:  # !solved arrived mid-run
                return await self._exit_solved()

            # fold operator steers into the model's context
            for s in self._collect_steers():
                self.chall.steers.append(s)
                self.tried.append(f"steer:{s}")
                messages.append({"role": "user", "content": f"[operator steer] {s}"})
                await self._post(f"[steer] adjusting for: {s}")

            # budget guard - ask the human instead of silently burning money
            cap = self.max_steps + budget_bonus
            tok_cap = self.max_tokens + token_bonus
            if self.current_step >= cap or self._tokens >= tok_cap:
                # Triage's job is to SCOPE then make the call - so at the cap we
                # FORCE a triage_report (difficulty + recommendation), not just a
                # free-text summary. The operator then decides: !continue (let it
                # build the solve if it's simple), !escalate (hand off), or !kill.
                # BUT once the operator has greenlit solo_finish, it's in SOLVE
                # mode, not triage - don't keep re-forcing the same call, just a
                # plain budget halt.
                solving = self.chall.recommendation == "solo_finish"
                report = None if solving else await self._force_report()
                self.chall.state = "needs_human"
                if report:
                    self.chall.difficulty = report["difficulty"]
                    self.chall.technique = report["technique"]
                    self.chall.escalation_reason = report["blockers"]
                    self.chall.gist = report["gist"]
                    self.chall.recommendation = report["recommendation"]
                    self.chall.confidence = report["confidence"]
                    block = summary(
                        f"[budget] **{self.name}** hit its cap "
                        f"({self.current_step} steps, {self._tokens} tok) - forced triage call",
                        self.findings[-6:],
                        report=report,
                        menu="`!continue` (keep going - e.g. build the solve if it's "
                        "simple) | `!escalate` (hand off) | `!kill`",
                    )
                else:
                    rollup = await self._summarize_findings()
                    block = summary(
                        f"[budget] **{self.name}** hit its cap "
                        f"({self.current_step} steps, {self._tokens} tok)",
                        rollup or self.findings[-5:],
                        needs_human=True,
                        menu="`!continue` to keep churning | `!kill` to stop",
                    )
                kind = await self._halt(block)
                if kind == "cancelled":
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                budget_bonus += self.max_steps   # !continue grants more steps...
                token_bonus += self.max_tokens   # ...and more tokens, so it can actually churn on
                self.chall.state = "solving"
                messages.append({
                    "role": "user",
                    "content": "[operator] more runway granted - keep working. If it "
                    "is simple enough, build and run the solve yourself; otherwise "
                    "keep scoping and refine your triage_report.",
                })
                continue

            _log.info("%s step %d: calling model (%d tok so far)...",
                      self.name, self.current_step + 1, self._tokens)
            _t0 = time.monotonic()
            reply = await self._chat(messages, specs)
            if reply is None:  # model failed twice; operator resolved the halt
                continue
            _ncalls = len(reply.tool_calls)
            _log.info("%s step %d: model replied in %.1fs (+%d tok, %d tool call%s)",
                      self.name, self.current_step + 1, time.monotonic() - _t0,
                      reply.tokens, _ncalls, "" if _ncalls == 1 else "s")
            self._tokens += reply.tokens
            self.current_step += 1
            self.budget_used = round(min(1.0, self.current_step / max(cap, 1)), 2)
            messages.append(assistant_message(reply))

            if reply.content.strip():
                text = reply.content.strip()
                self.findings.append(text[:200])
                # Post the FULL thought - the channel chunks it across messages.
                # Escaped so stray markdown in the model's prose can't mangle the
                # thread. Only a pathological dump gets a visible trim (-> !trace).
                await self._post_long(f"[{self.current_step}]", _md_escape(text))

            # Periodic checkpoint: a consolidated rollup so the operator gets
            # signal, not just per-step noise. Template-only (no extra model
            # call / cost). 0 disables.
            if self.checkpoint_every and self.current_step % self.checkpoint_every == 0:
                await self._post(self._checkpoint(cap))

            if not reply.tool_calls:
                messages.append({
                    "role": "user",
                    "content": "Take the next concrete action with a tool, or submit_flag if you have it.",
                })
                continue

            submitted: str | None = None
            report: dict | None = None
            ready: dict | None = None
            for tc in reply.tool_calls:
                if tc.name == "submit_flag":
                    cand = str(tc.arguments.get("flag", "")).strip()
                    if not cand or is_placeholder_flag(cand):
                        # Reject empty / format-placeholder submits instead of
                        # halting the thread on a non-flag (same hygiene the
                        # harness applies to its .cddc_solution sentinel).
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": "that is not a real flag (empty or a "
                                         "format placeholder) - keep working and submit the "
                                         "actual verified flag."})
                        await self._post(f"[{self.current_step}] ignored non-flag submit: {cand!r}")
                    else:
                        submitted = cand
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": f"flag recorded: {submitted}"})
                    continue
                if tc.name == "triage_report":
                    report = {
                        "gist": str(tc.arguments.get("gist", "")).strip(),
                        "difficulty": int(tc.arguments.get("difficulty", 0) or 0),
                        "technique": str(tc.arguments.get("technique", "")).strip(),
                        "blockers": str(tc.arguments.get("blockers", "")).strip(),
                        "recommendation": str(tc.arguments.get("recommendation", "")).strip(),
                        "confidence": str(tc.arguments.get("confidence", "")).strip(),
                    }
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": "triage report filed - holding for an operator decision"})
                    continue
                if tc.name == "solve_ready":
                    ready = {
                        "summary": str(tc.arguments.get("summary", "")).strip(),
                        "needs": str(tc.arguments.get("needs", "")).strip(),
                    }
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": "local solve flagged - pinging the operator for the missing input"})
                    continue
                brief = _brief(tc.name, tc.arguments)
                self.tried.append(f"{tc.name}: {brief}")
                await self._post_long(f"[{self.current_step}] {tc.name}", _md_escape(brief))
                _log.info("%s step %d: %s %s ...", self.name, self.current_step, tc.name, brief[:80])
                _tt = time.monotonic()
                result = await self.toolbox.run(tc.name, tc.arguments)
                _log.info("%s step %d: %s -> %d chars in %.1fs",
                          self.name, self.current_step, tc.name, len(result or ""), time.monotonic() - _tt)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                # Post the RESULT too (it was previously fed only to the model). Tool
                # output is code/data -> CODE-FENCED so shell output, file contents,
                # and search results render monospace and never mangle the thread.
                await self._post_code(f"[{self.current_step}] {tc.name} ->", result or "(no output)")
                # Record a compact action->result line as a finding, so FINDINGS
                # reflects real work even when the model emits NO narration text
                # (common on tool-only / thinking-mode turns, where the reasoning
                # goes to reasoning_content which we drop). Without this the budget/
                # report summary shows "(none)" after a busy run.
                snippet = " ".join((result or "(no output)").split())[:180]
                self.findings.append(f"{brief} -> {snippet}")

            if submitted:
                kind = await self._halt(
                    summary(f"[done] **{self.name}** candidate", self.findings[-6:], flag=submitted),
                    candidate=True,
                )
                if kind == "cancelled":
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                # !continue: the reason was folded into the inbox; re-open.
                self.chall.state = "solving"
                messages.append({
                    "role": "user",
                    "content": "The operator rejected that flag. Re-examine and try again.",
                })

            if ready:
                kind = await self._halt_local_solve(ready)
                if kind == "cancelled":
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                # !continue: the operator hooked up the remote / supplied the
                # missing input (folded as a steer). Run the solve for real now.
                self.chall.state = "solving"
                messages.append({
                    "role": "user",
                    "content": "[operator] the missing input / remote is now provided "
                    "(see the steer). Run your working solve against it and submit_flag "
                    "the real flag.",
                })

            if report:
                kind = await self._halt_report(report)
                if kind == "cancelled":
                    # Operator approved an escalation: !escalate cancelled us and is
                    # respawning a specialist. Stand down quietly (respawn announces).
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                # !continue / !deny: the operator reviewed the report and wants the
                # agent to keep going (their note was folded in as a steer). Neutral
                # framing - NOT "rejected" (there was no candidate flag).
                self.chall.state = "solving"
                self.tried.append(f"report continued (d{report['difficulty']})")
                messages.append({
                    "role": "user",
                    "content": "[operator] reviewed your report - keep going. If your "
                    "recommendation was solo_finish, build and run the solve; otherwise "
                    "keep working and refine your read.",
                })

    async def _summarize_findings(self) -> list[str]:
        """One CHEAP model call - over the LOCAL logs (actions + the findings
        trail), NOT the full conversation - so the agent distills what it found,
        tried, and where it's stuck when it halts at the budget cap. Re-sending the
        whole message log would burn another big chunk right after hitting the cap;
        the compact trail is enough to write a useful operator summary. Falls back
        to [] (caller uses the raw findings) if the call fails.
        """
        actions = [t for t in self.tried if not t.startswith("steer:")][-12:]
        context = (
            f"Challenge: {self.chall.name} (lane {self.lane.name}).\n"
            f"Description: {(self.chall.description or '(none)')[:500]}\n"
            f"Actions taken: {'; '.join(actions) or '-'}\n"
            f"Tool results / notes so far:\n" + ("\n".join(self.findings[-12:]) or "-")
        )
        msgs = [
            {"role": "system", "content": "You summarize a CTF triage run for a human operator. Be concrete and terse."},
            {"role": "user", "content": context + "\n\nGive 3-6 bullet lines: what the "
             "challenge is, what you found and tried, and exactly where you are stuck "
             "or what you would try next. Bullets only, no preamble."},
        ]
        try:
            reply = await self.model.chat(msgs, [])
            self._tokens += reply.tokens
            lines = [ln.strip(" -*\t").strip() for ln in (reply.content or "").splitlines()]
            return [ln for ln in lines if ln]
        except Exception:
            return []

    async def _force_report(self) -> dict | None:
        """FORCE the difficulty call at the budget cap. One model call that MUST
        produce a triage_report (gist/difficulty/blockers/recommendation), built
        from the compact local logs (not the whole conversation). The triager's
        job is to SCOPE then make the call - this guarantees it, even if the cheap
        model kept wanting to grind. Returns the report dict, or None if it could
        not produce one (caller falls back to a free-text summary).
        """
        spec = [s for s in tool_specs() if s["function"]["name"] == "triage_report"]
        actions = [t for t in self.tried if not t.startswith("steer:")][-12:]
        context = (
            f"Challenge: {self.chall.name} (lane {self.lane.name}).\n"
            f"Description: {(self.chall.description or '(none)')[:500]}\n"
            f"Actions taken: {'; '.join(actions) or '-'}\n"
            f"Findings / results:\n" + ("\n".join(self.findings[-14:]) or "-")
        )
        msgs = [
            {"role": "system", "content": "You are a CTF triage agent that is OUT OF BUDGET. Make the difficulty call now."},
            {"role": "user", "content": context + "\n\nYou are out of triage budget. "
             "File your triage_report NOW: gist, difficulty 1-5, technique, blockers, "
             "and a recommendation - solo_finish if it is simple enough to just "
             "finish, else race / specialist / deep_solver / needs_human. Base it on "
             "what you found above."},
        ]
        reply = None
        for choice in ("required", None):  # try to force the tool, then fall back
            try:
                reply = await self.model.chat(msgs, spec, tool_choice=choice)
                break
            except Exception:
                reply = None
        if reply is None:
            return None
        self._tokens += reply.tokens
        for call in reply.tool_calls:
            if call.name == "triage_report":
                a = call.arguments
                return {
                    "gist": str(a.get("gist", "")).strip(),
                    "difficulty": int(a.get("difficulty", 0) or 0),
                    "technique": str(a.get("technique", "")).strip(),
                    "blockers": str(a.get("blockers", "")).strip(),
                    "recommendation": str(a.get("recommendation", "")).strip(),
                    "confidence": str(a.get("confidence", "")).strip(),
                }
        return None

    async def _await_decision(self) -> str:
        """Wait for the operator's verdict (!solved / !continue / !kill), but stay
        RESPONSIVE while halted: a !steer is treated as a question - the agent
        answers it from what it knows and keeps waiting. Asking does NOT consume the
        continue/kill decision, so the operator can interrogate before deciding.
        """
        while True:
            self._steer_event.clear()
            vt = asyncio.create_task(self._validation.wait())
            st = asyncio.create_task(self._steer_event.wait())
            try:
                await asyncio.wait({vt, st}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                vt.cancel()
                st.cancel()
            if self._cancelled:
                return "cancelled"
            if self._validation.is_set():
                return self._validation_kind or "continue"
            steers = self._collect_steers()
            if steers:
                await self._answer_while_halted(steers)

    async def _answer_while_halted(self, steers: list[str]) -> None:
        """Answer an operator question (asked via !steer while halted) from what the
        agent already knows - no tools, compact context. Stays halted afterward."""
        q = " ".join(s.strip() for s in steers if s.strip())
        if not q:
            return
        await self._post(f"[steer] {q}")
        actions = [t for t in self.tried if not t.startswith("steer:")][-12:]
        context = (
            f"Challenge: {self.chall.name} (lane {self.lane.name}).\n"
            f"What you've done: {'; '.join(actions) or '-'}\n"
            f"Findings so far:\n" + ("\n".join(self.findings[-12:]) or "-")
        )
        msgs = [
            {"role": "system", "content": "You are a paused CTF triage agent answering the operator's question. Answer concisely from what you already know; do not call tools."},
            {"role": "user", "content": context + f"\n\nOperator asks: {q}\n\nAnswer concisely."},
        ]
        try:
            reply = await self.model.chat(msgs, [])
            self._tokens += reply.tokens
            await self._post_long("[answer]", reply.content or "(no answer)")
        except Exception as e:
            await self._post(f"[answer] failed: {e!r}")

    def _checkpoint(self, cap: int) -> str:
        """Consolidated 'what's come out so far' rollup (no model call)."""
        actions = [t for t in self.tried if not t.startswith("steer:")]
        steers = [t[len("steer:"):] for t in self.tried if t.startswith("steer:")]
        lines = [
            f"[checkpoint] **{self.name}** step {self.current_step}/{cap} "
            f"- {self._tokens} tok",
            "  recent actions: " + (" | ".join(actions[-5:]) or "-"),
            "  latest findings: "
            + (" | ".join(f[:140] for f in self.findings[-3:]) or "-"),
        ]
        if steers:
            lines.append("  steers folded in: " + " | ".join(steers[-3:]))
        return "\n".join(lines)

    def trace_text(self) -> str:
        """Full message+tool trace - what `!trace` dumps to a file."""
        head = super().trace_text()
        out = [
            head,
            "",
            f"=== full message log ({len(self.messages)} msgs, "
            f"{self._tokens} tok) ===",
        ]
        for m in self.messages:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if role == "assistant":
                if content:
                    out.append(f"[assistant] {content}")
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    out.append(f"  -> call {fn.get('name')}({fn.get('arguments')})")
            elif role == "tool":
                out.append(f"[tool result] {content}")
            else:
                out.append(f"[{role}] {content}")
        return "\n".join(out)

    async def _chat(self, messages: list[dict], specs: list[dict]) -> Reply | None:
        """Call the model, retry once, then halt for a human on repeated failure."""
        try:
            return await self.model.chat(messages, specs)
        except Exception as e:
            await self._post(f"[error] model call failed: {e!r} - retrying once")
        try:
            return await self.model.chat(messages, specs)
        except Exception as e2:
            kind = await self._halt(
                summary(f"[error] **{self.name}** model failed: {e2!r}",
                        self.findings[-5:], needs_human=True)
            )
            if kind == "cancelled":
                self._cancelled = True
            return None

    async def _post_long(self, header: str, body: str) -> None:
        """Post `header body`, letting the channel chunk it across messages.

        The operator should see the full thought / full tool output, not a
        silently clipped fragment. Only a pathological dump is trimmed, and
        visibly - with the dropped-char count and a pointer to !trace.
        """
        body = (body or "").strip()
        if len(body) > MAX_POST:
            body = body[:MAX_POST] + f"\n...[+{len(body) - MAX_POST} chars - !trace for full]"
        await self._post(f"{header} {body}".rstrip())

    async def _post_code(self, header: str, body: str) -> None:
        """Post tool output as CODE-FENCED chunks - each chunk small enough to keep
        its own fences (so a long dump split across messages never breaks the
        fence). Trims a pathological dump, visibly, with a pointer to !trace."""
        body = (body or "(no output)").rstrip()
        if len(body) > MAX_POST:
            body = body[:MAX_POST] + f"\n...[+{len(body) - MAX_POST} chars - !trace for full]"
        await self._post(header)
        chunk = 1850
        for i in range(0, len(body), chunk):
            await self._post(_fence(body[i:i + chunk]))

    async def _halt_report(self, report: dict) -> str:
        """Record triage's read, post the TRIAGE REPORT (advice, not a decision),
        and HALT for the operator.

        Reuses the validation gate. Resolved by the bot: !escalate cancels us and
        respawns a specialist (-> 'cancelled'); !deny folds a note and re-opens us
        as triage (-> 'continue'). The recommendation is advisory - the operator
        picks; a `solo_finish` rec just means "!deny / let it grind".
        """
        self.chall.difficulty = report["difficulty"]
        self.chall.technique = report["technique"]
        self.chall.escalation_reason = report["blockers"]
        self.chall.gist = report["gist"]
        self.chall.recommendation = report["recommendation"]
        self.chall.confidence = report["confidence"]
        self._validation.clear()
        self._validation_kind = None
        self.chall.state = "needs_human"
        rec = report.get("recommendation") or ""
        if rec == "solo_finish":
            menu = "`!continue` (let it build/finish the solve) | `!kill`"
        else:
            menu = ("`!escalate` (specialist) | `!escalate deep` | `!escalate race [n]` | "
                    "`!continue` (keep it on triage)")
        await self.channel.post(
            summary(
                f"[triage] **{self.name}** filed a triage report",
                self.findings[-6:],
                report=report,
                menu=menu,
            )
        )
        return await self._await_decision()

    async def _halt_local_solve(self, ready: dict) -> str:
        """A WORKING local solve that's only missing an external piece (the remote
        target). Pings the operator with the SAME urgency as a flag (the "LOCAL
        SOLVE" marker -> 'big' severity in bot.py: @ping + #status), and halts so
        the operator can feed the target via !steer + !continue.
        """
        self._validation.clear()
        self._validation_kind = None
        self.chall.state = "needs_human"
        body = (
            f"{ready.get('summary') or '-'}\n"
            f"needs: {ready.get('needs') or 'the remote target / a missing external input'}"
        )
        block = "\n".join([
            f"[ready] **{self.name}** has a WORKING LOCAL SOLVE - blocked only on the remote",
            "",
            "**FINDINGS**",
            _fence("\n".join(f"- {f}" for f in self.findings[-6:]) or "(none)"),
            "",
            "**LOCAL SOLVE READY**",
            _fence(body),
            "",
            "your call: `!steer <target / details>` to feed it, then `!continue` once "
            "it's hooked up | `!kill`",
        ])
        await self.channel.post(block)
        return await self._await_decision()

    async def _halt(self, announcement: str, *, candidate: bool = False) -> str:
        """Announce + HALT for operator validation. Returns the verdict.

        Arms the gate BEFORE announcing so a fast verdict can't race the reset.
        """
        self._validation.clear()
        self._validation_kind = None
        if candidate:
            self.chall.state = "candidate"
        await self.channel.post(announcement)
        if candidate and self._on_candidate is not None:
            await self._on_candidate(self, "")
        return await self._await_decision()

    async def _exit_killed(self) -> None:
        self.chall.state = "killed"
        await self._post(f"[kill] **{self.name}** killed", force=True)

    async def _exit_solved(self) -> None:
        self.chall.state = "solved"
        await self._post(f"[solved] **{self.name}** - confirmed, standing down", force=True)
