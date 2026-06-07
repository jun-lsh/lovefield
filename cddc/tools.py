"""Agent tools - operate inside the per-challenge workdir, return short digests.

v1 runs LOCALLY with NO isolation (operator's call, for fast iteration). Safe
for crypto / web / research / forensics-on-safe-files. Do NOT point the agent at
untrusted pwn/rev binaries until containerised. Output is truncated to a digest,
not dumped raw, to keep token cost down.

Discord-agnostic; stdlib only.
"""

from __future__ import annotations

import asyncio
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
            "(no search engine). Use this for the challenge's OWN target/host; "
            "use read_url for public reference pages.",
            {"url": {"type": "string"}},
            ["url"],
        ),
        _spec(
            "web_search",
            "Search the web (Google via Serper, or DuckDuckGo) and return ranked "
            "title/url/snippet results. Use it to look up a CVE, library version, "
            "error string, attack name, or find a writeup. To read a result's "
            "full page, follow up with read_url.",
            {
                "query": {"type": "string"},
                "num_results": {
                    "type": "integer",
                    "description": "how many results, default 5 (max 10)",
                },
            },
            ["query"],
        ),
        _spec(
            "read_url",
            "Fetch a PUBLIC web page (docs, writeup, CVE entry) and return clean "
            "extracted text via a reader service - better than fetch_url for "
            "bot-blocked or heavy pages. Use fetch_url for the challenge's own "
            "target/host instead (a reader can't reach internal/localhost).",
            {"url": {"type": "string"}},
            ["url"],
        ),
        _spec(
            "list_skill_docs",
            "List available markdown skill docs under cddc/skills. Use this "
            "to discover lane references before reading a specific doc.",
            {
                "path": {
                    "type": "string",
                    "description": "Optional subdirectory under cddc/skills, e.g. lanes/ctf-pwn",
                }
            },
            [],
        ),
        _spec(
            "read_skill_doc",
            "Read a markdown skill doc from cddc/skills. Read only the docs "
            "that are relevant to the current challenge.",
            {
                "path": {
                    "type": "string",
                    "description": "Path under cddc/skills, e.g. lanes/ctf-pwn/SKILL.md",
                }
            },
            ["path"],
        ),
        _spec(
            "submit_flag",
            "Submit a candidate flag for human validation. Halts the agent. Only "
            "call after verifying - do not guess.",
            {"flag": {"type": "string"}},
            ["flag"],
        ),
        _spec(
            "solve_ready",
            "Call this when you have a WORKING solve/exploit that succeeds locally "
            "but cannot produce the REAL flag because something external is missing "
            "- most often the remote target (host:port) isn't connected, or a "
            "server-provided input. It pings the operator to hook it up (same alert "
            "as a flag). Do NOT use it for being stuck - only when the solve itself "
            "works and the ONLY thing left is the external piece.",
            {
                "summary": {"type": "string", "description": "what your solve does / how it works"},
                "needs": {
                    "type": "string",
                    "description": "the exact external thing required, e.g. "
                    "'remote host:port for the ElGamal oracle'",
                },
            },
            ["summary", "needs"],
        ),
        _spec(
            "triage_report",
            "File your triage report - the terminal handoff when you are NOT "
            "cleanly solving this yourself. You are a cheap first pass: do NOT "
            "decide the challenge's fate, RECOMMEND and let the operator pick. "
            "Halts for an operator decision. Give an honest read - a clear 'this "
            "is hard, here's why' is a win, not a failure.",
            {
                "gist": {
                    "type": "string",
                    "description": "what the challenge IS / what it wants, in 1-2 lines",
                },
                "difficulty": {
                    "type": "integer",
                    "description": "1 (trivial) to 5 (very hard)",
                },
                "technique": {
                    "type": "string",
                    "description": "suspected attack/challenge class, e.g. 'RSA/Franklin-Reiter'",
                },
                "blockers": {
                    "type": "string",
                    "description": "what makes it hard / what's needed - include any "
                    "web_search signals (e.g. 'libfoo 2.0 -> CVE-2024-xxxx exists')",
                },
                "recommendation": {
                    "type": "string",
                    "enum": ["solo_finish", "race", "specialist", "deep_solver", "needs_human"],
                    "description": "your advised next move (the operator may override): "
                    "solo_finish=let an agent keep grinding; race=fan out 2-3; "
                    "specialist=hand to a specialist on this lane; deep_solver=the "
                    "expensive deep researcher; needs_human=a human must look",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "how sure you are of this read",
                },
            },
            ["gist", "difficulty", "blockers", "recommendation"],
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

    def __init__(self, workdir: str, shell_timeout: int = 30, *, sandbox=None, skills_dir: str | None = None,
                 searcher=None, reader=None) -> None:
        self.workdir = pathlib.Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.shell_timeout = shell_timeout
        self.sandbox = sandbox
        self.skills_dir = pathlib.Path(skills_dir).resolve() if skills_dir else None
        # Injected async callables (cddc.search factories): searcher(query, num)
        # -> [{title,url,snippet}], reader(url) -> clean text. None = that tool is
        # unconfigured (the handler returns a clear message; the worker also drops
        # it from the advertised specs).
        self.searcher = searcher
        self.reader = reader

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
            if name == "web_search":
                return await self._web_search(str(args.get("query", "")), args.get("num_results"))
            if name == "read_url":
                return await self._read_url(str(args.get("url", "")))
            if name == "list_skill_docs":
                return self._list_skill_docs(str(args.get("path", "")))
            if name == "read_skill_doc":
                return self._read_skill_doc(str(args.get("path", "")))
            return f"unknown tool: {name}"
        except Exception as e:  # tools must never crash the agent loop
            return f"tool error: {e!r}"

    def _safe(self, path: str) -> pathlib.Path:
        p = (self.workdir / path).resolve()
        root = self.workdir.resolve()
        if root != p and root not in p.parents:
            raise ValueError(f"path escapes workdir: {path}")
        return p

    def _safe_skill(self, path: str) -> pathlib.Path:
        if self.skills_dir is None:
            raise ValueError("skills dir not configured")
        p = (self.skills_dir / path).resolve()
        root = self.skills_dir
        if root != p and root not in p.parents:
            raise ValueError(f"path escapes skills dir: {path}")
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

    def _list_skill_docs(self, path: str = "") -> str:
        root = self._safe_skill(path)
        if not root.exists():
            return f"no such skill path: {path}"
        if root.is_file():
            if root.suffix.lower() != ".md":
                return f"not a markdown skill doc: {path}"
            return root.relative_to(self.skills_dir).as_posix()

        docs = sorted(
            p.relative_to(self.skills_dir).as_posix()
            for p in root.rglob("*.md")
            if p.is_file()
        )
        return "\n".join(docs) or "(no markdown skill docs)"

    def _read_skill_doc(self, path: str) -> str:
        p = self._safe_skill(path)
        if not p.exists():
            return f"no such skill doc: {path}"
        if not p.is_file():
            return f"not a file: {path}"
        if p.suffix.lower() != ".md":
            return f"not a markdown skill doc: {path}"
        return _truncate(p.read_text("utf-8", "replace"))

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

    async def _web_search(self, query: str, num) -> str:
        if self.searcher is None:
            return "web_search not configured (set CDDC_WEB_SEARCH=ddg|serper)"
        if not query.strip():
            return "(empty query)"
        try:
            results = await self.searcher(query, num)
        except Exception as e:
            return f"search error: {e!r}"
        if not results:
            return "(no results)"
        lines = []
        for i, r in enumerate(results, 1):
            block = f"{i}. {r.get('title', '')}\n   {r.get('url', '')}"
            snip = (r.get("snippet") or "").strip()
            if snip:
                block += f"\n   {snip}"
            lines.append(block)
        return _truncate("\n".join(lines))

    async def _read_url(self, url: str) -> str:
        if self.reader is None:
            return "read_url not configured"
        if not url.strip():
            return "(empty url)"
        try:
            return _truncate(await self.reader(url))
        except Exception as e:
            return f"read error: {e!r}"


def _truncate(s: str, n: int = MAX_OUTPUT) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n...[truncated {len(s) - n} chars]"
