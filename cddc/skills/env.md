# Current Environment

SWAPPABLE SNIPPET - these are facts about the box you are running on RIGHT NOW,
not doctrine. Update this file when the tool container lands; do not bake these
into the role/lane prompts.

- You are on bare-metal Windows. Your `run_shell` is the local shell.
- **No SageMath.** For crypto, use pure python / pycryptodome / sympy / z3 /
  gmpy2. If a solve genuinely needs Sage (lattice reduction, polynomial GCD over
  Z_n[x], heavy elliptic-curve work), say so plainly - that is a reason to
  escalate, not to fake it with a tool that cannot do the job.
- No dedicated `web_search` tool is wired in yet. `fetch_url` is direct-fetch
  only. If you need to look something up, say so and ask the operator.
- No reversing/pwn tooling (gdb, ghidra, etc.) is set up here yet. Treat binary
  challenges as analysis-only for now and flag what you would need.
