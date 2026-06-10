"""Headless Claude Code session - drive `claude -p` and parse its JSON event stream.

The clean alternative to the tmux screen-scraper (harness.py, untouched): instead of
capturing a TUI and diffing the screen, we run the CLI in HEADLESS mode

    claude -p "<task>" --output-format stream-json --verbose --dangerously-skip-permissions

over `docker exec` into the per-challenge box, and read its newline-delimited JSON
events (system/init, assistant, user, result) straight off stdout - structured, no
scraping, no summarizer model needed. Multi-turn (operator steers) is `--resume
<session_id>`; we capture the id from the stream.

Per-TIER model is just a per-SPAWN env profile (see cc_worker / dispatcher):
  - DeepSeek tiers point ANTHROPIC_BASE_URL at https://api.deepseek.com/anthropic
    (DeepSeek's official Anthropic-compatible endpoint) with ANTHROPIC_MODEL =
    deepseek-v4-flash (triage) or deepseek-v4-pro (specialist);
  - the deep tier sets NO DeepSeek env, so the CLI uses the mounted subscription
    login -> Opus 4.8.
Secrets (the DeepSeek key as ANTHROPIC_AUTH_TOKEN) are passed via an inherited
`-e VAR` flag whose VALUE rides in the spawn's OWN env, never on the argv.

The box is left usable by hand too: `docker exec -it <box> claude` gets the same
MCP servers / skills / env, so an operator can drive it standalone (no Discord).

Discord-agnostic; stdlib + asyncio only. `FakeHeadless` lets simulate.py exercise
the worker with no docker / no CLI / no key / no cost.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

# Reuse the flag-declaration sentinel + home helper from the tmux harness so both
# harnesses agree on the success signal (importing these does NOT pull in libtmux,
# which harness.py loads lazily).
from .harness import SOLUTION_FILE, container_home

# Task kickoff handed to the CLI as the -p prompt. The flag is DECLARED by writing
# it to the sentinel (read host-side off the bind-mounted workdir), NOT by printing
# it - the agent prints test/example flags too.
KICKOFF = (
    "Solve the CTF challenge in your working directory (/challenge). "
    "START by running `ls -la` there and reading the ACTUAL files - do NOT guess "
    "filenames. This is jeopardy: do NOT scan ports or sweep the network, and connect "
    "to a remote ONLY if the description gives an explicit host:port. If something is "
    "missing or looks wrong, STOP and report it - do not flail. Otherwise use your "
    "tools (shell, the decompiler MCP, web search) and any docker services the "
    "challenge ships. If a prior worker left .cddc/dossier.md, "
    "read it first and build on it. To DECLARE the flag: once you have VERIFIED the "
    "real one, write ONLY that flag to .cddc_solution in your working directory "
    "(e.g. `printf '%s' 'CDDC{...}' > .cddc_solution`). Never write a test, example, "
    "or placeholder flag there. Then stop."
)

# Triage-tier task: a FAST assessment, NOT a solve. The thin-triage doctrine for the
# cheap (flash) tier - it writes a structured report to .cddc/triage.md and stops, so
# the operator can escalate to a real solver instead of letting flash grind.
TRIAGE_KICKOFF = (
    "You are TRIAGE - a FAST, cheap first look, NOT a full solve. Run `ls -la` in "
    "/challenge and read the files + the description. Work out the category, the likely "
    "technique/vulnerability, and how hard it is. Do NOT grind on a solution or run long "
    "tools. Write your assessment to .cddc/triage.md with these labeled lines (one each, "
    "exactly these keys):\n"
    "gist: <one line - what the challenge is>\n"
    "category: <pwn|rev|crypto|web|forensics|misc|ai|hardware>\n"
    "technique: <the likely attack / technique>\n"
    "difficulty: <1-5>\n"
    "blockers: <what a solver needs, or why it is hard>\n"
    "recommendation: <solve_now | escalate | needs_human>\n"
    "Then STOP. Do NOT scan/brute/fuzz. ONLY if it is trivially solvable in 1-2 steps, "
    "solve it and write the flag to .cddc_solution instead. If something is missing or "
    "odd, say so in blockers and stop."
)


@dataclass
class Event:
    """One parsed item off the stream-json output - the worker maps these to posts."""

    kind: str            # init | text | tool | tool_result | result | retry | error | idle
    text: str = ""       # assistant prose / result text / error message
    tool: str = ""       # tool name (kind == "tool")
    tool_input: str = ""  # short rendering of the tool input
    is_error: bool = False
    session_id: str = ""
    cost_usd: float = 0.0
    tokens: int = 0


def _short(obj, n: int = 200) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def events_from_line(line: str) -> list[Event]:
    """Parse one stream-json line into zero or more Events. Never raises."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    sid = str(obj.get("session_id", "") or "")

    if t == "system":
        sub = obj.get("subtype")
        if sub == "init":
            model = obj.get("model", "?")
            mcp = [m.get("name") for m in (obj.get("mcp_servers") or []) if isinstance(m, dict)]
            return [Event(kind="init", session_id=sid,
                          text=f"model={model}" + (f" mcp={mcp}" if mcp else ""))]
        if sub == "api_retry":
            return [Event(kind="retry", session_id=sid,
                          text=f"api retry {obj.get('attempt', '?')}/{obj.get('max_retries', '?')} "
                               f"({obj.get('error', '?')})")]
        return []

    if t == "assistant":
        out: list[Event] = []
        for block in ((obj.get("message") or {}).get("content") or []):
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text" and block.get("text", "").strip():
                out.append(Event(kind="text", text=block["text"].strip(), session_id=sid))
            elif bt == "tool_use":
                out.append(Event(kind="tool", session_id=sid,
                                 tool=str(block.get("name", "?")),
                                 tool_input=_short(block.get("input", {}))))
        return out

    if t == "user":
        out = []
        for block in ((obj.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                out.append(Event(kind="tool_result", text=_short(content), session_id=sid,
                                 is_error=bool(block.get("is_error"))))
        return out

    if t == "result":
        usage = obj.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        return [Event(kind="result", session_id=sid,
                      text=str(obj.get("result", "") or ""),
                      is_error=bool(obj.get("is_error")),
                      cost_usd=float(obj.get("total_cost_usd", 0.0) or 0.0),
                      tokens=tokens)]

    return []


class HeadlessClaude:
    """A headless `claude -p` session against the per-challenge box (one turn at a
    time; steer/continue via --resume). Mirrors the lifecycle the worker needs:
    run_turn() (async-iter events), read_solution(), stop()."""

    def __init__(
        self,
        sandbox,
        workdir: str,
        *,
        model: str = "",
        env_profile: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        user: str = "",
        mcp_config: str = "",
        append_system_prompt: str = "",
        mount: str = "/challenge",
        cli: str = "claude",
        extra_args: list[str] | None = None,
        heartbeat: float = 30.0,
    ) -> None:
        self.sandbox = sandbox
        self.workdir = workdir
        self.model = model
        # Emit a synthetic kind="idle" Event after this many seconds of silence, so a
        # long/slow turn (or a hung CLI) shows a heartbeat instead of looking frozen.
        self.heartbeat = heartbeat
        # Non-secret env -> `-e K=V` on the argv (base url, model ids, effort level).
        self.env_profile = dict(env_profile or {})
        # Secret env -> `-e K` on the argv, VALUE supplied via the spawn's own env
        # (never on argv / never in `ps`). e.g. {"ANTHROPIC_AUTH_TOKEN": <deepseek key>}.
        self.secret_env = dict(secret_env or {})
        self.user = user
        self.mcp_config = mcp_config  # in-container path or inline JSON for --mcp-config
        self.append_system_prompt = append_system_prompt
        self.mount = mount
        self.cli = cli
        self.extra_args = list(extra_args or [])
        self.session_id = ""
        self.last_result = ""
        self.last_cost = 0.0
        self.last_is_error = False
        self._proc: asyncio.subprocess.Process | None = None
        self._ready = False  # box started + user aligned once before the first turn

    def _argv(self, prompt: str, *, resume: str) -> list[str]:
        # NOTE: -w "/" not the workdir. `docker exec -w <bind-mount>` trips runc's
        # CVE-2024-21626 "cwd outside mount namespace root" guard on Docker Desktop/WSL,
        # so we exec at / and chdir into the workdir INSIDE a shell (a normal chdir, not
        # runc's pre-exec check). The CLI argv rides "$@", so the multi-line prompt needs
        # no shell quoting.
        argv = ["docker", "exec", "-w", "/"]
        for k, v in self.env_profile.items():
            argv += ["-e", f"{k}={v}"]
        for k in self.secret_env:
            argv += ["-e", k]  # value rides in the spawn env, not here
        argv += ["-e", f"HOME={container_home(self.user)}"]
        if self.user:
            argv += ["-u", self.user]
        cli = [
            self.cli, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.model:
            cli += ["--model", self.model]
        if resume:
            cli += ["--resume", resume]
        if self.mcp_config:
            cli += ["--mcp-config", self.mcp_config]
        if self.append_system_prompt:
            cli += ["--append-system-prompt", self.append_system_prompt]
        cli += self.extra_args
        return argv + [self.sandbox.name, "sh", "-c", f"cd {self.mount} && exec \"$@\"", "cc", *cli]

    async def _ensure_ready(self):
        """Once before the first turn: bring the (shared, persistent) box up and align
        the run-as user's uid to the host's, so `claude` - which refuses to run as
        root under --dangerously-skip-permissions - can read the mounted subscription
        creds and WRITE the bind-mounted /challenge. Returns an error string or ""."""
        if self._ready:
            return ""
        self._ready = True
        try:
            await self.sandbox.start()  # idempotent: reuse a live box or create one (#12)
        except Exception as e:
            return f"box failed to start: {e!r}"
        # uid alignment (posix host only; no-op for root / the sim's FakeHeadless).
        if self.user and hasattr(os, "getuid"):
            uid, gid = os.getuid(), os.getgid()  # type: ignore[attr-defined]
            home = container_home(self.user)
            # Docker-out-of-docker: the bound host socket is root:root 0660, so the
            # non-root agent user can't reach it -> chmod it world-usable. (Loosens the
            # host socket's perms; fine for a single-user solve box.)
            sock = getattr(self.sandbox, "docker_sock", "") or ""
            sock_fix = f"chmod 0666 {sock} 2>/dev/null; " if sock else ""
            await self.sandbox.exec(
                sock_fix +
                f"groupmod -o -g {gid} {self.user} 2>/dev/null; "
                f"usermod -o -u {uid} -g {self.user} {self.user} 2>/dev/null; "
                f"chown {uid}:{gid} {home} 2>/dev/null; "
                f"chown -R {uid}:{gid} {home}/.local {home}/.cache {home}/.npm "
                f"{home}/.claude {home}/.codex 2>/dev/null; true",
                30,
            )
        return ""

    async def run_turn(self, prompt: str):
        """Run one turn (resuming the session if we have an id) and async-yield its
        Events as they stream. Updates session_id / last_result. Never raises - a
        spawn/parse failure surfaces as a final kind='error' Event."""
        err = await self._ensure_ready()
        if err:
            yield Event(kind="error", text=err, is_error=True)
            return
        env = {**os.environ, **self.secret_env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv(prompt, resume=self.session_id),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=16 * 1024 * 1024,  # big tool results (decompiled fns) overflow the 64KB default
            )
        except Exception as e:  # docker missing / exec failed to spawn
            yield Event(kind="error", text=f"failed to launch claude in box: {e!r}", is_error=True)
            return

        assert self._proc.stdout is not None
        stdout = self._proc.stdout
        saw_result = False
        tail: list[str] = []  # recent non-JSON stdout lines - claude prints errors HERE, not stderr
        while True:
            try:
                raw = await asyncio.wait_for(stdout.readline(), timeout=self.heartbeat)
            except asyncio.TimeoutError:
                yield Event(kind="idle")  # alive, just no output yet -> worker heartbeats
                continue
            except Exception as e:  # oversized line / reader error - don't spin silently
                yield Event(kind="error", text=f"stream read error: {e!r}", is_error=True)
                break
            if not raw:
                break  # EOF: the CLI finished streaming
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            evs = events_from_line(line)
            if not evs:
                tail.append(line)  # not a JSON event -> keep it for error context
                del tail[:-12]
                continue
            for ev in evs:
                if ev.session_id:
                    self.session_id = ev.session_id
                if ev.kind == "result":
                    saw_result = True
                    self.last_result, self.last_cost, self.last_is_error = (
                        ev.text, ev.cost_usd, ev.is_error,
                    )
                yield ev

        await self._proc.wait()
        rc = self._proc.returncode or 0
        if rc != 0 and not saw_result:
            err = b""
            try:
                if self._proc.stderr is not None:
                    err = await self._proc.stderr.read()
            except Exception:
                pass
            # claude usually prints the real reason to stdout (non-JSON), so fall back to
            # the captured stdout tail when stderr is empty.
            detail = err.decode("utf-8", "replace").strip() or " | ".join(tail)
            yield Event(kind="error", is_error=True,
                        text=f"claude exited rc={rc}: {_short(detail, 500) or '(no output)'}")
        self._proc = None

    async def read_solution(self) -> str:
        """The flag the agent DECLARED via the sentinel, or "" - read host-side off
        the bind-mounted workdir (robust vs scraping CDDC{...} out of the stream)."""
        try:
            with open(os.path.join(self.workdir, SOLUTION_FILE), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    async def stop(self) -> None:
        """Kill the in-flight turn (operator !kill / handoff). Best-effort."""
        p = self._proc
        if p is not None and p.returncode is None:
            try:
                p.kill()
                await p.wait()
            except Exception:
                pass
        self._proc = None


class FakeHeadless:
    """Scripted HeadlessClaude for the token-free sim: yields canned Events per turn
    and 'declares' a flag by returning a preset solution. No docker / CLI / key."""

    def __init__(self, turns: list[list[Event]], *, solution: str = "") -> None:
        self._turns = [list(t) for t in turns]
        self._solution = solution
        self.session_id = "fake-session"
        self.last_result = ""
        self.last_cost = 0.0
        self.last_is_error = False
        self.started = False
        self.stopped = False
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str):
        self.started = True
        self.prompts.append(prompt)
        evs = self._turns.pop(0) if self._turns else []
        for ev in evs:
            if ev.kind == "result":
                self.last_result, self.last_cost, self.last_is_error = (
                    ev.text, ev.cost_usd, ev.is_error,
                )
            yield ev

    async def read_solution(self) -> str:
        return self._solution

    async def stop(self) -> None:
        self.stopped = True
