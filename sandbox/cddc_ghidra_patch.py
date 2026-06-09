"""CDDC overlay for pyghidra-mcp: robust function lookup by name.

The problem it fixes
--------------------
pyghidra-mcp resolves a function name by comparing the query ONLY against each
function's PRIMARY symbol (`f.getSymbol().getName(True)`).  GolangAnalyzer (and
some manglers / strippers) attach the human name -- e.g. `main.transformSeed` --
as a *label at the entry point* that is NOT the function's primary symbol, so a
by-name lookup misses while by-address works (the address branch short-circuits
first).  On a Go binary the agent SEES `main.transformSeed` in the function
listing yet `decompile main.transformSeed` returns "not found": pure friction
over a tool we fully control, hit identically by `dc` and the Claude harness.

What it does
------------
Replaces `GhidraTools._lookup_functions` with a version that is strictly
ADDITIVE -- same address-first fast path, same primary-name match, PLUS:
  - a direct symbol-table lookup (global + any-namespace) resolved back to its
    containing function (catches Go entry-point labels),
  - a scan of every label sitting at each function's entry point,
  - `::` <-> `.` separator normalization (Ghidra namespaces vs Go dotted names),
  - a unique simple-name fallback (`transformSeed` -> the one `*.transformSeed`).
It can never match LESS than upstream, so it is safe to leave installed.

How it attaches
---------------
A one-line `.pth` (`import cddc_ghidra_patch`) dropped into pyghidra-mcp's venv
site-packages imports THIS module at interpreter start.  We do NOT import
`pyghidra_mcp.tools` here (it pulls Ghidra classes that only exist after the JVM
starts); instead we install a meta-path import hook that patches the module the
moment the SERVER imports it -- after the JVM is up.  Every failure is swallowed
so a broken overlay can never stop the decompiler from booting.

Iterate without a full image rebuild: edit this file, copy it into the running
container's site-packages and restart -- the import hook re-arms at startup:
  sp=$(docker exec cddc-decompiler sh -lc \
        'sed -n "1s/^#!//p" /usr/local/bin/pyghidra-mcp | xargs -I{} {} -c \
         "import sysconfig;print(sysconfig.get_path(\"purelib\"))"')
  docker cp sandbox/cddc_ghidra_patch.py cddc-decompiler:"$sp/cddc_ghidra_patch.py"
  docker restart cddc-decompiler
"""

import importlib.abc
import logging
import sys

logger = logging.getLogger("cddc.ghidra_patch")

_TARGET = "pyghidra_mcp.tools"


def _norm(name):
    """Lowercase + treat Ghidra's '::' namespace sep and Go's '.' as equivalent."""
    return str(name).lower().replace("::", ".")


