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

from .challenge import Challenge
from .channel import Channel
from .lanes.base import Lane
from .models import ModelClient, Reply, assistant_message
from .tools import Toolbox, tool_specs
from .worker import Worker, summary

SYSTEM = """You are an autonomous CTF-solving agent on the {lane} lane for CDDC 2026.

Doctrine:
- Bias to action: try the cheapest plausible approach immediately, observe, adjust. Short loops, no long silent planning.
- Use your tools. run_shell runs in the challenge workdir and python is available. read_file/write_file/fetch_url as needed.
- Narrate concisely in between tool calls - an operator is watching the thread and may steer you.
- Verify before you submit. When you are confident you have the flag, call submit_flag exactly once. Never submit a guess.
- If you stall (a few tries with no traction), say so plainly - the operator can redirect you.

Flags are usually formatted CDDC{{...}} unless the challenge states otherwise."""


def _specs_for_lane(lane: Lane) -> list[dict]:
    """Filter tool_specs() to the lane's allowed set.

    An empty `lane.tools` means "offer all" (back-compat for `raw` / unset lanes).
    `submit_flag` is always offered - it is handled in the agent, not the toolbox.
    """
    specs = tool_specs()
    if not lane.tools:
        return specs
    return [
        s for s in specs
        if s["function"]["name"] in lane.tools or s["function"]["name"] == "submit_flag"
    ]


def _brief(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", " ")
        parts.append(f"{k}={s[:60]}")
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
    ) -> None:
        super().__init__(
            lane, chall, channel,
            id=id, name=name, location=location, operator=operator,
            on_candidate=on_candidate,
        )
        self.model = model
        self.sandbox = sandbox
        self.toolbox = Toolbox(workdir, shell_timeout, sandbox=sandbox)
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self._tokens = 0

    def status(self) -> dict:
        s = super().status()
        s["tokens"] = self._tokens
        s["model"] = getattr(self.model, "model", "?")
        return s

    async def run(self) -> None:
        self.chall.state = "solving"
        await self._post(
            f"[start] **{self.name}** ({self.location}) agent on `{self.lane.name}` "
            f"- model {getattr(self.model, 'model', '?')}"
        )
        if self.sandbox is not None:
            try:
                await self.sandbox.start()
                await self._post(f"[sandbox] container `{self.sandbox.name}` up")
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
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM.format(lane=self.lane.name)},
            {
                "role": "user",
                "content": (
                    f"Challenge: {self.chall.name}\nCategory: {self.chall.category}\n"
                    f"Files in your workdir: {files}\n\nDescription:\n{self.chall.description}"
                ),
            },
        ]
        specs = _specs_for_lane(self.lane)
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
                self.findings.append(reply.content.strip()[:200])
                await self._post(f"[{self.current_step}] {reply.content.strip()[:1500]}")

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
