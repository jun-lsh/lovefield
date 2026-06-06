"""Harness sessions - drive a real CLI coding agent (claude / codex) in tmux.

The alternative to the API tool-loop (`agent_worker.py`): instead of our own
loop over `models.py` + `tools.py`, run the full `claude` (Claude Code) or
`codex` CLI - it brings its own planning, tool use, and file editing. The CLI
runs INSIDE the per-challenge sandbox container; libtmux drives it from the HOST
through `docker exec`, so we can capture its screen and feed it steers.

`HarnessSession` is the seam (mirrors `models.ModelClient`): `TmuxHarness` is the
real libtmux+docker impl, `FakeHarness` scripts pane output so `simulate.py` can
exercise the worker loop with no docker / no tmux / no key / no cost.

Discord-agnostic. `libtmux` is imported lazily (only TmuxHarness needs it), so
the token-free sim imports this module without the lib installed.
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class HarnessSession(Protocol):
    """A running CLI agent the worker can watch and nudge.

    All methods are async so the real (subprocess/libtmux) impl never blocks the
    event loop. `capture()` returns the agent's screen text so far (append-mostly
    - the worker diffs it to post only the new tail).
    """

    async def start(self, prompt: str) -> None: ...
    async def capture(self) -> str: ...
    async def send(self, text: str) -> None: ...
    async def alive(self) -> bool: ...
    async def stop(self) -> None: ...


# Env vars passed THROUGH to the container (no value = inherit from the bot's
# env, so the key never lands on the docker argv). claude reads ANTHROPIC_API_KEY,
# codex reads OPENAI_API_KEY; passing both is harmless.
_PASSTHROUGH_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

# Sent to the CLI once it has booted - points it at the task file we drop in the
# workdir (avoids a giant, quoting-fragile send-keys of the whole prompt).
_KICKOFF = "Read .cddc_task.md and solve the challenge. When you find the flag, print it on its own line."

# Host credential FILES to share into the container so the CLIs are already
# logged in. We mount individual files, not the whole ~/.claude / ~/.codex dirs,
# so we don't mask anything the native CLI install keeps in those dirs.
#   claude: OAuth token in ~/.claude/.credentials.json; account/onboarding in
#           ~/.claude.json.  codex: ~/.codex/auth.json.
_CRED_PATHS = (".claude/.credentials.json", ".claude.json", ".codex/auth.json")


def container_home(user: str) -> str:
    """Home dir of the container user the CLI runs as ("" / root -> /root)."""
    return "/root" if not user or user == "root" else f"/home/{user}"


def credential_mounts(user: str) -> list[str]:
    """`-v` specs sharing the host's claude/codex login into the container.

    Only existing host paths are mounted (docker would otherwise create empty
    dirs that mask the CLI's own config). Mapped to the container user's home and
    relabeled `:z` so SELinux lets the container read them. Read-write so the CLIs
    can refresh OAuth tokens. The files are 0600 owned by the host uid;
    TmuxHarness._align_user() retags the container user to that uid at runtime so
    it can read them (root needs no alignment).
    """
    host_home = os.path.expanduser("~")
    ctr_home = container_home(user)
    specs = []
    for rel in _CRED_PATHS:
        src = os.path.join(host_home, rel)
        if os.path.exists(src):
            specs.append(f"{os.path.abspath(src)}:{ctr_home}/{rel}:z")
    return specs


class TmuxHarness:
    """Real session: a libtmux window on the HOST running `docker exec <cli>`.

    Owns the whole lifecycle - it starts the sandbox container, drops the task
    prompt into the bind-mounted workdir, opens a host tmux session whose window
    runs the CLI inside the container, and tears both down on stop().
    """

    def __init__(
        self,
        cli: str,
        sandbox,
        workdir: str,
        *,
        launch_cmd: str,
        session_name: str,
        mount: str = "/challenge",
        user: str = "",
        boot_delay: float = 6.0,
        history: int = 3000,
        kickoff: str = _KICKOFF,
        startup_keys: list[str] | None = None,
    ) -> None:
        self.cli = cli
        self.sandbox = sandbox
        self.workdir = workdir
        self.launch_cmd = launch_cmd
        self.session_name = session_name
        self.mount = mount
        self.user = user
        self.boot_delay = boot_delay
        self.history = history
        self.kickoff = kickoff
        # Key names (tmux send-keys) to clear the CLI's startup gate before the
        # kickoff. claude's `--dangerously-skip-permissions` warning highlights
        # "No, exit" by default, so ["Down", "Enter"] selects "Yes, I accept".
        self.startup_keys = ["Down", "Enter"] if startup_keys is None else list(startup_keys)
        self._server = None
        self._session = None
        self._pane = None

    def _docker_cmd(self) -> str:
        """The shell command tmux runs in its window: docker exec into the box."""
        parts = ["docker", "exec", "-it", "-w", self.mount]
        for var in _PASSTHROUGH_ENV:
            parts += ["-e", var]
        # Pin HOME so the CLI finds the credentials we mounted at the user's home
        # (docker exec -u does NOT set HOME on its own).
        parts += ["-e", f"HOME={container_home(self.user)}"]
        if self.user:
            parts += ["-u", self.user]
        parts += [self.sandbox.name]
        # launch_cmd is a ready-to-run shell string (e.g. "claude --foo"); append
        # it verbatim so operators can pass flags without us re-quoting.
        return " ".join(parts) + " " + self.launch_cmd

    async def _align_user(self) -> None:
        """Make the container user's uid/gid match THIS host user's.

        The shared credential files are 0600 owned by the host uid, so the CLI
        (running as `self.user`) can only read them if its uid matches. We adjust
        the user at runtime (read from os.getuid()) rather than baking a uid into
        the image - so it adapts to whoever runs the bot, and survives the base
        image's default uid-1000 `ubuntu` user (`-o` allows the duplicate). The
        native CLI install + caches + config dirs (~/.local, ~/.cache, ~/.npm,
        ~/.claude, ~/.codex) are created at build under the image's uid and must be
        owned by the new uid to run/self-update and to let the CLI write its state,
        so we chown them recursively. That recursion does touch the bind-mounted
        credential files, but it's a no-op there: we set them to the SAME uid they
        already have (the host owner == this uid), so the host files are unchanged.
        Root needs no alignment - uid 0 reads any file.
        """
        if not self.user:
            return
        uid, gid = os.getuid(), os.getgid()
        home = container_home(self.user)
        await self.sandbox.exec(
            f"groupmod -o -g {gid} {self.user} 2>/dev/null; "
            f"usermod -o -u {uid} -g {self.user} {self.user} 2>/dev/null; "
            f"chown {uid}:{gid} {home} 2>/dev/null; "
            f"chown -R {uid}:{gid} {home}/.local {home}/.cache {home}/.npm "
            f"{home}/.claude {home}/.codex 2>/dev/null; "
            f"true",
            30,
        )

    async def start(self, prompt: str) -> None:
        import libtmux  # lazy: sim / FakeHarness don't need it

        await self.sandbox.start()  # docker run -d ... sleep infinity
        await self._align_user()    # so `self.user` can read the mounted creds
        # Task prompt lands in the bind-mounted workdir -> visible in-container.
        os.makedirs(self.workdir, exist_ok=True)
        with open(os.path.join(self.workdir, ".cddc_task.md"), "w", encoding="utf-8") as f:
            f.write(prompt)

        def _spawn():
            server = libtmux.Server()
            if server.has_session(self.session_name):
                server.kill_session(self.session_name)
            session = server.new_session(
                session_name=self.session_name,
                attach=False,
                window_command=self._docker_cmd(),
            )
            # Keep a dead pane around so alive() can observe the CLI exiting,
            # instead of the window vanishing out from under us.
            try:
                session.set_option("remain-on-exit", "on")
            except Exception:
                pass
            pane = session.windows[0].panes[0]
            return server, session, pane

        self._server, self._session, self._pane = await asyncio.to_thread(_spawn)
        await asyncio.sleep(self.boot_delay)  # let the TUI come up
        # Clear the startup gate (e.g. the bypass-permissions warning: Down -> "Yes,
        # I accept" -> Enter) so the kickoff lands in the chat input, not the modal.
        for key in self.startup_keys:
            await self._key(key)
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.5)
        await self.send(self.kickoff)

    async def capture(self) -> str:
        if self._pane is None:
            return ""

        def _cap():
            # -S -<history> pulls scrollback so output accumulates append-mostly,
            # which is what the worker's tail-diff expects.
            out = self._pane.cmd("capture-pane", "-p", "-S", f"-{self.history}").stdout
            return "\n".join(out)

        try:
            return await asyncio.to_thread(_cap)
        except Exception:
            return ""

    async def _key(self, name: str) -> None:
        """Send a single named key (tmux key name, e.g. 'Enter', 'Down')."""
        if self._pane is None:
            return
        await asyncio.to_thread(
            lambda: self._pane.send_keys(name, enter=False, literal=False, suppress_history=False)
        )

    async def _enter(self) -> None:
        await self._key("Enter")

    async def send(self, text: str) -> None:
        if self._pane is None:
            return
        # Type the line LITERALLY (so tmux doesn't interpret tokens like ';' or
        # 'Enter', and doesn't prepend a history-suppression space), then submit
        # with a separate Enter after a beat - TUIs often only register the line
        # if Enter arrives after the text has settled.
        await asyncio.to_thread(
            lambda: self._pane.send_keys(text, enter=False, literal=True, suppress_history=False)
        )
        await asyncio.sleep(0.4)
        await self._enter()

    async def alive(self) -> bool:
        if self._server is None or self._pane is None:
            return False

        def _alive():
            if not self._server.has_session(self.session_name):
                return False
            dead = self._pane.cmd("display-message", "-p", "#{pane_dead}").stdout
            return not (dead and dead[0].strip() == "1")

        try:
            return await asyncio.to_thread(_alive)
        except Exception:
            return False

    async def stop(self) -> None:
        def _kill():
            try:
                if self._server is not None and self._server.has_session(self.session_name):
                    self._server.kill_session(self.session_name)
            except Exception:
                pass

        await asyncio.to_thread(_kill)
        await self.sandbox.teardown()


class FakeHarness:
    """Scripted session for the sim: pops a canned screen-capture per poll.

    `captures` are CUMULATIVE pane snapshots (append-mostly), mirroring real
    scrollback - the worker tail-diffs them. `sent` records every steer/kickoff
    the worker pushes, so the sim can assert steering reached the agent. No
    docker, no tmux, no key.
    """

    def __init__(self, captures: list[str], *, stay_alive: bool = True) -> None:
        self._captures = list(captures)
        self._last = self._captures[-1] if self._captures else ""
        self._stay_alive = stay_alive
        self.sent: list[str] = []
        self.started = False
        self.stopped = False

    async def start(self, prompt: str) -> None:
        self.started = True
        self.start_prompt = prompt

    async def capture(self) -> str:
        if self._captures:
            self._last = self._captures.pop(0)
        return self._last

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def alive(self) -> bool:
        # Alive while there's scripted output left, then honor stay_alive (so a
        # test can drive the worker to a candidate halt without the agent exiting).
        return bool(self._captures) or self._stay_alive

    async def stop(self) -> None:
        self.stopped = True