def _robust_lookup_functions(
    self, name_or_address, *, exact=True, partial=False, include_externals=True
):
    """Drop-in replacement for GhidraTools._lookup_functions (same signature)."""
    program = self.program
    af = program.getAddressFactory()
    fm = program.getFunctionManager()
    st = program.getSymbolTable()

    # 1) address first (also accept a leading 0x) -- unchanged fast path.
    for cand in (
        name_or_address,
        name_or_address[2:] if name_or_address[:2].lower() == "0x" else None,
    ):
        if not cand:
            continue
        try:
            addr = af.getAddress(cand)
        except Exception:
            addr = None
        if addr:
            func = fm.getFunctionAt(addr)
            if func:
                return [func]

    target = _norm(name_or_address)
    functions = self.get_all_functions(include_externals=include_externals)
    seen = set()
    matches = []

    def _add(func):
        if func is None:
            return
        key = func.getEntryPoint()
        if key not in seen:
            seen.add(key)
            matches.append(func)

    def _primary_names(f):
        out = set()
        try:
            out.add(_norm(f.getName()))
        except Exception:
            pass
        try:
            sym = f.getSymbol()
        except Exception:
            sym = None
        if sym is not None:
            for arg in (True, False):
                try:
                    out.add(_norm(sym.getName(arg)))
                except Exception:
                    pass
        return out

    # 2) cheap exact: against each function's primary name(s) -- upstream behavior,
    #    just broadened to getName()/getName(False) as well as getName(True).
    if exact:
        for f in functions:
            if target in _primary_names(f):
                _add(f)

    # 3) exact fallback: ask the symbol table directly, then resolve a symbol back
    #    to its containing function. This is the one that fixes Go entry-point
    #    labels that are not the primary symbol, and it is O(lookup), not O(funcs).
    if exact and not matches:
        variants = {
            name_or_address,
            name_or_address.replace("::", "."),
            name_or_address.replace(".", "::"),
        }
        syms = []
        for v in variants:
            try:
                syms.extend(list(st.getGlobalSymbols(v)))
            except Exception:
                pass
            try:
                syms.extend(list(st.getSymbols(v)))
            except Exception:
                pass
        for s in syms:
            try:
                addr = s.getAddress()
            except Exception:
                continue
            _add(fm.getFunctionContaining(addr) or fm.getFunctionAt(addr))

    # 4) exact last resort: scan EVERY label at each function's entry point.
    if exact and not matches:
        for f in functions:
            try:
                labels = st.getSymbols(f.getEntryPoint())
            except Exception:
                continue
            names = set()
            for s in labels:
                for arg in (True, False):
                    try:
                        names.add(_norm(s.getName(arg)))
                    except Exception:
                        pass
            if target in names:
                _add(f)

    # 5) exact convenience: a bare simple name (`foo`) -> the one `*.foo`, but only
    #    when it is unambiguous (otherwise leave it for the caller's error).
    if exact and not matches and "." not in target:
        uniq = {}
        for f in functions:
            if any(n.rsplit(".", 1)[-1] == target for n in _primary_names(f)):
                uniq[f.getEntryPoint()] = f
        if len(uniq) == 1:
            _add(next(iter(uniq.values())))

    # 6) partial: substring across all primary name forms (faithful to upstream).
    if partial:
        for f in functions:
            if any(target in n for n in _primary_names(f)):
                _add(f)

    return matches


def _patch(module):
    GhidraTools = getattr(module, "GhidraTools", None)
    if GhidraTools is None or getattr(GhidraTools, "_cddc_patched", False):
        return
    GhidraTools._lookup_functions = _robust_lookup_functions
    GhidraTools._cddc_patched = True
    logger.info(
        "cddc ghidra patch: GhidraTools._lookup_functions replaced "
        "(robust name lookup for Go / namespaced / labeled functions)"
    )


class _Loader(importlib.abc.Loader):
    """Wraps the real loader so we patch right after the module finishes loading."""

    def __init__(self, real):
        self._real = real

    def create_module(self, spec):
        return self._real.create_module(spec)

    def exec_module(self, module):
        self._real.exec_module(module)
        try:
            _patch(module)
        except Exception:
            logger.exception("cddc ghidra patch: failed to patch %s", _TARGET)


class _Finder(importlib.abc.MetaPathFinder):
    """Intercepts the import of pyghidra_mcp.tools and wraps its loader."""

    def find_spec(self, name, path, target=None):
        if name != _TARGET:
            return None
        # Resolve the real spec via the OTHER finders (skip self -> no recursion).
        for finder in sys.meta_path:
            if finder is self:
                continue
            try:
                spec = finder.find_spec(name, path, target)
            except Exception:
                spec = None
            if spec is not None:
                if spec.loader is not None:
                    spec.loader = _Loader(spec.loader)
                return spec
        return None


def _install():
    # If the server already imported tools (e.g. a warm reload), patch it now.
    already = sys.modules.get(_TARGET)
    if already is not None:
        _patch(already)
        return
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


try:
    _install()
except Exception:  # never let an overlay failure stop the server from booting
    logger.exception("cddc ghidra patch: install failed; using upstream lookup")
