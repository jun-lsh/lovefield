#!/usr/bin/env python3
"""dc - CLI mirror of the shared pyghidra-mcp decompiler (one warm Ghidra server).

Agents use this (via run_shell) so they get the SAME decompiler the Claude harness
reaches over MCP - same tools, same names - with no per-agent JVM. The one
convenience on top of the raw MCP: you NEVER type the daemon's hash-suffixed binary
id. dc auto-targets the binary imported for THIS challenge; if several are loaded,
pass `--bin <name>` (see `dc binaries`). Server URL: CDDC_DECOMPILER_URL.

  dc import <path>              import + analyze a binary (path relative to your workdir)
  dc binaries [--all]           list this challenge's binaries (+ analysis status)
  dc info                       binary metadata (arch, compiler, endianness, hashes)

  dc decompile <fn|addr>...     decompile to pseudo-C   [--callees --strings --xrefs]
  dc functions [regex]          list functions (regex, case-insensitive; default all)
  dc symbols <regex>            search ALL symbols (labels/vars/classes too)
  dc strings <regex>            search defined strings
  dc imports [regex] | dc exports [regex]
  dc xrefs <fn|addr>...         cross-references to function(s)/symbol(s)/address(es)
  dc search <query> [--literal] search decompiled code (semantic by default)
  dc bytes <addr> [--size N]    read raw bytes
  dc callgraph <fn> [--direction both|calling|called]

  dc rename-func <old> <new>          dc rename-var <fn> <var> <new>
  dc set-type <fn> <var> <type>       dc set-proto <fn> <prototype>
  dc comment <target> <text> [--type decompiler|listing]

Every command accepts `--bin <name-substring>` to pick the binary when >1 is loaded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlparse

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("CDDC_DECOMPILER_URL", "http://cddc-decompiler:8000/mcp")
FILES_BASE = os.environ.get("CDDC_DECOMPILER_FILES_BASE", "/files").rstrip("/")
THREAD = os.environ.get("CDDC_THREAD", "")
# FastMCP enforces DNS-rebinding protection (allowed_hosts = localhost/127.0.0.1),
# so dialing the service by its container name returns 421. Connect over the network
# as normal, but present an allowed Host header.
_HEADERS = {"Host": f"localhost:{urlparse(URL).port or 8000}"}


def _strip(name: str) -> str:
    """/path/miro_bin-796880 -> miro_bin  (drop dir + the -<hash> the daemon appends)."""
    base = str(name).rsplit("/", 1)[-1]
    return base.rsplit("-", 1)[0] if "-" in base else base


def _service_path(p: str) -> str:
    """Map a workdir-relative (or /challenge/...) path to the service's /files view."""
    if p.startswith(FILES_BASE + "/"):
        return p
    rel = p[len("/challenge"):] if p.startswith("/challenge") else p
    rel = rel.lstrip("/")
    return f"{FILES_BASE}/{THREAD}/{rel}" if THREAD else f"{FILES_BASE}/{rel}"


async def _text(s, tool, args):
    res = await s.call_tool(tool, args)
    return "".join(getattr(c, "text", "") for c in (res.content or []))


async def _binaries(s, scoped=True):
    """Loaded binaries; scoped to THIS challenge (by file_path) unless scoped=False."""
    try:
        progs = json.loads(await _text(s, "list_project_binaries", {})).get("programs", [])
    except Exception:
        return []
    if scoped and THREAD:
        mine = [p for p in progs if f"/{THREAD}/" in str(p.get("file_path", ""))]
        return mine or progs  # fall back to all if none are tagged to this challenge
    return progs


async def _resolve(s, hint):
    """Resolve to ONE binary_name for THIS challenge, or None (after printing why).
    Never raises - a sys.exit here gets wrapped in an ugly BaseExceptionGroup."""
    progs = await _binaries(s)
    names = [p["name"] for p in progs if p.get("name")]
    if not names:
        print("dc: no binaries loaded for this challenge - run `dc import <path>` first")
        return None
    if hint:
        exact = [n for n in names if _strip(n) == hint]
        if len(exact) == 1:
            return exact[0]
        sub = [n for n in names if hint in n or hint in _strip(n)]
        if len(sub) == 1:
            return sub[0]
        opts = [_strip(n) for n in names]
        print(f"dc: --bin {hint!r} " + (f"is ambiguous among {opts}" if sub else f"matches none of {opts}"))
        return None
    if len(names) == 1:
        return names[0]
    opts = [_strip(n) for n in names]
    print(f"dc: this challenge has {len(names)} binaries: {opts}. "
          f"Pick one with --bin <name>, e.g. `--bin {opts[0]}`.")
    return None


