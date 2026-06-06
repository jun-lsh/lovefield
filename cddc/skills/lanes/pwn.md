Binary exploitation. Find a memory-safety bug and turn it into flag exfiltration.

- Triage: `file`, `checksec` (canary/NX/PIE/RELRO), identify libc version.
- Find the primitive: overflow, format string, UAF, off-by-one. Locate `win()`
  / a leak gadget. Map the input path from stdin/argv/network to the bug.
- Build the exploit as a pwntools script written to disk; test locally before
  anything remote.

NOTE: no debugger/pwn tooling on this box yet, and DO NOT run untrusted target
binaries here (no isolation). For now: static analysis + draft the exploit, and
flag that local/remote execution needs the container or an on-site operator.
