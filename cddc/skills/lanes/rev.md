Reverse engineering. Understand what the binary/bytecode does, then recover or
satisfy the flag check.

- Triage: `file`, `strings`, check for packers, identify arch/format and
  language (Go/Rust/C/.NET/Python-frozen all change the approach).
- Find the check: locate where input is compared to the flag, or where the flag
  is derived. Static read first; name the algorithm (xor, simple math, a VM, a
  hash compare).
- If it's a transformation you can invert, write a script to invert it. If it's a
  constraint check, model it with z3 and solve for the input.
- Frozen/bytecode (PyInstaller, .pyc, .NET): decompile rather than disassemble.

NOTE: heavy tooling (gdb, ghidra, a debugger) is NOT set up on this box yet -
treat this as static analysis for now and flag clearly if dynamic analysis is
what's needed.