def _print(txt):
    """Pretty-print a JSON tool result; pass through anything else."""
    try:
        print(json.dumps(json.loads(txt), indent=2))
    except Exception:
        print(txt or "(no output)")


def _show_decompile(txt):
    try:
        d = json.loads(txt)
    except Exception:
        print(txt)
        return
    for fn in (d if isinstance(d, list) else [d]):
        err = str(fn.get("error") or "")
        if err and not fn.get("code"):
            print(f"// {fn.get('name','?')}: {err}")
            if "not found" in err.lower():
                # A bad name is NOT a tool failure - say so, so the agent fixes its
                # input instead of concluding dc is broken.
                print("// dc is working - that name isn't in this binary. `dc functions <regex>` "
                      "to list real names (Go funcs look like `main.main`), then decompile one.")
            continue
        if fn.get("signature"):
            print("// " + str(fn["signature"]))
        print(fn.get("code") or "// (no code)")
        if fn.get("referenced_strings"):
            print("\n// strings: " + json.dumps(fn["referenced_strings"]))
        if fn.get("xrefs"):
            print("// xrefs: " + json.dumps(fn["xrefs"]))
        print()


async def amain(a):
    async with streamablehttp_client(URL, headers=_HEADERS) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            # --- commands that DON'T need a resolved binary -------------------
            if a.cmd == "binaries":
                progs = await _binaries(s, scoped=not a.all)
                if not progs:
                    print("(no binaries loaded - `dc import <path>`)")
                for p in progs:
                    state = "ready" if p.get("analysis_complete") else "analyzing"
                    print(f"{_strip(p.get('name'))}\t({p.get('name')})\t<- {p.get('file_path')}\t[{state}]")
                return
            if a.cmd == "import":
                sp = _service_path(a.path)
                out = await _text(s, "import_binary", {"binary_path": sp})
                _print(out)
                if "cannot be found" in out.lower() or "not found" in out.lower():
                    print(f"// dc: the decompiler can't see {sp}. Its /files mount must point at the "
                          f"bot's CDDC_FILES_DIR - re-run sandbox/run-decompiler.sh.")
                return

            # --- everything else operates on ONE binary -----------------------
            bn = await _resolve(s, a.bin)
            if bn is None:
                return

            if a.cmd == "decompile":
                tgt = a.target if len(a.target) > 1 else a.target[0]
                _show_decompile(await _text(s, "decompile_function", {
                    "binary_name": bn, "name_or_address": tgt,
                    "include_callees": a.callees, "include_strings": a.strings,
                    "include_xrefs": a.xrefs, "timeout_sec": a.timeout}))
            elif a.cmd in ("functions", "symbols"):
                _print(await _text(s, "search_symbols_by_name", {
                    "binary_name": bn, "query": a.query,
                    "functions_only": a.cmd == "functions", "limit": a.limit}))
            elif a.cmd == "strings":
                _print(await _text(s, "search_strings", {"binary_name": bn, "query": a.query, "limit": a.limit}))
            elif a.cmd in ("imports", "exports"):
                tool = "list_imports" if a.cmd == "imports" else "list_exports"
                _print(await _text(s, tool, {"binary_name": bn, "query": a.query, "limit": a.limit}))
            elif a.cmd == "xrefs":
                tgt = a.target if len(a.target) > 1 else a.target[0]
                _print(await _text(s, "list_xrefs", {"binary_name": bn, "name_or_address": tgt}))
            elif a.cmd == "search":
                _print(await _text(s, "search_code", {
                    "binary_name": bn, "query": a.query,
                    "search_mode": "literal" if a.literal else "semantic", "limit": a.limit}))
            elif a.cmd == "bytes":
                _print(await _text(s, "read_bytes", {"binary_name": bn, "address": a.address, "size": a.size}))
            elif a.cmd == "info":
                _print(await _text(s, "list_project_binary_metadata", {"binary_name": bn}))
            elif a.cmd == "callgraph":
                args = {"binary_name": bn, "function_name": a.function}
                if a.direction:
                    args["direction"] = a.direction
                _print(await _text(s, "gen_callgraph", args))
            elif a.cmd == "rename-func":
                _print(await _text(s, "rename_function", {
                    "binary_name": bn, "name_or_address": a.old, "new_name": a.new}))
            elif a.cmd == "rename-var":
                _print(await _text(s, "rename_variable", {
                    "binary_name": bn, "function_name_or_address": a.function,
                    "variable_name": a.var, "new_name": a.new}))
            elif a.cmd == "set-type":
                _print(await _text(s, "set_variable_type", {
                    "binary_name": bn, "function_name_or_address": a.function,
                    "variable_name": a.var, "type_name": a.type}))
            elif a.cmd == "set-proto":
                _print(await _text(s, "set_function_prototype", {
                    "binary_name": bn, "function_name_or_address": a.function, "prototype": a.prototype}))
            elif a.cmd == "comment":
                _print(await _text(s, "set_comment", {
                    "binary_name": bn, "target": a.target, "comment": a.comment, "comment_type": a.type}))


