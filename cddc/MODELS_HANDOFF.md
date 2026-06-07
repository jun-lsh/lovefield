# Handoff: agent brains for the CDDC stack

There are TWO brain tiers, and they share one substrate (the layered docker
image + per-challenge containers + the `skills/` toolchain + webhook reporting +
the Worker control plane). They differ only in how the "brain" runs.

```
                 ┌───────────────── shared substrate ─────────────────┐
                 │  layered docker image -> per-challenge container    │
                 │  skills/ playbooks + tools  •  Worker control plane │
                 │  webhook narration  •  Discord/local steering       │
                 └─────────────────────────────────────────────────────┘
   churn tier (API, chat loop)              deep tier (CLI harness)
   DeepSeek / Sonnet-API                    Claude Code / Codex
   = AgentWorker + ModelClient              = a Worker subclass wrapping the CLI
   the loop calls model.chat()              the CLI owns its OWN tool-loop
```

## Tier 2 - CLI harness (Claude Code / Codex) — THE PRIMARY GOAL

Claude Code and Codex are agent harnesses, not chat endpoints. They drive the
container + toolchain themselves. So they do NOT fit `ModelClient.chat()`.
Build a **`Worker` subclass** (suggest `cddc/cli_worker.py`,
`class CliAgentWorker(Worker)`) that:

- spawns the CLI (`claude` / `codex`) **inside the challenge's docker container**
  (reuse `cddc/sandbox.py` - the workdir is already bind-mounted), pointed at the
  challenge dir, with the `skills/` playbooks available to it.
- reuses the Worker control plane for free: `status` / `pause` / `resume` /
  `cancel`, the candidate-flag validation halt, and the escalation halt all live
  in the base `Worker`. You implement `run()`; you inherit the rest.
- **narrates via the Channel** - so it works with both `DiscordChannel` (my
  remote seat) and `WebhookChannel` (on-site teammates' own identity). Stream the
  CLI's progress out through `self._post(...)`; emit the candidate flag through
  the same `summary(...)` + candidate-halt path AgentWorker uses.
- **is steerable**: fold `self._collect_steers()` into the CLI between turns (or
  into its input stream if it's interactive). This is the novel bit - I need to
  steer the deep researchers from Discord (`!steer`), and on-site folks steer
  theirs locally (`WebhookChannel.push_steer`). Both arrive via the same
  `_collect_steers()`.
- parses the CLI's output for the candidate flag / a "stuck" signal.

Open decisions to settle before coding: which CLI flags give us
non-interactive-but-steerable execution; how to detect the candidate flag in CLI
output (structured output vs regex on the transcript); how the CLI authenticates
inside the container (mounted creds / API key env). **This worker shares almost
no code with the chat loop - it's additive, in its own file.**

## Tier 1 - churn API clients (DeepSeek done; add Sonnet-API / OpenAI)

The cheap churners ride the existing `AgentWorker` + `ModelClient` chat loop.
Adding a provider = a new client in `cddc/models.py` implementing:

```python
async def chat(self, messages: list[dict], tools: list[dict]) -> Reply
```

- `messages` are OpenAI-shaped dicts the loop OWNS (`system`/`user`/`assistant`
  w/ `tool_calls`/`tool`). Translate to your provider INSIDE the client, per
  call, statelessly - the loop's history stays OpenAI-shaped.
- `tools` is the OpenAI function-schema list, already filtered per-lane by
  `_specs_for_lane()`. Translate to your provider's tool schema.
- return `Reply(content, tool_calls=[ToolCall(id,name,arguments)], tokens)`.
  `tokens` MUST be populated (drives the budget cap); expose `self.model`; RAISE
  on failure (the loop retries once then halts).
- **`OpenAIClient`**: trivial - clone `DeepSeekClient`, swap base_url/model/key.
- **`AnthropicClient` (Sonnet churn)**: needs translation - `system` is a
  top-level param; tools use `input_schema`; assistant calls are `tool_use`
  blocks and results are `tool_result` blocks in a `user` message (not
  `role:"tool"`); tokens = `input_tokens + output_tokens`. Lazy-import the SDK.

config: `CDDC_PROVIDER=deepseek|openai|anthropic` + per-provider key/model; the
bot's `_model = ...` block (~bot.py:52) picks the client. `submit_flag` /
`triage_report` are agent-handled - just surface them as `tool_calls`.

## Division of labor (so we don't clash)

- **Tier 1 work** lives in `cddc/models.py` + a few `config.py` knobs + the
  `_model` block in `bot.py`. The `ModelClient` protocol is the seam.
- **Tier 2 work** is a NEW file (`cddc/cli_worker.py`) + its dispatch wiring.
- **Do not edit** `agent_worker.py` (the chat loop), `channel.py`, `worker.py`,
  or the escalation/help in `bot.py` - those are in flight and model-agnostic.

## Testing

Keep `python cddc/simulate.py` green (token-free, uses `FakeModel`). Tier-1
clients: unit-test the translation both ways. Tier-2 CLI worker: a fake-CLI
(echoes a scripted transcript incl. a flag) proves `run()` + steering + the
candidate halt without spending tokens, same spirit as `FakeModel`.
