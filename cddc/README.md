# CDDC 2026 Agent Stack - Phase 1

The human <-> orchestration **control plane**, proven end to end with dummy
workers (no real solving yet). An operator drops a challenge into a category
thread with `!start`; the bot routes it to a lane, spawns a Worker that streams
progress, answers `!status`, takes `!steer`, and obeys `!race`/`!pause`/
`!resume`/`!kill`/`!lane`. Phase 4 swaps the dummy loop for a real agent loop and
inherits all of this plumbing.

## Design rule (load-bearing)

**Discord lives only at the edges.** Nothing in `dispatcher / worker / lanes /
channel / registry` imports `discord` - they talk to a tiny `Channel` protocol
(`post` / `drain_steer` / `ask`). `bot.py` implements `Channel` against a Discord
thread; `simulate.py` implements it against the console. That's why the entire
control plane is testable with **zero token**.

```
challenge.py   Challenge dataclass (the unit of work; states; hard flag)
channel.py     Channel protocol + ConsoleChannel (sim impl)
lanes/         Lane = strategy/config (dummy_script now; tools/prompt later)
worker.py      Worker base + DummyWorker (the live loop) + summary()
registry.py    active workers per thread (a LIST - many agents per challenge)
dispatcher.py  pick lane -> build worker -> register -> run as a task
config.py      channel->category->lane maps, ignore/status channels, files dir
bot.py         the ONLY discord import: thread<->Channel + !commands
simulate.py    token-free harness (asserts the done-criteria)
```

## Run the simulation (no token)

```
python cddc/simulate.py
```

Proves: every category routes to the right lane; a worker streams scripted
progress; an injected steer shows up in the next progress post AND in
`status()`; the stall-ask escalates to `racing`; and pause/resume/kill take
effect.

## Run the bot (live)

1. `uv venv && uv pip install -r cddc/requirements.txt`
2. **Developer Portal -> Bot -> enable MESSAGE CONTENT INTENT** (privileged).
   Operators paste free-form challenge text + file drops, which slash commands
   can't capture - this intent is required, by design.
3. Invite the bot (OAuth2 -> scope `bot`) with: View Channels, Send Messages,
   Send Messages in Threads, Create Public Threads, Read Message History,
   Add Reactions, Embed Links, Attach Files, Mention Everyone.
4. `cp cddc/.env.example cddc/.env` and set `DISCORD_TOKEN`.
5. `python -m cddc.bot`

### Channels

Threads opened under a **category channel** become challenges. Drop channels:
`pwn rev crypto web forensics misc ai hardware research`. `general` is teammate
chat (ignored). `status` is the global feed - the bot posts an `@everyone`
alert + a link there on a **candidate flag** or **needs-human**. `windows` and
`deep_solver` have no drop channel (reached via `!lane` / escalation later).

## Commands

| Command | Effect |
|---|---|
| `!start <description>` (+ attachments) | Begin a challenge in this thread. Infers category from the channel, downloads attached distribution files to `_files/<thread_id>/`. The only way to start. |
| `!status` | Snapshot of every worker on the thread (state, step, budget, tried, steers). |
| `!steer <text>` | Fold a nudge into the worker(s). The ONLY path from a human to an agent - plain thread chatter never reaches it. Attachments on a `!steer` are added to the challenge files. `!steer @name <text>` targets one worker. |
| `!race` | Flip worker(s) to `racing`, or confirm a held race-ask (phase 1: a marker; real fan-out is phase 5). |
| `!solo` | The negative of `!race`: decline or undo a race, drop back to solo. Releases a held race-ask. |
| `!pause` / `!resume` | Idle / continue. |
| `!kill` | Stop and stand down. |
| `!lane <name>` | Cancel and reroute onto another lane, respawn. |
| `!solved` | Confirm a pending candidate flag: agents stand down, thread renamed `[SOLVED] ...`. |
| `!continue <why>` | Reject a pending candidate flag: the reason is folded in as a steer, agents re-open and re-derive. |
| `!help` | List all commands. |

Control commands take an optional `@name` to target one worker;
default is broadcast to all workers on the thread.

### Halts and pings

Halts hold **indefinitely** until a human acts - no timeouts, no silent
auto-continues. A candidate flag does **not** insta-kill racers; it halts the
thread for `!solved` / `!continue`. A stall holds at the race-ask for `!race` /
`!solo`. Any halt pings and mirrors a link into `#status`. `ALERT_MODE` controls
who: `user` pings `ALERT_USER_ID` only (testing); `everyone` pings `@everyone`
for flags/needs-human and `@here` for lighter halts.

### Pace

`CDDC_STEP_DELAY` (seconds, default 8) sets how long the dummy waits between
steps on the live bot - slow enough to catch a run and steer/kill/reroute it.
`simulate.py` runs fast regardless.

## Not in phase 1 (seams left, not built)

Real solving, the dashboard (phase 2), sandboxes/helpers + `web_search`
(phase 3), real agent loops + budgets (phase 4), race fan-out / deep solver
(phase 5), on-site webhook identity (phase 6), VPS file-pull for big uploads.