def _parse():
    p = argparse.ArgumentParser(
        prog="dc", description="CLI mirror of the shared pyghidra-mcp decompiler. "
        "Binary is auto-targeted to this challenge; use --bin <name> if >1 is loaded.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def bin_flag(sp):
        sp.add_argument("--bin", dest="bin", default="", metavar="NAME",
                        help="binary name/substring when >1 is loaded (see `dc binaries`)")

    pb = sub.add_parser("binaries", help="list this challenge's loaded binaries")
    pb.add_argument("--all", action="store_true", help="show every binary in the shared project")
    pi = sub.add_parser("import", help="import + analyze a binary from a path")
    pi.add_argument("path")
    pinf = sub.add_parser("info", help="binary metadata: arch, compiler, endianness, hashes")
    bin_flag(pinf)

    pd = sub.add_parser("decompile", help="decompile function(s) to pseudo-C, by name or address")
    pd.add_argument("target", nargs="+", metavar="FN|ADDR")
    bin_flag(pd)
    pd.add_argument("--callees", action="store_true", help="also attach callees")
    pd.add_argument("--strings", action="store_true", help="also attach referenced strings")
    pd.add_argument("--xrefs", action="store_true", help="also attach xrefs")
    pd.add_argument("--timeout", type=int, default=120, metavar="SEC", help="decompiler timeout per target")

    pf = sub.add_parser("functions", help="list/search FUNCTIONS by regex (case-insensitive)")
    pf.add_argument("query", nargs="?", default=".*")
    bin_flag(pf)
    pf.add_argument("--limit", type=int, default=100)
    psym = sub.add_parser("symbols", help="search ALL symbols by regex (labels/vars/classes too)")
    psym.add_argument("query")
    bin_flag(psym)
    psym.add_argument("--limit", type=int, default=100)

    pstr = sub.add_parser("strings", help="search defined strings")
    pstr.add_argument("query", nargs="?", default=".*")
    bin_flag(pstr)
    pstr.add_argument("--limit", type=int, default=200)
    for c in ("imports", "exports"):
        pc = sub.add_parser(c, help=f"list {c} (optional regex filter)")
        pc.add_argument("query", nargs="?", default=".*")
        bin_flag(pc)
        pc.add_argument("--limit", type=int, default=100)
    px = sub.add_parser("xrefs", help="cross-references to function(s)/symbol(s)/address(es)")
    px.add_argument("target", nargs="+", metavar="FN|ADDR")
    bin_flag(px)
    pse = sub.add_parser("search", help="search decompiled code (semantic default; --literal for exact)")
    pse.add_argument("query")
    bin_flag(pse)
    pse.add_argument("--literal", action="store_true")
    pse.add_argument("--limit", type=int, default=5)
    pby = sub.add_parser("bytes", help="read raw bytes at an address")
    pby.add_argument("address")
    bin_flag(pby)
    pby.add_argument("--size", type=int, default=64)
    pcg = sub.add_parser("callgraph", help="MermaidJS call graph for a function")
    pcg.add_argument("function")
    bin_flag(pcg)
    pcg.add_argument("--direction", choices=["both", "calling", "called"], default="")

    prf = sub.add_parser("rename-func", help="rename a function")
    prf.add_argument("old", metavar="NAME|ADDR")
    prf.add_argument("new")
    bin_flag(prf)
    prv = sub.add_parser("rename-var", help="rename a parameter/local by exact name")
    prv.add_argument("function", metavar="FN|ADDR")
    prv.add_argument("var")
    prv.add_argument("new")
    bin_flag(prv)
    pst = sub.add_parser("set-type", help="set a parameter/local type by exact name")
    pst.add_argument("function", metavar="FN|ADDR")
    pst.add_argument("var")
    pst.add_argument("type")
    bin_flag(pst)
    pp = sub.add_parser("set-proto", help="set a function prototype")
    pp.add_argument("function", metavar="FN|ADDR")
    pp.add_argument("prototype")
    bin_flag(pp)
    pcm = sub.add_parser("comment", help="set a decompiler/listing comment at a target")
    pcm.add_argument("target", metavar="FN|ADDR")
    pcm.add_argument("comment")
    bin_flag(pcm)
    pcm.add_argument("--type", default="decompiler", choices=["decompiler", "listing"])

    return p.parse_args()


def main():
    a = _parse()
    try:
        anyio.run(amain, a)
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"dc: cannot reach decompiler at {URL} ({type(e).__name__}: {e}). Is the service up?")


if __name__ == "__main__":
    main()
