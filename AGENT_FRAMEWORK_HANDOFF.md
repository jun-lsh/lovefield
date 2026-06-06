# CDDC 2026 Agent Stack — Build Handoff (for Claude Code)

You are building an agent fleet that auto-solves online CTF challenges for CDDC 2026, run by a 4-person team over Discord. Full architecture context is in **`cddc2026-agent-stack.md`** (read it first — it has the lanes, model tiers, resource plan, and the deferred/seams notes). This file is the **build plan**: what to build, in what order, and the decisions already locked so you don't re-derive them.

Build incrementally. Do **phase 1 only** until its done-criteria pass, then stop and let the human verify before moving on.

## Working agreement (read first — "Claire")

How to run this collab. Paste this into a fresh session along with the rest of the doc.

* **Who:** you are **claire**, the assistant. the user is the user, not claire. don't relitigate this.
* **Vibe:** lowercase casual. "yk / highkey / lowkey / icl" are fine. swear normally. no reddit voice. **never say "rnrn"** — they cringe at it.
* **Iterate fast.** "stop overthinking, just whack a possible solution and i'll try it." short responses, less hedging. they WILL paws you / tell you to chill when you over-think a thing they could just throw in and observe — **listen immediately.**
* **One clarifying question only when genuinely ambiguous.** otherwise state your read and move.
* **Match paws / ":3c"** when initiated.
* **Be honest about diminishing returns.** they'd rather hear "this is ineffective, skip it" than get rubber-stamped. several good calls came from pushing back — keep poking holes.

\---



\---

## Tech + conventions

* **Python 3.12+**, `discord.py` 2.x for the bot. `uv` or `venv` — your call, but pin deps in `requirements.txt`.
* Repo layout (suggested):

```
  cddc/
    bot.py            # discord wiring (thin): events/commands → control plane
    dispatcher.py     # routing logic (Discord-agnostic)
    challenge.py      # Challenge dataclass
    channel.py        # Channel protocol (post + drain\_steer + ask); console impl
    worker.py         # Worker base + DummyWorker (the live loop)
    registry.py       # active workers by thread\_id; commands route here
    lanes/
      \_\_init\_\_.py     # registry: category → lane config
      base.py         # Lane config/strategy (light)
    simulate.py       # local harness, NO discord needed
    config.py         # channel→category map, lane map, env
    .env.example
    requirements.txt
    README.md
  ```

* **Keep Discord at the edges.** Nothing in `dispatcher/worker/lanes/channel` imports `discord`. They talk to a `Channel` — `post(content)`, `drain\_steer() -> list\[str]` (non-blocking), `ask(prompt, timeout, default)`. `bot.py` implements `Channel` against a Discord thread; `simulate.py` implements it against console + scripted input. This is what lets the **whole control plane** (progress, polling, steering, control) be tested with zero Discord.
* **Keep v1 lean, but leave seams — defer, don't ban.** Recursive subagent trees and a coordination protocol are **deferred until subtask complexity demands them, not ruled out**. So: make `Worker` *composable* (it can spawn sub-workers later) and keep `status`/findings *queryable* (so they could be shared agent-to-agent later). Don't bake in assumptions that preclude either.

\---

## Skills to pull (phase 1 — scaffolding only)

Pull **one** skill to accelerate the discord.py layer; the orchestration logic is plain Python against this spec and needs no skill.

* **PULL:** `davila7/claude-code-templates` → the `discord-bot-architect` skill. Covers gateway intents, slash commands, components, rate limiting, sharding, and the footguns. Cloning the whole `claude-code-templates` repo also gives a general scaffolding library.
* **SKIP:** Discord *operation* skills (`idanbeck/discord-skill`, `Nice-Wolf-Studio/agent-discord-skills`, the anthropic discord plugin, openclaw). Those let an agent *operate* Discord as a tool — wrong layer. We are *writing* a bot, not driving one.
* **CTF technique skills (`ljagiello/ctf-skills`) are NOT for this phase** — they're for the individual agents (phase 3+, containers). Don't pull them yet.

