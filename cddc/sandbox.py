"""Sandbox - run untrusted shell in a per-challenge `ctf-sandbox` container.

The host workdir is bind-mounted into the container, so file IO done host-side by
the Toolbox (read_file/write_file) and shell run in-container both see the same
files. Only `run_shell` is routed here - executing untrusted binaries is the real
risk; reading/writing bytes on the shared mount is not.

Mirrors the Toolbox contract: `exec` NEVER raises - it returns a string (the raw
combined output, or a "tool error: ..." message). The Toolbox truncates it to a
digest, same as the local path.

Discord-agnostic; stdlib only. Pure ASCII.
"""

from __future__ import annotations

import asyncio


class Sandbox:
    """A per-CHALLENGE docker container, SHARED by every worker on the challenge.

    Decoupled from the worker lifecycle (task #12): the box is created once (by the
    first worker that runs) and PERSISTS across handoffs - escalation, lane reroute,
    and race fan-out all reuse the SAME live container, so a fresh brain takes over
    the box (installed tools, running services, scratch files, analysis) with zero
    teardown. It is destroyed only at challenge end, via release(); ownership lives
    in the Registry (the bot releases it on !kill / !solved).

    start()    -> reuse a running box / restart a stopped one / create one. Idempotent
                  and concurrency-safe (race fan-out calls it N times for one box).
    exec()     -> docker exec ... sh -c <command>    (one tool call)
    teardown() -> NO-OP by contract: a worker let go, but the box persists for the
                  next worker. Kept so harness.py's stop() needs no change.
    release()  -> docker rm -f + reap sibling services. The real teardown.
    """

    def __init__(
        self,
        image: str,
        thread_id,
        host_workdir: str,
        *,
        mount: str = "/challenge",
        extra_mounts: list[str] | None = None,
        docker_sock: str | None = None,
        mount_flag: str = "",
        gpu: bool = False,
        network: str = "",
        decompiler_url: str = "",
    ) -> None:
        self.image = image
        self.thread_id = thread_id
        self.host_workdir = host_workdir
        self.mount = mount
        # SELinux relabel flag for the workdir bind-mount ("z"/"Z"); empty = none.
        # Needed only on SELinux-enforcing hosts; a no-op (and a footgun) elsewhere.
        self.mount_flag = (mount_flag or "").strip().lstrip(":")
        # Expose the host GPU (docker --gpus all) - for the ai lane's CUDA torch.
        self.gpu = gpu
        # Docker network to join (so the agent can reach the shared decompiler
        # service by name) + the decompiler MCP URL passed in for the `dc` client.
        self.network = (network or "").strip()
        self.decompiler_url = (decompiler_url or "").strip()
        # Extra `-v` specs appended to `docker run` (e.g. host credential files
        # for the harness CLIs). Each is a full "src:dst[:opts]" string.
        self.extra_mounts = list(extra_mounts or [])
        # Host docker socket to bind in (docker-OUT-of-docker). When set, the
        # agent inside can drive the HOST daemon - anything it `docker run`s is a
        # SIBLING of this sandbox, not a child, so a service it stands up survives
        # this sandbox's teardown and is reachable by a handed-off worker. The
        # flip side: those siblings leak unless we reap them (see teardown), so we
        # scope them by the COMPOSE_PROJECT_NAME / label below. Gated to non-triage
        # roles by the dispatcher - mounting the socket is host-root, not casual.
        self.docker_sock = docker_sock
        self.name = f"cddc-{thread_id}"
        # Label/compose scope for sibling containers the agent spins via the socket.
        self.scope = f"cddc-{thread_id}"
        self._started = False
        # Serialize concurrent start() calls: a race fan-out hands the SAME box
        # object to N workers that each call start() at once - only one should
        # create the container, the rest reuse it.
        self._start_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Ensure the per-challenge box is up, REUSING it if it already is.

        Idempotent and concurrency-safe (race fan-out calls this N times for ONE
        box): a running container is reused as-is, a stopped one (daemon restart /
        crash, or a prior session) is restarted so its state survives, and only a
        missing one is created fresh. This reuse is what lets a new worker take over
        a live box on handoff - the box is no longer recreated per worker (#12)."""
        import os

        async with self._start_lock:
            if await self._running():
                self._started = True  # reuse: a prior worker already has it up
                return
            if await self._exists():
                # Present but stopped - restart to recover its filesystem/state
                # rather than throwing it away.
                rc, _ = await self._docker("start", self.name)
                if rc == 0:
                    self._started = True
                    return
                await self._docker("rm", "-f", self.name)  # unrecoverable -> recreate
            os.makedirs(self.host_workdir, exist_ok=True)
            host_abs = os.path.abspath(self.host_workdir)
            rc, out = await self._docker(*self._run_argv(host_abs))
            if rc != 0:
                # Leave _started False; exec() will return a clear tool error.
                raise RuntimeError(f"docker run failed (rc={rc}): {out.strip()[:500]}")
            self._started = True

    async def _running(self) -> bool:
        """True if a container named self.name exists AND is running."""
        rc, out = await self._docker("inspect", "-f", "{{.State.Running}}", self.name)
        return rc == 0 and out.strip() == "true"

    async def _exists(self) -> bool:
        """True if a container named self.name exists (running or stopped)."""
        rc, _ = await self._docker("inspect", "-f", "{{.State.Status}}", self.name)
        return rc == 0

    def _run_argv(self, host_abs: str) -> list[str]:
        """Build the `docker run` argv. Split out so the sim can assert on it
        (mounts/socket) without actually launching a container."""
        workdir_spec = f"{host_abs}:{self.mount}"
        if self.mount_flag:
            workdir_spec += f":{self.mount_flag}"
        mount_args: list[str] = ["-v", workdir_spec]
        for spec in self.extra_mounts:
            mount_args += ["-v", spec]
        # CDDC_THREAD is always exported: the `dc` decompiler client needs it to map
        # a workdir-relative path to the service's /files/<thread> view, and manual
        # `docker run`s use it for the cddc.thread label.
        env_args: list[str] = ["-e", f"CDDC_THREAD={self.thread_id}"]
        if self.decompiler_url:
            env_args += ["-e", f"CDDC_DECOMPILER_URL={self.decompiler_url}"]
        sock_args: list[str] = []
        if self.docker_sock:
            # Bind the host daemon socket in, and pre-seed COMPOSE_PROJECT_NAME so any
            # `docker compose up` the agent runs is scoped to this challenge and
            # reapable at release (challenge end). (Manual `docker run`s should carry
            # `--label cddc.thread=<id>`; CDDC_THREAD above is exported for that.)
            sock_args = [
                "-v", f"{self.docker_sock}:{self.docker_sock}",
                "-e", f"COMPOSE_PROJECT_NAME={self.scope}",
            ]
        gpu_args = ["--gpus", "all"] if self.gpu else []
        net_args = ["--network", self.network] if self.network else []
        # No --rm: the box is a persistent per-challenge resource (#12). It must
        # survive an accidental stop / daemon blip so start() can restart it and
        # recover state; it's removed only by release() at challenge end.
        return [
            "run", "-d",
            "--name", self.name,
            "-w", self.mount,
            *mount_args,
            *env_args,
            *sock_args,
            *gpu_args,
            *net_args,
            self.image,
            "sleep", "infinity",
        ]

    async def exec(self, command: str, timeout: int) -> str:
        """Run `command` via `sh -c` inside the container. Never raises."""
        if not self._started:
            return "tool error: sandbox container not started"
        if not command.strip():
            return "(empty command)"
        try:
            # -w "/" not self.mount: `docker exec -w <bind-mount>` trips runc's
            # CVE-2024-21626 "cwd outside mount namespace" guard on Docker Desktop/WSL.
            # We exec at / and `cd` into the workdir in-shell (a normal chdir).
            rc, out = await self._docker(
                "exec", "-w", "/", self.name,
                "sh", "-c", f"cd {self.mount} 2>/dev/null; {command}",
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"(command timed out after {timeout}s)"
        except Exception as e:  # never propagate into the agent loop
            return f"tool error: {e!r}"
        return out or "(no output)"

    async def teardown(self) -> None:
        """NO-OP by contract: a worker is done, but the per-challenge box PERSISTS
        so the next worker (escalation / race / lane reroute) takes it over live -
        the whole point of #12. The real teardown is release(), called once at
        challenge end. Kept named `teardown` (and still awaitable) so harness.py
        (teammate's module) needs no change: its stop() now just means "this worker
        let go of the box", not "destroy it"."""
        return

    async def release(self) -> None:
        """Destroy the per-challenge box and reap any sibling services it started.

        The ACTUAL teardown - call exactly ONCE, at challenge end (the bot does this
        on !kill / !solved via Registry.release_box). `rm -f` removes it whether
        running or stopped. Best-effort; swallow everything."""
        try:
            await self._docker("rm", "-f", self.name)
        except Exception:
            pass
        if self.docker_sock:
            await self._reap_children()
        self._started = False

    async def _reap_children(self) -> None:
        """Remove sibling containers the agent spun up via the mounted socket.

        Scoped by label so we never touch another challenge's containers: compose
        tags everything it creates with `com.docker.compose.project`, and manual
        runs are asked to set `cddc.thread`. Best-effort; swallow everything.
        """
        for label in (
            f"com.docker.compose.project={self.scope}",
            f"cddc.thread={self.thread_id}",
        ):
            try:
                rc, out = await self._docker(
                    "ps", "-aq", "--filter", f"label={label}"
                )
                ids = [x for x in out.split() if x] if rc == 0 else []
                if ids:
                    await self._docker("rm", "-f", *ids)
            except Exception:
                pass

    async def _docker(self, *args: str, timeout: int | None = None) -> tuple[int, str]:
        """Run `docker <args>`; return (returncode, combined stdout+stderr text).

        Argv form (no shell) for the docker invocation itself; the untrusted
        challenge command is passed as a single arg to `sh -c` inside the
        container, so host-shell metacharacters never apply.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, data.decode("utf-8", "replace")
