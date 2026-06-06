# CDDC 2026 Agent Stack — Architecture (v2)

Agent fleet that full-clears the online/sloppable set while humans take the on-site hardware. Built from how CDDC 2024/25 actually played + lessons from verialabs (race swarms, generic toolset, never-give-up) and Squid Agent (category specialization, curated tools, validation gates, persistent decompiler).

---

## Operating model

- **4 operators**: you (remote) + 3 on-site. Humans pick and route challenges. **No autonomous global coordinator** — Discord is the substrate.
- **Discord-native**: category channels exist → **one thread per challenge** = that challenge's workspace. Agent output streams in; operator drives.
- **Bidirectional control plane** (built first): a dropped challenge spawns a Worker that **pushes progress proactively**, answers **polls** (`!status`), accepts **explicit steering** (`!steer <text>` — so thread discussion doesn't accidentally nudge the agent), and obeys **control** (`!race`/`!pause`/`!resume`/`!kill`/`!lane`). Workers are never fire-and-forget — you can always see what's happening and steer it.
- **Unified agents + dashboard:** on-site player agents and the remote fleet are the *same kind of agent* — both narrate to a Discord thread (per-agent webhook identity) and heartbeat to a shared **dashboard** (simple CRUD on a VPS: challenges, agents-per-challenge, solve status). Many agents can work one challenge; first valid flag wins. The dashboard is a **mirror, not a master** — Discord stays primary; agents keep working if it wobbles.
- **Geographic split** (this is load-bearing):
  - **On-site tier** owns anything *interactive / box-facing* — live web exploitation, live pwn, reverse-callbacks, timing-sensitive exploits. Direct ethernet, no proxy.
  - **Remote tier** (you + this fleet) is *analysis & churn in parallel* — reversing, crypto, static analysis, forensics on pulled files, CVE research, exploit-script authoring (handed to on-site to fire). Outbound-only contact with boxes.
- **Load**: ~25 drop at once day 1. ~5-7 are find-CVE-PoC→run. The bulk are basic decomp/crypto/web/forensics. 1-2 genuinely hard per category. Day 2 = more of the same + trad med-ish.
- **Models are API, not local** (DeepSeek-class churn, Claude top-tier deep solver). Local hardware runs sandboxes + services only.
- **Goal**: full clear of the online set. Hardware/RF/physical = human-led, agent-assisted.

---

## Topology

```
  Discord (category channels, thread-per-challenge)
        │  operator drops desc + files into a thread
        ▼
  ┌─────────────────┐
  │  Dispatcher     │  per-thread, lightweight. picks lane + mode + budget.
  └────────┬────────┘
           ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  LANE A — Research+Run     (solo, cheap)   CVE-PoC, ~5-7 challs    │
  │  LANE B — Category churn   (solo→race)     the bulk               │
  │  LANE C — Deep Solver      (heavy, top)    the hard tail          │
  │  LANE W — Windows box      (specialist)    anti-Wine / packed / kernel │
  │  LANE D — Raw escape hatch (no harness)    trivial                │
  └────────┬─────────────────────────────────────────────────────────┘
           │ every lane calls shared services through a resource governor
           ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Shared services (read-mostly; agents are clients, never pip into) │
  │  • persistent decompiler (ghidra/IDA, cached projects, concurrent) │
  │  • exploit-db mirror + searchsploit + nuclei                       │
  │  • GPU crack rig (hashcat/john on the 4060)                        │
  │  • egress proxy (SOCKS5h over SSH → on-site laptop; outbound only) │
  │  • resource governor (queues RAM hogs: ghidra/vol/angr/qemu/winVM) │
  │  • flag handler (candidate → thread → human submits)               │
  └──────────────────────────────────────────────────────────────────┘
       Per-challenge work runs in DISPOSABLE sandboxes (blow away after).
```

---

## Dispatch ladder (per thread)

Category is known (posted in a category channel), so the dispatcher just picks lane + mode + budget, then streams the agent's work into the thread.

1. **Parse** dropped desc + files; pull software/versions, file types, smells.
2. **Pick the lane** (table below).
3. **Run solo first** — most slop one-shots from helper-triage + one cheap model.
4. **Escalate to race** only if solo blows budget — 2-3 models on the same thread, first valid flag kills the rest. (Ladder it; don't race everything.)
5. **Deep Solver fires in parallel** on hard-tail smells — immediately, not after anyone gives up.
6. **Candidate flag → thread → human submits.**

### Dispatch decision table

| Smell | Lane | Mode |
|---|---|---|
| names software+version / known CMS / "exploit the service" | A — Research+Run | solo |
| binary to understand, logic checker | B — Rev | solo → race |
| overflow/fmt/vuln service binary | B — Pwn | solo → race |
| RSA/ECC/AES/encoding/cipher | B — Crypto | solo → race |
| webapp w/ source (static analysis) | B — Web (analysis) | solo |
| **live web exploitation needing box interaction** | **on-site tier** | on-site |
| disk/mem image, pcap, stego, file-carve | B — Forensics/Misc | solo (flat) |
| hash/zip/pdf/password to crack | B → GPU crack rig | solo |
| **anti-Wine / packed (VMProtect/Themida) / kernel-driver / .NET+Win32 dynamic** | **W — Windows box** | specialist |
| kernel pwn, custom-VM/arch reconstruction, exotic shellcode | **C — Deep Solver** | heavy, unattended + human glance |
| pure guess-the-theme / RF capture / physical | C best-effort + **human primary** | human-led |
| dead obvious (trivial strings/base64) | D — Raw | unharnessed codex/claude-code |

---

## Model tiers (all API)

Human is the global router — the highest-stakes routing decision is made by a person before any model runs.

- **Dispatch / fine-triage** — mid (Sonnet-class). Confirms lane, finds the concrete approach. Hard rule in its prompt: **search-first, never recall a CVE number from memory.**
- **Churn / execution** — cheap (DeepSeek-class). Safe to cheap out *because* the risky identification happened upstream. Diffuse token cost here: cheap model + digest-returning helpers (not extreme tool-calling).
- **Deep solver** — top model (Claude), max reasoning, big budget. Only the hard tail. **The one real cost center — give it a hard per-challenge token cap** or a runaway grind quietly burns $100.
- **Race pool** — mid + top, only on escalation.

Tune the cheap-tier choice empirically against the replay set (below).

---

## Sub-agents & the tool library

Organize the tool library as **Claude Code skills**: each category = a `SKILL.md` playbook + a `scripts/` dir of helpers. Drops straight into a claude-code-based agent with zero glue. **Crib heavily from `ljagiello/ctf-skills`** (SKILL.md-spec, all categories, deep technique coverage incl. custom-VM lifting, anti-debug, angr, VMProtect), plus technique scripts from `ByamB4/Common-CTF-Challenges` and `Crypto-Cat/CTF`. Don't reinvent.

> **Core principle:** anything a skilled human does the *same way every time* (first-pass recon) is a deterministic **script returning a digest**, not raw output the model wades through. That's simultaneously the token win and the don't-waste-tool-calls win. Raw output stays on disk to grep into if the digest flags something.

### LANE A — Research+Run (CVE-PoC, ~5-7 challs)
No bespoke pipeline needed. It's just: a capable agent + web search + a disposable sandbox + the box-proxy + a one-paragraph playbook ("ID software+version, search for a public PoC, run it against the box in your sandbox"). Cheap model. Solo.

### LANE B — Rev
- Tools: persistent decompiler, pwndbg, r2, z3.
- `triage_binary(path)` → file + checksec + filtered/flag-grepped strings + symbols + r2 `aa;afl` + packer/lang(rust/go/c)/arch detect → **compact digest** (protections, lang, func count, top interesting strings/symbols, entry, main). *(r2 `aaa` slow on big binaries — cap or punt to deep solver.)*
- `decompile(func)` → client to the persistent ghidra/IDA service; first call imports+analyzes+caches the project, rest instant.
- `crossverify(claim, addr)` → run under gdb, dump regs/mem/stack at a breakpoint to confirm the decompiler's claim. **Use whenever control flow is unclear — rust/obfuscated decomp is actively wrong, not just noisy.**

### LANE B — Pwn
- Tools: pwntools, checksec, cyclic, gdb/pwndbg, ROPgadget, decompiler.
- `checksec_and_offsets(path, crash?)` → protections + cyclic pattern + cyclic_find offset + GOT/PLT + win() if present.
- `test_exploit_locally(script, binary)` / `run_remote(script)` → local test in sandbox; remote fires through the proxy with flag-watch + timeout. **Gate: no remote fire until local passes** (don't burn attempts on bad offsets/endianness).

### LANE B — Crypto (dual-path)
- Tools: sage, z3, sympy, RsaCtfTool, pycryptodome.
- `sweep_encodings(blob)` → base64/32/85, hex, rot-N, single-byte xor, morse, atbash… returns only printable/flag-format hits. Replaces ~10 model flails with one call.
- `rsa_attacks(params)` → small-e / common-modulus / wiener / fermat / RsaCtfTool; reports which fired.
- Flow: sweep first → classify → known attacks → sage harness → write→run→interpret→review (no untested submissions).

### LANE B — Web (analysis only; live exploitation = on-site)
- Tools: requests, nuclei, curl, sqli/ssti probes, static source review.
- `recon(url)` → fetch + tech fingerprint + robots/sitemap + common paths.
- Remote tier does source review + builds the exploit; **on-site fires anything interactive.**

### LANE B — Forensics / Misc (flat, wide-tools)
- Tools: volatility3, sleuthkit, binwalk, foremost, exiftool, stego suite (steghide/zsteg/stegseek/zbar), EZ tools (MFTECmd/EvtxECmd/RECmd .NET Core), ffmpeg/sox.
- `triage_file(path)` → file + binwalk + foremost + exiftool + strings + image stego sweep, all at once; for images/disks suggest vol profile / sleuthkit entry → findings digest.
- Guessy ones → flag fast, don't grind.

### LANE W — Windows box (specialist fallback)
Rev defaults to Linux; the dispatcher routes here only on Wine-failure triggers.
- **Triggers**: anti-Wine detection; packed/protected (VMProtect/Themida/Enigma); kernel-mode/drivers; .NET dynamic debugging with Win32/COM interop; forensics needing VSS / native VHD-VHDX mount / Windows-GUI-only tools.
- **Form**: a **snapshottable Windows VM** (Hyper-V/VMware) on the PC — snapshot clean → agent gets free rein on the FS / detonates sketchy binaries → revert. Snapshots are what make free-rein safe. (Windows Sandbox for pure detonate-and-observe; the VM for anything needing persisted tooling or kernel work.)
- **Resource note**: ~6-8GB RAM, competes with ghidra → **run on-demand** (boot when a Windows-critical chall lands, shut after), governed by the queue. Disk ~100-200GB is fine.
- **MCP exposes**: powershell/cmd + filesystem + ideally an x64dbg/WinDbg bridge. Critical exactly when the environment is.

### LANE C — Deep Solver (hard tail)
A real agent on the hard challs, not an insta-handoff. Wins on *grindable*-hard; collaborates with a human on *insight*-hard.
- Top model, max reasoning, **big budget, runs long, unattended, in parallel.** Optionally race two framings (high payoff justifies cost).
- **Full** MCP kit (not curated-minimal): decompiler, debugger, **qemu+gdb-stub kernel debugging**, angr, z3. Breadth matters; the problem is novel.
- **External notebook**: a structured scratchpad it writes state to, so it doesn't lose the thread when context fills (the documented long-solve failure). Checkpoints summaries to the thread.
- **Human role**: not primary solver — periodic glance → redirect if grinding a dead path, take over on insight. Agent runs regardless.
- Targets: kernel pwn, MIPS/custom-arch checker reconstruction, exotic-constraint shellcode, VM reversing.

### LANE D — Raw escape hatch
Unharnessed codex/claude-code in the challenge dir. For dead-trivial challs where harness overhead > the challenge. First-class option, not a fallback.

---

## Resources & deployment (your actual hardware)

Models are API, so local hardware = sandboxes + services. The constraint isn't total compute, it's **spiky RAM hogs** (ghidra 2-4GB, volatility on big images multi-GB, angr, qemu, the Windows VM). **Build a resource governor that caps those to one-or-two-at-a-time and queues the rest** — that's higher value than more iron.

Allocation:
- **Main PC (32GB, RTX 4060, 300GB)** → workhorse: persistent decompiler service (ghidra ~8-16GB), **GPU cracking on the 4060** (hashcat/john — every comp has a few crack challs), the heavy-job queue (vol/angr/qemu serialized), the on-demand Windows VM, dispatcher, rev/pwn sandboxes.
- **2× laptops (8-12GB, 2020-era)** → pools of light, API-bound churn sandboxes (crypto, web-analysis, encoding sweeps, Research+Run). Also redundancy.
- **Deep solver** is the one strain (qemu+angr won't fit comfortably on a 2020 laptop): **one deep solve at a time, on the PC's headroom**, churn shuffled to laptops while it grinds. Fine — only 1-2 hard/cat.

**Don't local-host an LLM on the 4060** — 8GB VRAM runs nothing good enough, and DeepSeek API is cheaper than the electricity. The GPU is for cracking.

**Reliability**: consumer hardware full-tilt for 48h has a crash/thermal/sleep risk a VPS doesn't. Disable sleep, watch temps, have a "PC died → fail over to laptop" plan, and keep a spot/preemptible cloud instance bookmarked as break-glass.

**Cost**: dominated by tokens, not compute. Cheap churn model + digest helpers keep the bulk cheap; the deep solver (Claude) is the cost center — hard token cap per hard chall.

---

## Proxying (it's not bad, because the split moved the hard cases)

Remote tier's box contact = pull files once + occasional outbound probe / one-shot PoC. Pure outbound:
- `ssh -D 1080 laptop` → SOCKS5 on localhost. pwntools (`context.proxy=(SOCKS5,'127.0.0.1',1080)`), requests (`proxies={'http':'socks5h://127.0.0.1:1080'}`), curl all speak it natively.
- **Use socks5h (remote DNS)** so box hostnames resolve laptop-side if they only exist on the comp LAN.

The painful cases are **not the remote tier's job** — the split routed them to on-site direct access:
- **reverse callbacks** (reverse shells, OOB SSRF/XXE/blind) → on-site, listener on the comp LAN.
- **timing/latency-sensitive exploits** (heap races, tight windows) → on-site, local RTT.

So remote = one SOCKS forward, done.

---

## Build order

1. **Research+Run + egress proxy + exploit-db mirror.** ~5-7 challs, cheap, fast win.
2. **Helper-script skill library** per category (`triage_*` / `sweep_*` / `*_offsets`, digest-returning). Biggest reliability+cost lever — crib from `ljagiello/ctf-skills`.
3. **Category churn agents** (rev/pwn/crypto/web-analysis/forensics) on the solo path + shared decompiler + GPU crack rig.
4. **Resource governor + disposable-sandbox harness** (the thing that lets your hardware cope).
5. **Dispatcher + Discord glue** (thread open → lane pick → stream → FINDINGS/FLAG/NEEDS-HUMAN summary).
6. **Race escalation**, **Deep Solver lane** (full MCP kit + external notebook), **Windows VM lane** (snapshots + on-demand).
7. **Replay harness vs CDDC 2024/25** (NUS Greyhats posts full lists/writeups; structure it in NYU CTF Lite format w/ the `nyuctf` pypi loader for a clean eval loop). Tune cheap-tier model + helpers *before* the comp.

Steps 1-4 get you most of the clear. 5-6 are force-multipliers. 7 makes them work on the day.

---

## Scope: firm nos vs. deferred

**Firm nos (v1):**
- Autonomous global coordinator / platform poller — the 4 of you are the coordinator.
- Local LLM inference on the 4060 — API is cheaper and better; GPU is for cracking.
- A Windows box as the default — it's a specialist fallback; most rev stays Linux.
- Hard-coding category as a cage — containers are supersets; a miscategorized challenge re-routes. CTF categories lie.

**Deferred — not banned; leave seams, revisit when subtask complexity demands:**
- **Recursive subagent trees** (Squid-style). Overkill *now*, but the Deep Solver especially may grow them. Keep `Worker` composable so it can spawn sub-workers later.
- **Agent-to-agent coordination protocol** (CCP-style). Discord threads + status objects + voice cover 4 humans today; if agents start running unattended and need each other's partial work, this is where it goes. Keep `status`/findings queryable so they could be exposed agent-to-agent.