**Override the skill's default advice on intents:** the skill will push "use slash commands, avoid the privileged message\_content intent." For this bot, **keep message\_content enabled** — operators paste free-form challenge text + drop file attachments into threads, which slash commands can't capture. Do **not** refactor the drop-a-challenge flow into slash commands. (Skill examples may be Pycord; we're on **discord.py 2.x** — mind the API differences.)

\---

## Agent behavior doctrine (the worker's operating style)

Seed of the phase-3 system prompt; in phase 1 the **dummy workers simulate it** (frequent updates + a stall→ask). Bake these into every worker:

* **Bias to action — whack something and observe.** Try the cheapest plausible approach *immediately*; don't over-plan or theorize in silence. Running beats reasoning about running.
* **Short loops.** Small cheap experiments → read output → adjust. No long silent stretches.
* **Narrate frequently, signal-dense.** Post on every *meaningful* change — new finding, approach switch, dead end, candidate flag — not every trivial tool call. The operator should always know what you're trying and what's next without having to `!status`.
* **Ask before rabbit-holing.** When stalling, STOP grinding and `ask()` the operator instead of silently burning budget: *"tried A/B/C for N steps, no traction — race it, try W, or steer me?"* Surface the dead end early.

**Stall heuristic** (tune later): trip the ask when (no new finding in \~3–5 steps) **OR** (same approach retried twice) **OR** (≥50% of budget spent with no candidate). The dummy worker's scripted `ask("race?")` decision point *is* this trigger in miniature — wire it to the heuristic so phase 4 inherits the behavior for free.

\---

## Phase 1 — Orchestration scaffolding + live control loop  ⬅ START HERE

**Goal:** an operator drops a challenge into a category thread; the bot routes it to a lane, spins a **Worker** running a (dummy) loop that **streams progress proactively**, answers **`!status` polls**, accepts **free-form steering mid-run**, and obeys **control commands** (race / pause / resume / kill / reroute). All exercised by dummy workers — no real solving yet. This phase builds and proves the **human↔orchestration control plane** so phase 4 just plugs real agent loops into it.

### Build

1. **`Challenge` dataclass** (`challenge.py`): `id, name, category, description, thread\_id, channel, files: list\[str], box: str|None, state: str, steers: list\[str]` (states: `new|dispatched|solving|racing|paused|candidate|needs\_human|solved|killed`).
2. **`Channel` protocol** (`channel.py`):

   * `async post(content)`, `drain\_steer() -> list\[str]` (non-blocking — returns + clears pending steering), `async ask(prompt, timeout, default) -> str`.
   * `ConsoleChannel` impl for simulate (prints; serves steers from a scripted queue; `ask` returns scripted/default).
3. **Lane config** (`lanes/base.py` + `\_\_init\_\_.py`): each category → a light lane = `{name, default\_mode, dummy\_script}`. Lanes are *strategy/config*; the Worker does the running. Lanes: `research\_run, rev, pwn, crypto, web, forensics, deep\_solver, windows, raw`.
4. **`Worker`** (`worker.py`) — `DummyWorker(lane, chall, channel)` with an `async run()` loop. Each iteration:

   * (a) `steers = channel.drain\_steer()` → fold in (append to `chall.steers`, post `🧭 adjusting for: …`, reflect in next step);
   * (b) advance one scripted step;
   * (c) post progress on **meaningful change only** (state change or ≥N seconds — rate-limited, not spammy);
   * (d) update `status()`;
   * (e) honor control flags (paused → idle; cancelled → clean exit).
   * At a scripted decision point, call `channel.ask("stuck after N steps — race 3 subagents? !race to confirm")` → proceed on confirm, auto-continue on timeout.
   * Methods: `status() -> dict` (state, current\_step, budget\_used, tried\[], steers\[]), `steer(msg)`, `race\_now()`, `pause()`, `resume()`, `cancel()`.
   * **Identity (seam for multi-agent):** each worker carries `id, name, location: "onsite"|"offsite", operator` — set at spawn. Used for in-thread attribution and dashboard registration. Off-site fleet workers default `location="offsite"`; this field is why on-site agents can join the *same* challenge cleanly in phase 6.
   * **Composable seam:** structure `run()` so a worker could later `await self.spawn(sub\_lane, subtask)` — don't preclude it.
5. **Registry** (`registry.py`): `active: dict\[thread\_id, list\[Worker]]` — **many agents per challenge**, not one. Dispatcher appends a started worker; commands operate over the list. In phase 1 there's usually one worker per thread, but model it as a list now so on-site agents (phase 6) join the same challenge without a retrofit. **Keep it dumb — realistic counts are 1–3 agents per challenge** (e.g. one on-site + one fleet churner for easy challs, a couple more for hard ones), so no scheduling/load-balancing/fairness machinery: just a short list. `!status` lists the 1–3 workers on the thread; `!steer`/`!pause`/`!kill` broadcast to all (or target one if `!steer @name …`).
6. **Dispatcher** (`dispatcher.py`): `pick\_lane(chall)` = category→lane map + `!lane` override + **`# TODO` smell hooks** (CVE/version→research\_run, Windows-PE/anti-wine→windows, trivial→raw — stubbed, not implemented). `dispatch()` → build Worker → register → `asyncio.create\_task(worker.run())`.
7. **Bot** (`bot.py`):

   * `commands.Bot`, **message\_content intent** (privileged — README must flag enabling it).
   * Category = thread's **parent channel name**.
   * `on\_thread\_create` / first message → build `Challenge` → `dispatch` (spawns Worker).
   * Commands routed to the thread's worker: `!steer <text>` (**the only steering path** — folds text into the worker's inbox; attachments on a `!steer` → append to `chall.files`), `!status`, `!race`, `!pause`, `!resume`, `!kill`, `!lane <name>` (cancel current + reroute + respawn), `!dispatch`.
   * **Plain messages in a thread are human discussion — the worker ignores them.** Only `!steer` reaches the agent. (Exception: the *first* message / starter is the challenge description, consumed at dispatch.)
   * Discord `Channel` impl: `post`→`thread.send`; `drain\_steer`→queue fed by `on\_message`; `ask`→post + `wait\_for` next message/reaction with timeout.
   * `summary(post, findings, flag=None, needs\_human=False)` helper → the `FINDINGS / CANDIDATE FLAG / NEEDS HUMAN` block.
