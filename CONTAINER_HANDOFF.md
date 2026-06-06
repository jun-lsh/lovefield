# CDDC Supertool Interface - Handoff

For whoever's building the agent's **execution substrate + tool interface**
(the "supertool container"). The orchestration/control plane and a working agent
loop already exist - **your job is the tools and where they run**, not the agent
or the Discord wiring.

**Which tools to wire in (ghidra, gdb, decompilers, whatever) is your call /
TBD with [operator] later.** The requirement right now is just: stand up a
working tool interface, be able to **plug an LLM into it**, and have it **narrate
into Discord**. This doc shows you those three hookups so you can iterate freely.

Containerize if you want isolation (recommended - then untrusted binaries are
safe to run). On Windows, Docker Desktop's WSL2 backend gives a clean Linux FS;
bind-mount the challenge dir, no `/mnt/c` glue.

---

## The 3 hookups you actually need

### 1. Expose a tool the LLM can call - `cddc/tools.py`

Two pieces:
- `tool_specs() -> list[dict]`: OpenAI-format JSON schemas advertised to the model.
- `Toolbox.run(name, args: dict) -> str`: executes a call, returns the result.

**The only contract: return a short DIGEST string** (truncate big output - see
`_truncate`, 4000 chars; raw stays on disk to grep into), and **never raise**
(catch -> return `"tool error: ..."`). Add a tool = add a `_spec(...)` + a branch
in `run`. (Refactoring `run` into a `{name: handler}` registry is fine - just
keep the `run(name, args) -> digest` shape.) Note: `submit_flag` is handled in
the agent, not the toolbox - leave it.

```python
# advertise it
_spec("decompile", "Decompile a function to pseudo-C.",
      {"func": {"type": "string"}}, ["func"])
# handle it (return a digest, not a raw dump)
async def run(self, name, args):
    if name == "decompile":
        return _truncate(await self._decompile(args["func"]))
```

### 2. Plug an LLM into it - `cddc/models.py` + `cddc/agent_worker.py`

You almost certainly **don't need to write a loop** - `AgentWorker` already is
one: it asks a model for tool calls, runs them via the `Toolbox`, feeds results
back, and stops on `submit_flag`. It's **model-agnostic** via this protocol:

```python
class ModelClient(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply: ...
```

`DeepSeekClient` (OpenAI-compatible) is the real one; `FakeModel` returns scripted
replies for tests. So to drive YOUR tools with an LLM, you just add the tools
(hookup 1) and run the agent - the loop and model plumbing are done. If you ever
want a standalone harness, implement that one `chat` method and reuse everything
else.

### 3. Make it spit into Discord - `cddc/channel.py` + the worker

Narration flows through a tiny seam - the worker calls `post`:

```python
await self.channel.post("found the win() gadget at 0x401234")   # -> the thread
```

`AgentWorker` already narrates each model turn + tool call this way, so **your
tools' activity shows up in the thread for free**. Markers trigger pings:
`summary(..., flag=...)` produces a `CANDIDATE FLAG` block (halts for `!solved`/
`!continue`), `summary(..., needs_human=True)` a `NEEDS HUMAN` block - both ping
+ mirror to `#status`. To literally watch your interface drive Discord: set
`CDDC_WORKER=agent` (+ a model key) in `.env`, run `python -m cddc.bot`, and
`!start` a challenge - your tools narrate live into the thread.

`bot.py` is the **only** file that imports `discord`; everything else (tools,
agent, channel) is Discord-agnostic and testable without a token.

---

## Where your tools run (your call)

Today the tools run on the host in `_files/<thread_id>/` with no isolation
(`Toolbox._shell/_read/_write`). To containerize: introduce a `Sandbox` (suggest
`cddc/sandbox.py`) and route the tools through it -

```python
class Sandbox:
    async def start(self, thread_id, host_workdir): ...   # docker run -d, mount host_workdir:/work
    async def exec(self, cmd, timeout) -> str: ...        # docker exec, cwd /work, digest out
    async def teardown(self): ...
```

Gate it behind a config flag (`CDDC_SANDBOX = local | docker`, add to `config.py`
+ `.env.example`) so crypto/web keep working host-side without Docker. The
workdir is already `os.path.join(config.DOWNLOAD_DIR, str(thread_id))` (see
`dispatcher.py`, `kind="agent"`).

## Per-lane tool gating - `cddc/lanes/base.py`

`Lane.tools: tuple[str, ...]` exists and is empty. Populate it per lane and have
the agent filter `tool_specs()` to the lane's set, so e.g. rev gets your binary
tools and crypto doesn't. (Currently the agent offers all specs - wiring this
filter is part of the job whenever you add lane-specific tools.)

## Test without burning tokens

Drive the loop with `FakeModel` (see `scenario_agent` in `cddc/simulate.py`):
script replies that call your new tools and assert the toolbox returns a sane
digest. No model key, no Docker needed for logic tests (mock `Sandbox.exec`);
integration-test against a real container separately.

## Don't break (control-plane invariants)

- Only `bot.py` imports `discord`. Keep tools/agent/channel Discord-agnostic.
- Keep `Toolbox.run(name, args) -> digest str` and the `ModelClient` protocol.
- Honor the budget caps (`AGENT_MAX_STEPS`/`AGENT_MAX_TOKENS`) - a runaway loop
  must stay bounded.
- Keep the `CDDC_SANDBOX=local` path working (crypto/web shouldn't need Docker).
- Pure ASCII in source (Windows cp1252 console chokes on unicode/emoji).

## Map of what's already there

```
cddc/tools.py        tool_specs() + Toolbox.run  <- add tools here
cddc/models.py       ModelClient / DeepSeekClient / FakeModel  <- the LLM seam
cddc/agent_worker.py AgentWorker - the tool-loop (model-agnostic)
cddc/channel.py      Channel.post  <- narration into Discord
cddc/lanes/base.py   Lane.tools  <- per-lane tool gating seam
cddc/dispatcher.py   builds the agent worker (kind="agent", workdir)
cddc/bot.py          the ONLY discord import
cddc/simulate.py     FakeModel harness - test token-free
```
