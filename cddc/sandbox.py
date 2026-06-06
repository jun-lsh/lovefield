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
    ) -> None:
        self.image = image
        self.host_workdir = host_workdir
        self.mount = mount
        self.name = f"cddc-{thread_id}"
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
        await self._docker("rm", "-f", self.name)
        rc, out = await self._docker(
            "run", "-d", "--rm",
            "--name", self.name,
            "-w", self.mount,
            "-v", f"{host_abs}:{self.mount}:Z",
            self.image,
            "sleep", "infinity",
        )
        if rc != 0:
            # Leave _started False; exec() will return a clear tool error.
            raise RuntimeError(f"docker run failed (rc={rc}): {out.strip()[:500]}")
        self._started = True

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
        try:
            await self._docker("rm", "-f", self.name)
        except Exception:
            pass
        self._started = False

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