8. **Local harness** (`simulate.py`): `ConsoleChannel` + a scripted timeline per fake challenge — inject steers at certain ticks, poll `!status`, trigger `!race`, `!pause`/`!resume`, `!kill`. Assert the worker reflects each. Runs via `python simulate.py`, **no token**.

### Done-criteria (phase 1)

* `python simulate.py`: every fake challenge routes correctly; a Worker streams scripted progress; an injected steer appears in the next progress post **and** in `status()`; `!race`/`!pause`/`!resume`/`!kill` each take effect — all with no Discord token.
* Real server: drop a challenge in `#rev` → bot spawns the rev dummy Worker, which posts progress; `!status` returns its snapshot; **`!steer <text>` steers it while plain discussion in the thread does NOT**; `!race` flips it to racing; `!kill` stops it; `!lane pwn` reroutes.
* Nothing in `dispatcher/worker/lanes/channel` imports `discord`.

**Stop here and let the human verify.**

\---

## Phase 2 — Dummy dashboard  (do this next)

Stand up the aggregate dashboard early, wired to the phase-1 dummies, so phases 3–5 are observable while you build them. Deliberately a **simple CRUD** — don't overbuild.

### Build

* **Service** (separate VPS later; localhost for now): FastAPI/Flask + SQLite. Two flat tables:

  * `challenges` (id, name, category, thread\_id, state, points)
  * `agents` (id, name, location, operator, current\_challenge\_id, status, last\_heartbeat)
