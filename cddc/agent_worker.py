"""AgentWorker - a real LLM tool-loop, drop-in for DummyWorker.

It reuses the ENTIRE phase-1 control plane from Worker: steer-fold, pause/resume,
kill, the candidate-flag validation halt, and the #status pings. Only the
per-step logic changes - instead of walking a script, it drives a DeepSeek
tool-loop (run_shell / read_file / write_file / fetch_url / submit_flag) in the
challenge workdir.

Discord-agnostic: talks to a Channel and a ModelClient, nothing else.
"""

from __future__ import annotations

import os
import pathlib

from .challenge import Challenge
from .channel import Channel
from .lanes.base import Lane
from .models import ModelClient, Reply, assistant_message
from .tools import Toolbox, tool_specs
from .worker import Worker, summary

# Per-agent alignment lives in markdown, NOT here - teammates edit cddc/skills/
# without touching Python. The system prompt is STACKED from four parts so the
# triage "move fast" bias never poisons a specialist (see skills/README.md):
#   common.md + env.md + roles/<role>.md + lanes/<lane>.md
SKILLS_DIR = pathlib.Path(__file__).parent / "skills"

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
        (_read(SKILLS_DIR / "lanes" / f"{lane_name}.md"), f"# Lane playbook: {lane_name}"),
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


def _brief(args: dict) -> str:
    # Show enough to see what actually ran (commands especially). The channel
    # splits long posts into multiple messages, so we don't hard-truncate here.
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", " ")
        parts.append(f"{k}={s[:300]}")
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
        max_steps: int,
        max_tokens: int,
        shell_timeout: int = 30,
        checkpoint_every: int = 8,
        role: str = "triage",
    ) -> None:
        super().__init__(
            lane, chall, channel,
            id=id, name=name, location=location, operator=operator,
            on_candidate=on_candidate,
        )
        self.role = role
        self.model = model
        self.toolbox = Toolbox(workdir, shell_timeout)
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
        specs = tool_specs()
        budget_bonus = 0

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
            if self.current_step >= cap or self._tokens >= self.max_tokens:
                kind = await self._halt(
                    summary(
                        f"[budget] **{self.name}** hit its cap "
                        f"({self.current_step} steps, {self._tokens} tok)",
                        self.findings[-5:],
                        needs_human=True,
                    )
                )
                if kind == "cancelled":
                    return await self._exit_killed()
                if kind == "solved":
                    return await self._exit_solved()
                budget_bonus += self.max_steps  # !continue grants more runway
                self.chall.state = "solving"
                continue

            reply = await self._chat(messages, specs)
            if reply is None:  # model failed twice; operator resolved the halt
                continue
            self._tokens += reply.tokens
            self.current_step += 1
            self.budget_used = round(min(1.0, self.current_step / max(cap, 1)), 2)
            messages.append(assistant_message(reply))

            if reply.content.strip():
                text = reply.content.strip()
                self.findings.append(text[:200])
                # No hard truncation - DiscordChannel splits long posts into
                # multiple messages. Cap only to bound a pathological dump.
                await self._post(f"[{self.current_step}] {text[:8000]}")

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
            for tc in reply.tool_calls:
                if tc.name == "submit_flag":
                    submitted = str(tc.arguments.get("flag", "")).strip()
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": f"flag recorded: {submitted}"})
                    continue
                self.tried.append(f"{tc.name}{_brief(tc.arguments)}")
                await self._post(f"[{self.current_step}] {tc.name} {_brief(tc.arguments)}")
                result = await self.toolbox.run(tc.name, tc.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

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
