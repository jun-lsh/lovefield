"""Agent tools - operate inside the per-challenge workdir, return short digests.

v1 runs LOCALLY with NO isolation (operator's call, for fast iteration). Safe
for crypto / web / research / forensics-on-safe-files. Do NOT point the agent at
untrusted pwn/rev binaries until containerised. Output is truncated to a digest,
not dumped raw, to keep token cost down.

Discord-agnostic; stdlib only.
"""

from __future__ import annotations

import asyncio
import os
import pathlib

MAX_OUTPUT = 4000  # chars of tool output fed back to the model


def tool_specs() -> list[dict]:
    """OpenAI-format tool schemas advertised to the model."""
    return [
        _spec(
            "run_shell",
            "Run a shell command in the challenge workdir (python is available). "
            "Returns combined stdout+stderr, truncated.",
            {"command": {"type": "string"}},
            ["command"],
        ),
        _spec(
            "read_file",
            "Read a file from the workdir (truncated).",
            {"path": {"type": "string"}},
            ["path"],
        ),
        _spec(
            "write_file",
            "Write text to a file in the workdir (e.g. a solver script).",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        _spec(
            "fetch_url",
            "Download a URL and return its text, truncated. Direct fetch only "
            "(no search engine).",
            {"url": {"type": "string"}},
            ["url"],
        ),
        _spec(
            "submit_flag",
            "Submit a candidate flag for human validation. Halts the agent. Only "
            "call after verifying - do not guess.",
            {"flag": {"type": "string"}},
            ["flag"],
        ),
        _spec(
            "request_escalation",
            "Escalate this challenge to a specialist when it is beyond a cheap "
            "triage solve (too hard, or it needs tooling/knowledge you lack). "
            "Halts you for an operator decision - prefer this over grinding a "
            "hard challenge. Give your honest difficulty read and the reason.",
            {
                "difficulty": {
                    "type": "integer",
                    "description": "1 (trivial) to 5 (very hard)",
                },
                "technique": {
                    "type": "string",
                    "description": "named attack/challenge class, e.g. 'RSA/Franklin-Reiter'",
                },
                "reason": {
                    "type": "string",
                    "description": "one line: why a specialist is needed (what's blocking you)",
                },
            },
            ["difficulty", "technique", "reason"],
        ),
    ]


def _spec(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


class Toolbox:
    """Executes tool calls in `workdir`. Path access is confined to the workdir.

    If a `sandbox` is given, run_shell executes inside that container (the workdir
    is bind-mounted in, so read/write still operate host-side on the same files);
    otherwise run_shell runs on the host with no isolation.
    """

    def __init__(self, workdir: str, shell_timeout: int = 30, *, sandbox=None) -> None:
        self.workdir = pathlib.Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.shell_timeout = shell_timeout
        self.sandbox = sandbox

    async def run(self, name: str, args: dict) -> str:
        try:
            if name == "run_shell":
                return await self._shell(str(args.get("command", "")))
            if name == "read_file":
                return self._read(str(args.get("path", "")))
            if name == "write_file":
                return self._write(str(args.get("path", "")), str(args.get("content", "")))
            if name == "fetch_url":
                return await self._fetch(str(args.get("url", "")))
            return f"unknown tool: {name}"
        except Exception as e:  # tools must never crash the agent loop
            return f"tool error: {e!r}"

    def _safe(self, path: str) -> pathlib.Path:
        p = (self.workdir / path).resolve()
        root = self.workdir.resolve()
        if root != p and root not in p.parents:
            raise ValueError(f"path escapes workdir: {path}")
        return p

    async def _shell(self, command: str) -> str:
        if not command.strip():
            return "(empty command)"
        if self.sandbox is not None:
            return _truncate(await self.sandbox.exec(command, self.shell_timeout))
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.shell_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"(command timed out after {self.shell_timeout}s)"
        return _truncate(out.decode("utf-8", "replace")) or "(no output)"

    def _read(self, path: str) -> str:
        p = self._safe(path)
        if not p.exists():
            return f"no such file: {path}"
        return _truncate(p.read_text("utf-8", "replace"))

    def _write(self, path: str, content: str) -> str:
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    async def _fetch(self, url: str) -> str:
        import urllib.request

        def _get() -> str:
            req = urllib.request.Request(url, headers={"User-Agent": "cddc-agent"})
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                return r.read(200_000).decode("utf-8", "replace")

        try:
            return _truncate(await asyncio.to_thread(_get))
        except Exception as e:
            return f"fetch error: {e!r}"


def _truncate(s: str, n: int = MAX_OUTPUT) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n...[truncated {len(s) - n} chars]"
