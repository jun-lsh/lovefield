# Current Environment

SWAPPABLE SNIPPET - these are facts about the box you are running on RIGHT NOW,
not doctrine. Update this file when the tool container lands; do not bake these
into the role/lane prompts.

- You are on bare-metal Windows. Your `run_shell` is the local shell.
- **No SageMath.** For crypto, use pure python / pycryptodome / sympy / z3 /
  gmpy2. If a solve genuinely needs Sage (lattice reduction, polynomial GCD over
  Z_n[x], heavy elliptic-curve work), say so plainly - that is a reason to
  escalate, not to fake it with a tool that cannot do the job.
- `web_search` is available (Google via Serper, or DuckDuckGo) - use it to look
  up a CVE, library version, error string, attack name, or find a writeup. Read a
  result's full page with `read_url` (clean extraction, handles bot-blocked
  pages); use `fetch_url` only for the challenge's own target/host. If the tool
  reports it is not configured, say so and ask the operator.
- No reversing/pwn tooling (gdb, ghidra, etc.) is set up here yet. Treat binary
  challenges as analysis-only for now and flag what you would need.
- Docker (only if `docker` works in your shell - a host socket is bound in for
  specialist/deep roles, not triage): you can `docker compose up` / `docker run` a
  service challenge against the host daemon. Containers you start are
  auto-labeled `cddc.thread=$CDDC_THREAD` and reaped per-challenge, so you do NOT
  need to clean them up. The one exception: if you create a container by some
  OTHER route (the python `docker` SDK, a raw API call, podman), label it
  `cddc.thread=$CDDC_THREAD` yourself or it will leak.