* **Endpoints (plain CRUD):** `POST/GET/PATCH/DELETE /challenges`, `POST/PATCH/GET /agents`, and `GET /board` (challenges + per-challenge agent count + solve status). Live view via polling — websocket optional, not required.
* **Wire to phase-1 dummies:** the dummy Worker PATCHes its `agents` row each loop (status/heartbeat); the bot POSTs a `challenges` row on thread-create and PATCHes state on candidate/solved. Agent-count-per-challenge = `count(agents where current\_challenge\_id == X)` — trivial, no assignment engine.
* **Keep it dumb:** counts are 1–3 agents per challenge, so no aggregation logic beyond a `COUNT`. The dashboard *reads* state; it doesn't allocate or schedule (humans do that).
* **Mirror, not master:** if the dashboard is down, the dummy keeps looping and narrating to its thread — status writes just queue + retry. Don't make worker progress depend on the dashboard being up.

**Done:** drop two fake challenges, spawn a dummy worker on each, and `GET /board` shows both challenges, their agent counts, and live status that updates as the dummies progress — with the bot still fully functional if you kill the dashboard process.

\---

## Phase 3 — Containers + services (sketch)

* **Container model — NOT per-category.** Build **one shared superset base image** (radare2/gdb/pwntools/angr, sage/z3/RsaCtfTool, volatility3/sleuthkit/binwalk/stego suite, requests/nuclei). Spin a **disposable container per *challenge*** from it (isolation: sketchy installs + untrusted binaries don't bleed across challenges or to the host; blown away after). **Category = which helper scripts + prompt the worker loads, not a different image** — so `!lane` reroute just swaps the active helper set in the same container. Per-challenge containers are cheap: they share the base image's copy-on-write layers, only running-tool RAM counts (the governor caps it). Crib tooling + technique scripts from `ljagiello/ctf-skills` (SKILL.md format), `ByamB4/Common-CTF-Challenges`, `Crypto-Cat/CTF`.
* **One justified split — by *tier*, not category:** a separate, heavier, **privileged** deep-solver image (KVM/qemu/kernel-debug, angr) for LANE C. You don't want every churn container privileged just for the rare kernel-pwn.
* **Persistent decompiler service** (ghidra/IDA headless, cached projects, concurrent clients).
* **Disposable per-challenge sandbox** pattern (agent installs/runs sketchy stuff here; blown away after; never touches shared services).
* **Resource governor**: a queue that caps RAM hogs (ghidra / volatility / angr / qemu / windows-VM) to 1–2 concurrent. *This is the keystone that makes the home rig viable — don't skip it.*
* Helpers are **digest-returning** (not raw dumps): `triage\_binary`, `decompile`, `crossverify`, `checksec\_and\_offsets`, `test\_exploit\_locally`/`run\_remote`, `sweep\_encodings`, `rsa\_attacks`, `triage\_file`. Organize as `SKILL.md` + `scripts/` per category.

**Done:** a lane can spin a sandbox, run one helper, get a digest back.

\---

## Phase 4 — Per-challenge "slave driver" (sketch)

* Swap each `DummyWorker`'s scripted loop for a **real agent loop** — triage → cheap model (DeepSeek-class) → tool loop using the helpers → candidate flag → `summary(...)`. The control plane (progress / `status` / steer / race / pause / kill) **already works from phase 1**, so you're only replacing the per-step logic, not the plumbing. Steering = the human's `!steer` messages get folded into the agent's context at each `drain\_steer()`.
* Model API wiring: cheap churn model for lanes A/B; **hard token-budget cap** baked in.
* The driver owns the **budget** (iterations / tokens / wall-clock) and the state machine.
* Box-facing tool calls go through the **SOCKS5h proxy** (`socks5h` for remote DNS). Reverse-callback / timing-sensitive work is out of scope here (on-site handles it).

**Done:** one real (replayed 2024/25) challenge solved end-to-end by a single cheap agent on the solo path.

\---

## Phase 5 — Racing meta-strategy (sketch)

* **Escalation ladder**: solo blows budget → race 2–3 models on the same thread → first valid flag **cancels the rest**.
* **Deep Solver** lane fires in **parallel** on hard-tail smells (top model, full MCP kit, external notebook, big budget), human glances periodically.
* Cross-agent insight sharing = posts in the thread (no separate protocol).
* Decide per-lane whether to race or stay solo based on day-1 token burn.

**Done:** a stalled solo auto-escalates to a race and the first valid flag cancels the other racers.

\---

## Phase 6 — On-site agent unification (sketch)

Bring on-site player agents onto the same rails as the fleet. The dashboard (phase 2) and the control plane (phase 1) already exist; this phase adds the **identity/binding plumbing** so a locally-spawned agent joins a challenge cleanly. On-site *real* agents arrive once phase 4's loop exists (an on-site agent = a phase-4 worker spawned locally + self-bind).

**Unification principle:** every agent — on-site or fleet — speaks the same two interfaces: a **Discord thread** (narration + receives `!steer`) and the **dashboard API** (status + registry). They differ only in spawn (local player vs dispatcher) and network path (direct vs SOCKS proxy).

* **In-thread identity via webhooks.** Each agent narrates through its own Discord **webhook** (own display name/avatar, `?thread\_id=` to hit the thread) — no per-agent bot token. The central bot keeps owning commands + dispatch + dashboard sync; agents narrate out via webhook, receive steers in via the bot relaying `!steer`.
* **On-site self-bind flow:** mark the allocation (in the dashboard, or the agent self-registers) → agent reads the challenge's `thread\_id` from the dashboard → posts *"🧑‍💻 onsite/<operator> bound — working"* into the thread → runs as a normal worker, heartbeating to its `agents` row. Fleet agents can be on the same challenge in parallel; **first valid flag from any agent wins**, the rest stand down.
* **Keep treatment uniform and simple:** an on-site agent is just another entry in the challenge's worker list — same `status`/`steer`/`kill`. No special-casing beyond `location` and network path. Counts stay 1–3, so nothing fancy.

**Done:** an on-site agent (even a dummy) self-binds to an existing challenge thread, posts under its own webhook identity, shows up alongside a fleet agent on the same challenge in `GET /board`, and a confirmed flag flips the challenge to solved everywhere.

\---

## Replay harness (do alongside phase 4)

CDDC 2024/25 challenge lists + writeups are partly public (NUS Greyhats). Structure replays in **NYU CTF Lite format** (`nyuctf` pypi loader) for a clean eval loop. Use it to pick the cheap-tier model and tune helpers **before** the comp.

\---

## Locked decisions (don't re-litigate)

* 4 human operators are the coordinator; **no autonomous platform poller**.
* **All agents are unified (on-site + fleet):** same two interfaces — a Discord thread (narration via per-agent webhook + `!steer`) and the dashboard API (status/registry). They differ only in spawn + network path. Many agents can work one challenge; **first valid flag wins**, rest stand down.
* **The dashboard (VPS) is a shared registry + aggregate view, and a mirror not a master** — Discord stays primary; agents keep working if the VPS wobbles.
* **The control plane is bidirectional and core (built in phase 1):** workers push progress, humans poll (`!status`), steer (explicit `!steer <text>` — plain thread discussion never reaches the agent), and control (`!race`/`!pause`/`!resume`/`!kill`/`!lane`). A worker is never a fire-and-forget black box.
* **Recursive subagents + coordination protocol are deferred, not banned** — leave seams (composable `Worker`, queryable `status`/findings). Cross that bridge when subtask complexity demands it.
* Lanes route by category but **category is not a cage** — every container is a superset, `!lane` reroutes.
* Models are **API** (DeepSeek-class churn, Claude top-tier deep solver). **No local LLM on the 4060** — that GPU is for hashcat/john cracking.
* Hard challs get a **real unattended agent** (Deep Solver), not an insta-handoff to a human; human is a periodic-glance collaborator.
* Windows is a **specialist on-demand VM lane** (snapshots = safe free-rein), not the default; most rev stays Linux.
* Remote tier is outbound-only to boxes; **on-site owns interactive web/pwn, callbacks, timing**.

