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
    """A docker container that lives for one challenge worker's lifetime.

    start() -> docker run -d ... sleep infinity   (container stays up)
    exec()  -> docker exec ... sh -c <command>    (one tool call)
    teardown() -> docker rm -f                    (best-effort cleanup)
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

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Launch the container detached. Idempotent: removes any stale one first."""
        import os

        os.makedirs(self.host_workdir, exist_ok=True)
        host_abs = os.path.abspath(self.host_workdir)
        # Drop a stale container of the same name (e.g. from a crashed prior run).
        # TODO: decide when to delete. it could contain solve scripts / important stuff
        await self._docker("stop", self.name)
        rc, out = await self._docker(*self._run_argv(host_abs))
        if rc != 0:
            # Leave _started False; exec() will return a clear tool error.
            raise RuntimeError(f"docker run failed (rc={rc}): {out.strip()[:500]}")
        self._started = True

    def _run_argv(self, host_abs: str) -> list[str]:
        """Build the `docker run` argv. Split out so the sim can assert on it
        (mounts/socket) without actually launching a container."""
        workdir_spec = f"{host_abs}:{self.mount}"
        if self.mount_flag:
            workdir_spec += f":{self.mount_flag}"
        mount_args: list[str] = ["-v", workdir_spec]
        for spec in self.extra_mounts:
            mount_args += ["-v", spec]
        sock_args: list[str] = []
        if self.docker_sock:
            # Bind the host daemon socket in, and pre-seed COMPOSE_PROJECT_NAME +
            # a thread label so any `docker compose up` the agent runs is scoped to
            # this challenge and reapable on teardown. (Manual `docker run`s should
            # carry `--label cddc.thread=<id>`; CDDC_THREAD is exported for that.)
            sock_args = [
                "-v", f"{self.docker_sock}:{self.docker_sock}",
                "-e", f"COMPOSE_PROJECT_NAME={self.scope}",
                "-e", f"CDDC_THREAD={self.thread_id}",
            ]
        gpu_args = ["--gpus", "all"] if self.gpu else []
        return [
            "run", "-d", "--rm",
            "--name", self.name,
            "-w", self.mount,
            *mount_args,
            *sock_args,
            *gpu_args,
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
            rc, out = await self._docker(
                "exec", "-w", self.mount, self.name,
                "sh", "-c", command,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"(command timed out after {timeout}s)"
        except Exception as e:  # never propagate into the agent loop
            return f"tool error: {e!r}"
        return out or "(no output)"

    async def teardown(self) -> None:
        """Force-remove the container. Best-effort; swallow everything."""
        # TODO: save solve script and important files before tearing down
        try:
            await self._docker("stop", self.name)
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
