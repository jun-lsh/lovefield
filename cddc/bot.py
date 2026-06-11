"""Discord wiring (thin) - the ONLY module that imports `discord`.

Maps a Discord thread onto the Channel protocol and routes `!commands` to the
thread's worker(s). Everything it touches in dispatcher/worker/lanes/channel is
Discord-agnostic, so the whole control plane is testable via simulate.py.

Run:  python -m cddc.bot     (needs DISCORD_TOKEN in cddc/.env)

Footguns honoured (from the discord-bot-architect skill):
  - MESSAGE CONTENT INTENT is enabled (operators paste free-form text + drops).
  - Never block the gateway: workers run as asyncio tasks (dispatcher does this).
  - Never hardcode tokens: token comes from the env / .env only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import pathlib
import re
from urllib.parse import urlparse

import discord
from discord.ext import commands

# Operational logs to stdout (separate from the agent's Discord narration) so the
# operator's terminal shows a heartbeat - model-call timing, tool timing, spawns -
# and you can tell "stuck" from "thinking". discord.py's own logs stay at WARNING.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname).1s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("discord").setLevel(logging.WARNING)
_log = logging.getLogger("cddc.bot")

from .challenge import Challenge
from .config import (
    AGENT_PROVIDER,
    ALERT_MODE,
    ALERT_USER_ID,
    ANTHROPIC_API_KEY,
    ANTHROPIC_BASE_URL,
    CATEGORY_LANE,
    CDDC_SANDBOX,
    CHURN_MODEL,
    CHURN_THINKING,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CODEX_MODEL,
    DEEP_KIND,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DISABLE_HANDOFF,
    DOWNLOAD_DIR,
    ESCALATION_BUDGET_MULT,
    FETCH_MAX_MB,
    HARNESS_CLI,
    HARNESS_SUMMARIZE,
    IGNORE_CHANNELS,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    SPECIALIST_KIND,
    SPECIALIST_MODEL,
    STATUS_CHANNEL,
    WORKER_KIND,
    category_for_channel,
)
from .dispatcher import Dispatcher
from .lanes import LANES
from .models import ClaudeClient, CodexClient, DeepSeekClient
from .registry import Registry
from .worker import append_dossier

# .env is loaded by config.py (imported above) before its env reads.

registry = Registry()
dispatcher = Dispatcher(registry)

# Build the model client once if we're in agent mode and the selected provider's
# key is present; otherwise fall back to dummy workers (scripted, no key/cost).
def _build_model():
    if AGENT_PROVIDER == "claude":
        if not ANTHROPIC_API_KEY:
            print("CDDC_PROVIDER=claude but ANTHROPIC_API_KEY is empty -> using dummy workers")
            return None
        return ClaudeClient(
            ANTHROPIC_API_KEY, CLAUDE_MODEL,
            base_url=ANTHROPIC_BASE_URL or None, max_tokens=CLAUDE_MAX_TOKENS,
        )
    if AGENT_PROVIDER == "codex":
        if not OPENAI_API_KEY:
            print("CDDC_PROVIDER=codex but OPENAI_API_KEY is empty -> using dummy workers")
            return None
        return CodexClient(OPENAI_API_KEY, CODEX_MODEL, base_url=OPENAI_BASE_URL)
    # default: deepseek
    if not DEEPSEEK_API_KEY:
        print("CDDC_PROVIDER=deepseek but DEEPSEEK_API_KEY is empty -> using dummy workers")
        return None
    return DeepSeekClient(
        DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, CHURN_MODEL, thinking=CHURN_THINKING
    )


_model = _build_model() if WORKER_KIND == "agent" else None

# The escalation tier (specialist / deep / race) can run a STRONGER DeepSeek than
# triage's flash - see brain-cost doctrine. Only meaningful for the deepseek
# provider with a distinct model; otherwise it reuses the triage model (and the
# "harness" kind builds its own CLI agent, ignoring this).
if (
    _model is not None
    and AGENT_PROVIDER == "deepseek"
    and DEEPSEEK_API_KEY
    and SPECIALIST_MODEL != CHURN_MODEL
):
    _specialist_model = DeepSeekClient(
        DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, SPECIALIST_MODEL, thinking=CHURN_THINKING
    )
else:
    _specialist_model = _model

# Resolve the dispatch kind once. "harness" runs the CLI agents in tmux (no
# ModelClient); "agent" needs a built model; otherwise dummy.
if WORKER_KIND in ("harness", "cc"):
    _KIND = WORKER_KIND  # CLI-driven (tmux harness, or headless Claude Code) - no ModelClient
elif _model is not None:
    _KIND = "agent"
else:
    _KIND = "dummy"

# The ESCALATION tier (separate from triage): !escalate respawns THIS kind. The
# two-tier bridge - cheap triage escalates to a real specialist. Defaults to a
# light DeepSeek specialist now; "harness" makes it the Claude Code box agent. In
# dummy mode there's nothing real to escalate to, so it stays dummy.
_SPECIALIST_KIND = _KIND if _KIND == "dummy" else SPECIALIST_KIND
# The DEEP tier brain (what `!escalate deep` respawns): the real top of the ladder,
# default the Claude Code harness. Stays dummy in dummy mode (nothing real to run).
_DEEP_KIND = _KIND if _KIND == "dummy" else DEEP_KIND
# An "agent" tier needs a DeepSeek ModelClient. If we never built one (e.g. WORKER_KIND
# = cc/harness, which are CLI-driven), an agent specialist/deep would crash on
# model.chat - so fall back to the triage kind, which needs no ModelClient.
if _SPECIALIST_KIND == "agent" and _specialist_model is None:
    _log.warning("specialist kind 'agent' has no model (worker=%s) -> using '%s'", WORKER_KIND, _KIND)
    _SPECIALIST_KIND = _KIND
if _DEEP_KIND == "agent" and _specialist_model is None:
    _DEEP_KIND = _KIND

_log.info(
    "tiers: triage=%s specialist=%s | provider=%s triage_model=%s specialist_model=%s sandbox=%s",
    _KIND, _SPECIALIST_KIND, AGENT_PROVIDER, CHURN_MODEL,
    getattr(_specialist_model, "model", "?"), CDDC_SANDBOX,
)
if _KIND == "agent" and CDDC_SANDBOX != "docker":
    # run_shell runs on the HOST with no isolation - the model-driven shell can
    # read/touch your whole filesystem. Fine for trusted crypto/web solving, scary
    # for untrusted binaries. Set CDDC_SANDBOX=docker to confine it to /challenge.
    _log.warning(
        "CDDC_SANDBOX=%s -> run_shell runs on the HOST (no isolation; full filesystem "
        "access). Set CDDC_SANDBOX=docker to confine the agent to the container.",
        CDDC_SANDBOX,
    )

# Cheap model that narrates the CLI harness's noisy TUI into clean 1-line Discord
# updates. Always DeepSeek (cheap) regardless of the harness's own provider; None
# -> the harness posts cleaned-but-raw screen deltas instead.
_summarizer = None
if (_KIND == "harness" or _SPECIALIST_KIND == "harness") and HARNESS_SUMMARIZE:
    if DEEPSEEK_API_KEY:
        # Summarizer stays NON-thinking - it writes a 1-line narration, reasoning
        # tokens would just burn cost for no gain.
        _summarizer = DeepSeekClient(
            DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, CHURN_MODEL, thinking=False
        )
    else:
        print("CDDC_HARNESS_SUMMARIZE=1 but DEEPSEEK_API_KEY is empty -> harness posts raw output")

intents = discord.Intents.default()
intents.message_content = True  # PRIVILEGED - enable it in the Developer Portal
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=discord.AllowedMentions.none(),  # no accidental pings
    help_command=None,  # we ship our own !help (the default is noisy)
)


async def _candidate_hook(worker, flag: str) -> None:
    """Fired when a worker emits a candidate flag: HALT the thread for validation.

    A flag does not insta-kill racers - it pauses every other agent on the
    challenge and waits for the operator's verdict (!solved / !continue).
    """
    thread_id = worker.chall.thread_id
    for w in registry.workers(thread_id):
        if w is not worker and w.chall.state not in ("candidate", "solved", "killed"):
            w.pause()
    await worker.channel.post(
        "[halted] candidate flag pending validation - all agents on this "
        "challenge are paused. reply `!solved` to confirm (renames the thread) "
        "or `!continue <why it might be wrong>` to re-open and keep going."
    )


class DiscordChannel:
    """Channel impl bound to one Discord thread.

    Steering arrives via the `!steer` command (-> worker inbox), so drain_steer
    returns nothing here; the protocol stays satisfied and the worker code is
    identical to sim. Any HALT (a worker blocked awaiting a human) pings + posts
    to #status: candidate-flag / needs-human are @everyone, lighter "decision
    needed" halts (the race-ask) are @here. Both mirror with a link back.
    """

    def __init__(self, thread: discord.Thread, bot: commands.Bot) -> None:
        self.thread = thread
        self.bot = bot

    @staticmethod
    def _severity(content: str) -> str | None:
        """'big' for candidate-flag / needs-human, 'halt' for a decision-ask."""
        # a verified flag, a working-but-blocked local solve, or a hard stuck =
        # the big-alert tier (same urgency: the operator must act now)
        if ("CANDIDATE FLAG" in content) or ("LOCAL SOLVE" in content) or ("NEEDS HUMAN" in content):
            return "big"
        # any worker question / triage report = a halt awaiting a human decision
        if ("[ask]" in content) or ("TRIAGE REPORT" in content):
            return "halt"
        return None

    async def post(self, content: str) -> None:
        sev = self._severity(content)
        prefix, allowed = _alert(sev)
        body = content if prefix is None else f"{prefix} {content}"
        for chunk in _chunk(body, 1990):
            await self.thread.send(chunk, allowed_mentions=allowed)
        if sev is not None:
            await self._mirror_to_status(content, sev)

    def drain_steer(self) -> list[str]:
        return []  # steering comes via the !steer command -> worker inbox

    async def ask(self, prompt: str, timeout: float = 60.0, default: str = "") -> str:
        await self.post("[ask] " + prompt)

        def check(m: discord.Message) -> bool:
            return m.channel.id == self.thread.id and not m.author.bot

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=timeout)
            return msg.content
        except asyncio.TimeoutError:
            return default

    async def _mirror_to_status(self, content: str, sev: str) -> None:
        guild = self.thread.guild
        if guild is None:
            return
        status = discord.utils.get(guild.text_channels, name=STATUS_CHANNEL)
        if status is None:
            return
        if "CANDIDATE FLAG" in content:
            kind = "CANDIDATE FLAG"
        elif "LOCAL SOLVE" in content:
            kind = "LOCAL SOLVE (needs remote)"
        elif "NEEDS HUMAN" in content:
            kind = "NEEDS HUMAN"
        elif "TRIAGE REPORT" in content:
            kind = "TRIAGE REPORT"
        else:
            kind = "DECISION NEEDED"
        prefix, allowed = _alert(sev)
        lead = f"{prefix} " if prefix else ""
        await status.send(
            f"{lead}[{kind}] in {self.thread.mention} - {self.thread.jump_url}",
            allowed_mentions=allowed,
        )


def _alert(sev: str | None):
    """Render a halt severity into (mention_prefix, AllowedMentions).

    ALERT_MODE=user  -> ping ALERT_USER_ID only (testing, the default for now).
    ALERT_MODE=everyone -> @everyone for 'big' halts, @here for 'halt'.
    """
    if sev is None:
        return None, discord.AllowedMentions.none()
    if ALERT_MODE == "user" and ALERT_USER_ID:
        return f"<@{ALERT_USER_ID}>", discord.AllowedMentions(
            everyone=False, users=True, roles=False
        )
    prefix = "@everyone" if sev == "big" else "@here"
    return prefix, discord.AllowedMentions(everyone=True)


def _chunk(s: str, n: int) -> list[str]:
    return [s[i : i + n] for i in range(0, len(s), n)] or [""]


# --- events --------------------------------------------------------------
@bot.event
async def on_ready() -> None:
    print(f"logged in as {bot.user} - {len(bot.guilds)} guild(s)")


# No on_message override: commands.Bot dispatches `!commands` natively, and
# plain messages are simply ignored - the ONLY way to start a challenge is the
# explicit !start command, and the only way to reach a worker is !steer. There
# is no path from ambient thread chatter to an agent.


# --- file acquisition ----------------------------------------------------
_URL_RE = re.compile(r"https?://\S+")


async def _acquire_files(
    message: discord.Message, thread_id: int
) -> tuple[list[str], list[str]]:
    """Pull distribution files for a challenge.

    Discord attachments (the common case - box files dropped into the thread)
    are downloaded to `_files/<thread_id>/`. http(s) links in the text are
    recorded but NOT fetched yet (rare: a file-share upload).

    TODO (later, big-uploads only): pull from the VPS upload endpoint. Add a
    `cddc-vps://<id>` scheme here and fetch it to the same dir.
    """
    dest = pathlib.Path(DOWNLOAD_DIR) / str(thread_id)
    local: list[str] = []
    if message.attachments:
        dest.mkdir(parents=True, exist_ok=True)
        for a in message.attachments:
            path = dest / a.filename
            await a.save(path)
            local.append(str(path))
    links = _URL_RE.findall(message.content)
    return local, links


# --- commands ------------------------------------------------------------
def _parse_target(text: str) -> tuple[str | None, str]:
    """`@name rest` -> ('name', 'rest'); otherwise (None, text)."""
    if text.startswith("@"):
        first, _, rest = text.partition(" ")
        return first[1:], rest.strip()
    return None, text


def _clean_target(target: str | None) -> str | None:
    return target[1:] if target and target.startswith("@") else target


@bot.command(name="status")
async def cmd_status(ctx: commands.Context) -> None:
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no workers on this thread")
        return
    blocks = []
    for w in workers:
        s = w.status()
        race = " [racing]" if s["racing"] else ""
        tried = ", ".join(s["tried"][-3:]) or "-"
        steers = "; ".join(s["steers"]) or "-"
        blocks.append(
            f"**{s['name']}** [{s['location']}] lane=`{s['lane']}` "
            f"state={s['state']}{race} step={s['current_step']} "
            f"budget={s['budget_used']}\n  tried: {tried}\n  steers: {steers}"
        )
    for chunk in _chunk("\n".join(blocks), 1990):
        await ctx.send(chunk)


@bot.command(name="trace")
async def cmd_trace(ctx: commands.Context, target: str | None = None) -> None:
    """Dump a worker's full message+tool trace to a file and upload it."""
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no worker here")
        return
    target = _clean_target(target)
    dest = pathlib.Path(DOWNLOAD_DIR) / str(ctx.channel.id)
    dest.mkdir(parents=True, exist_ok=True)
    sent = 0
    for w in workers:
        if target and w.name != target:
            continue
        text = w.trace_text()
        path = dest / f"trace_{w.name}.txt"
        path.write_text(text, encoding="utf-8")
        await ctx.send(
            f"trace for **{w.name}** ({len(text)} chars)",
            file=discord.File(str(path)),
        )
        sent += 1
    if not sent:
        await ctx.send(f"no worker named `{target}` here")


@bot.command(name="files")
async def cmd_files(ctx: commands.Context, *, arg: str = "") -> None:
    """Files in this thread's agent workdir (_files/<id>/).

      !files              - list AND upload all
      !files list         - list only (no upload)
      !files a.txt, b.bin - upload just those, by name
    """
    dest = pathlib.Path(DOWNLOAD_DIR) / str(ctx.channel.id)
    paths = sorted(p for p in dest.iterdir() if p.is_file()) if dest.exists() else []
    if not paths:
        await ctx.send("no files in this thread's workdir yet")
        return

    arg = arg.strip()
    limit = 24 * 1024 * 1024  # stay under Discord's upload cap

    # --- list only ---
    if arg.lower() == "list":
        lines = [f"**files in workdir** ({len(paths)}):"]
        lines += [f"  - `{p.name}` ({p.stat().st_size} B)" for p in paths]
        for chunk in _chunk("\n".join(lines), 1990):
            await ctx.send(chunk)
        return

    # --- pull by name ---
    if arg:
        wanted = [n.strip() for n in arg.replace(",", " ").split() if n.strip()]
        by_name = {p.name: p for p in paths}
        sendable, missing, toobig = [], [], []
        for n in wanted:
            p = by_name.get(n)
            if p is None:
                missing.append(n)
            elif p.stat().st_size > limit:
                toobig.append(n)
            else:
                sendable.append(p)
        if sendable:
            await ctx.send(files=[discord.File(str(p)) for p in sendable[:10]])
        notes = []
        if missing:
            notes.append("not found: " + ", ".join(missing))
        if toobig:
            notes.append("too big: " + ", ".join(toobig))
        if len(sendable) > 10:
            notes.append(f"capped at 10 (you asked for {len(sendable)})")
        if notes:
            await ctx.send(" | ".join(notes))
        return

    # --- default: list AND upload all ---
    lines = [f"**files in workdir** ({len(paths)}):"]
    sendable, skipped = [], []
    for p in paths:
        lines.append(f"  - `{p.name}` ({p.stat().st_size} B)")
        if p.stat().st_size <= limit and len(sendable) < 10:
            sendable.append(p)
        else:
            skipped.append(p.name)
    for chunk in _chunk("\n".join(lines), 1990):
        await ctx.send(chunk)
    if sendable:
        await ctx.send(files=[discord.File(str(p)) for p in sendable])
    if skipped:
        await ctx.send("not uploaded (too big or >10 cap): " + ", ".join(skipped))


@bot.command(name="steer")
async def cmd_steer(ctx: commands.Context, *, text: str = "") -> None:
    target, text = _parse_target(text)
    files = [a.url for a in ctx.message.attachments]
    if not text and not files:
        await ctx.send("usage: !steer <text>  (or: !steer @name <text>)")
        return

    def apply(w):
        if text:
            w.steer(text)
        if files:
            w.chall.files.extend(files)

    hit = registry.broadcast(ctx.channel.id, apply, target=target)
    await ctx.send(f"steered {len(hit)} worker(s)" if hit else "no worker to steer here")


@bot.command(name="race")
async def cmd_race(ctx: commands.Context, target: str | None = None) -> None:
    hit = registry.broadcast(ctx.channel.id, lambda w: w.race_now(), target=_clean_target(target))
    await ctx.send(f"racing {len(hit)} worker(s)" if hit else "no worker here")


@bot.command(name="solo", aliases=["norace"])
async def cmd_solo(ctx: commands.Context, target: str | None = None) -> None:
    """Decline / undo a race - the negative of !race. Releases a held race-ask."""
    hit = registry.broadcast(ctx.channel.id, lambda w: w.go_solo(), target=_clean_target(target))
    await ctx.send(f"staying solo - {len(hit)} worker(s)" if hit else "no worker here")


@bot.command(name="pause")
async def cmd_pause(ctx: commands.Context, target: str | None = None) -> None:
    hit = registry.broadcast(ctx.channel.id, lambda w: w.pause(), target=_clean_target(target))
    await ctx.send(f"paused {len(hit)} worker(s)" if hit else "no worker here")


@bot.command(name="resume")
async def cmd_resume(ctx: commands.Context, target: str | None = None) -> None:
    hit = registry.broadcast(ctx.channel.id, lambda w: w.resume(), target=_clean_target(target))
    await ctx.send(f"resumed {len(hit)} worker(s)" if hit else "no worker here")


@bot.command(name="kill")
async def cmd_kill(ctx: commands.Context, target: str | None = None) -> None:
    hit = registry.broadcast(ctx.channel.id, lambda w: w.cancel(), target=_clean_target(target))
    for w in hit:
        registry.remove(ctx.channel.id, w)
    # Drop the shared per-challenge box (#12) only when NO worker remains - killing
    # one racer by name must not pull the container out from under the others.
    released = False
    if not registry.workers(ctx.channel.id):
        await registry.release_box(ctx.channel.id)
        released = True
    if not hit:
        await ctx.send("no worker here")
    else:
        await ctx.send(f"killing {len(hit)} worker(s)" + (" + released the box" if released else ""))


@bot.command(name="triage")
async def cmd_triage(ctx: commands.Context, target: str | None = None) -> None:
    """Force the agent(s) to STOP and file a triage report (.cddc/triage.md), instead
    of grinding on a solve. For the cc worker this interrupts the live turn and resumes
    it with the triage instruction (stop-and-steer)."""
    from .cc_worker import FORCE_TRIAGE_STEER

    hit = registry.broadcast(
        ctx.channel.id, lambda w: w.steer(FORCE_TRIAGE_STEER), target=_clean_target(target)
    )
    await ctx.send(f"forcing a triage on {len(hit)} worker(s)" if hit else "no worker here")


@bot.command(name="lane")
async def cmd_lane(ctx: commands.Context, name: str = "") -> None:
    if name not in LANES:
        await ctx.send(f"unknown lane `{name}`; known: {', '.join(sorted(LANES))}")
        return
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no worker here to reroute")
        return
    # Cancel current workers and respawn on the new lane. Use a fresh Challenge
    # copy so the cancelled workers (which flip their copy to 'killed' on exit)
    # don't clobber the new worker's state.
    old = workers[0]
    new_chall = dataclasses.replace(old.chall, state="dispatched")
    channel = old.chall.channel
    for w in workers:
        w.cancel()
        registry.remove(ctx.channel.id, w)
    worker = await dispatcher.dispatch(new_chall, channel, lane_override=name)
    await ctx.send(f"rerouted to lane `{name}` - {worker.name}")


def _deep_model_for(sel: str) -> str:
    """Map a `!escalate deep <sel>` model selector to a model id, or "" for the plan
    default. Lets you fall back to Opus 4.6 if 4.8 refuses on cyber-capability grounds
    mid-analysis: `!escalate deep 4.6`. A raw `claude-...` id is passed through."""
    s = (sel or "").lower().lstrip("v")
    if s in ("4.6", "46", "opus-4-6", "opus4.6", "opus-4.6"):
        return "claude-opus-4-6"
    if s in ("4.8", "48", "opus-4-8", "opus4.8", "opus-4.8"):
        return "claude-opus-4-8"
    return sel if s.startswith("claude") else ""


@bot.command(name="escalate")
async def cmd_escalate(ctx: commands.Context, *, arg: str = "") -> None:
    """Approve a pending escalation: stand the triage agent down and respawn a
    specialist (deeper budget), seeded with triage's handoff.

      !escalate                  - specialist on the SAME lane
      !escalate <lane>           - RE-SCOPE: specialist on <lane> (e.g. rev triage IDs
                                   it's really pwn -> hand findings to a pwn specialist)
      !escalate deep [<lane>] [4.6|4.8]
                                 - deep (Claude/Opus) on <lane>'s playbook if given, else
                                   the deep_solver lane; pick the Opus version (4.6
                                   fallback if 4.8 refuses on cyber grounds)
      !escalate race [<lane>] [n] - fan out N specialists (on <lane> if given; default 3)
    """
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no worker here to escalate")
        return
    if DISABLE_HANDOFF:
        await ctx.send(
            "handoff disabled (`CDDC_DISABLE_HANDOFF=1`) - staying on triage. "
            "`!deny` to keep it grinding, `!kill` to stop."
        )
        return

    # grammar: !escalate [deep|race] [<lane>] [4.6|4.8 (deep) | <n> (race)]
    # A <lane> RE-SCOPES the challenge (e.g. rev triage IDs it's really pwn -> hand the
    # findings to a pwn specialist); deep + <lane> runs that lane's playbook on Opus.
    toks = arg.split()
    mode = ""
    if toks and toks[0].lower() in ("deep", "race"):
        mode, toks = toks[0].lower(), toks[1:]
    lane_arg: str | None = None
    deep_model = ""
    n = 1
    for t in toks:
        tl = t.lower()
        if tl in LANES:
            lane_arg = tl
        elif mode == "race" and tl.isdigit():
            n = max(2, min(int(tl), 5))
        elif mode == "deep" and _deep_model_for(t):
            deep_model = _deep_model_for(t)
        else:
            await ctx.send(
                "usage: `!escalate [deep|race] [<lane>] [4.6|4.8|<n>]`  "
                f"(lanes: {', '.join(sorted(LANES))})"
            )
            return
    if mode == "race" and n == 1:
        n = 3
    # deep with no explicit lane -> the deep_solver lane; otherwise honour the target lane.
    lane_override = (lane_arg or "deep_solver") if mode == "deep" else lane_arg
    # deep forces the deep (Opus) brain even on a category lane like pwn.
    tier_override = "deep" if mode == "deep" else ""

    # Persist a handoff DOSSIER on the (now persistent) box's workdir so the brain
    # taking over inherits what was tried - not a blank slate in a warm box (#11).
    # The steer is just a POINTER to it (small, safe over the harness's send-keys);
    # the agent path also reads the file directly on start.
    old = workers[0]
    ch = old.chall
    workdir = os.path.abspath(os.path.join(DOWNLOAD_DIR, str(ch.thread_id)))
    dossier_path = append_dossier(workdir, old.dossier_text())
    handoff = (
        f"[handoff] You are taking over a LIVE box a prior {getattr(old, 'role', 'worker')} "
        f"used - its tools, services, and scratch files are still here. Read the full "
        f"dossier at {dossier_path} FIRST, then build on it (don't redo its steps). "
        f"Quick read: difficulty {ch.difficulty}/5, technique {ch.technique or '?'}, "
        f"recommended {ch.recommendation or '?'}."
    )
    # The ladder is REAL: `!escalate deep` swaps the BRAIN to the deep tier (the
    # Claude Code box by default), not just the lane; `!escalate` / `race` = specialist.
    esc_kind = _DEEP_KIND if mode == "deep" else _SPECIALIST_KIND
    channel = ch.channel
    for w in workers:
        w.cancel()
        registry.remove(ctx.channel.id, w)

    spawned = []
    for i in range(n):
        # racers get isolated boxes/workdirs (cddc-<thread>-r<i>); a solo escalation keeps
        # the shared box (instance="").
        instance = f"-r{i + 1}" if n > 1 else ""
        new_chall = dataclasses.replace(ch, state="dispatched", steers=list(ch.steers))
        worker = await dispatcher.dispatch(
            new_chall, channel,
            lane_override=lane_override,
            on_candidate=_candidate_hook,
            kind=esc_kind, model=_specialist_model, cli=HARNESS_CLI, summarizer=_summarizer,
            role_override="specialist",
            budget_mult=ESCALATION_BUDGET_MULT,
            model_override=deep_model,  # !escalate deep 4.6|4.8 pins the cc deep tier's Opus
            tier_override=tier_override,  # deep brain even when re-laned to a category lane
            instance=instance,  # race: isolated per-racer box + workdir
        )
        worker.steer(handoff)
        if n > 1:
            worker.race_now()
        spawned.append(worker.name)

    lane_tag = f" -> `{lane_arg}` lane" if lane_arg else ""
    if mode == "deep":
        brain = f"Claude, {deep_model or 'plan default'}" if esc_kind == "cc" else esc_kind
        label = f"deep ({brain}){lane_tag}"
    elif n > 1:
        label = f"{n}-way race{lane_tag}"
    else:
        label = f"specialist{lane_tag}"
    await ctx.send(
        f"escalated -> **{label}**: {', '.join(spawned)} "
        f"(handed off via dossier: difficulty {ch.difficulty}/5, {ch.technique or '?'})"
    )


def _normalize_fetch_url(url: str) -> str:
    """Make a host's share URL a DIRECT download for the couple that aren't already."""
    if "tmpfiles.org/" in url and "/dl/" not in url:
        return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    if "bashupload.com/" in url and "download=1" not in url:
        return url + ("&" if "?" in url else "?") + "download=1"
    return url


async def _download_to(url: str, dest: str, *, max_mb: int) -> int:
    """Stream a URL to `dest` (never buffers the whole file). Returns bytes written;
    raises on HTTP error or if it exceeds max_mb."""
    import httpx

    cap = max_mb * 1024 * 1024
    total = 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True,
                                 timeout=httpx.Timeout(1800.0, connect=30.0)) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(262144):
                    total += len(chunk)
                    if total > cap:
                        raise ValueError(f"exceeds {max_mb} MB cap (raise CDDC_FETCH_MAX_MB)")
                    f.write(chunk)
    return total


@bot.command(name="fetch")
async def cmd_fetch(ctx: commands.Context, url: str = "", *, name: str = "") -> None:
    """Pull a BIG challenge file from a URL into this thread's /challenge - for files
    too large for a Discord attachment. Upload it to a host first, then paste the URL:

      !fetch <direct-download-url> [filename]

    Anonymous hosts that give a direct curl-able URL (2026): litterbox.catbox.moe
    (<=1GB, 72h), temp.sh (<=4GB, 3d), x0.at (<=1GB, longer). `sh sandbox/share.sh
    <file>` uploads + prints the URL. (0x0.st / transfer.sh are dead.)"""
    if not url.startswith(("http://", "https://")):
        await ctx.send("usage: `!fetch <direct-download-url> [filename]` - upload to "
                       "litterbox/temp.sh/x0.at first (or `sh sandbox/share.sh <file>`), then paste the URL")
        return
    url = _normalize_fetch_url(url.strip())
    thread_id = ctx.channel.id
    workdir = os.path.abspath(os.path.join(DOWNLOAD_DIR, str(thread_id)))
    fname = (name.strip() or os.path.basename(urlparse(url).path) or "download.bin").split("?")[0]
    dest = os.path.join(workdir, fname)
    await ctx.send(f"fetching `{fname}` ... (large files take a moment)")
    try:
        size = await _download_to(url, dest, max_mb=FETCH_MAX_MB)
    except Exception as e:
        await ctx.send(f"fetch failed: {type(e).__name__}: {e}")
        return
    await ctx.send(f"saved `{fname}` ({size / 1024 / 1024:.1f} MB) -> /challenge")
    # nudge any running worker on this thread so it picks the file up
    registry.broadcast(thread_id, lambda w: w.steer(f"operator placed a new file in /challenge: {fname}"))


@bot.command(name="start", aliases=["dispatch"])
async def cmd_start(ctx: commands.Context, *, description: str = "") -> None:
    """!start <description>  (+ attach the distribution files).

    The ONLY way a challenge begins. Category is inferred from the thread's
    parent channel; reroute later with !lane. Must be run inside a thread.
    """
    thread = ctx.channel
    if not isinstance(thread, discord.Thread):
        await ctx.send(
            "open a thread in a category channel (pwn/rev/crypto/...) and !start in it"
        )
        return
    parent = thread.parent.name if thread.parent else ""
    if parent in IGNORE_CHANNELS:
        await ctx.send(f"`{parent}` is not a challenge category")
        return
    category = category_for_channel(parent)
    if category not in CATEGORY_LANE:
        await ctx.send(
            f"`{parent}` is not a known category; known: "
            f"{', '.join(sorted(CATEGORY_LANE))}"
        )
        return
    if registry.workers(thread.id):
        await ctx.send("already a worker here (use !lane to reroute, !kill to stop)")
        return

    local, links = await _acquire_files(ctx.message, thread.id)
    chall = Challenge(
        id=str(thread.id),
        name=thread.name,
        category=category,
        description=description or thread.name,
        thread_id=thread.id,
        files=local + links,
    )
    worker = await dispatcher.dispatch(
        chall, DiscordChannel(thread, bot), on_candidate=_candidate_hook,
        kind=_KIND, model=_model, cli=HARNESS_CLI, summarizer=_summarizer,
    )

    note = f"downloaded {len(local)} file(s)" if local else "no attachments"
    if links:
        note += f", {len(links)} link(s) recorded (not fetched yet)"
    await ctx.send(
        f"started **{worker.name}** ({_KIND}) on lane `{worker.lane.name}` "
        f"[{worker.lane.default_mode}] - {note}"
    )


@bot.command(name="solved")
async def cmd_solved(ctx: commands.Context) -> None:
    """Confirm a pending candidate flag: stand down + rename the thread."""
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no worker here")
        return
    for w in workers:
        w.mark_solved()
        registry.remove(ctx.channel.id, w)
    await registry.release_box(ctx.channel.id)  # challenge done -> destroy the box (#12)
    if isinstance(ctx.channel, discord.Thread) and not ctx.channel.name.startswith("[SOLVED]"):
        await ctx.channel.edit(name=f"[SOLVED] {ctx.channel.name}"[:100])
    await ctx.send(f"confirmed solved - {len(workers)} agent(s) standing down")
    # mirror the win to #status
    guild = ctx.guild
    status = discord.utils.get(guild.text_channels, name=STATUS_CHANNEL) if guild else None
    if status is not None and isinstance(ctx.channel, discord.Thread):
        prefix, allowed = _alert("big")
        lead = f"{prefix} " if prefix else ""
        await status.send(
            f"{lead}[SOLVED] {ctx.channel.mention} - {ctx.channel.jump_url}",
            allowed_mentions=allowed,
        )


@bot.command(name="continue", aliases=["deny"])
async def cmd_continue(ctx: commands.Context, *, reason: str = "") -> None:
    """Resume a halted worker. After a CANDIDATE FLAG it's a rejection
    (`!continue <why>`); after a TRIAGE REPORT / budget halt it just means
    "keep going" - an optional `!continue <steer>` folds a nudge in. `!deny` is
    the same, named for refusing an escalation."""
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no worker here")
        return
    # Only a pending candidate FLAG is a "rejection"; a report/budget halt is just
    # a continue. Pick the language (and the default folded note) from the state.
    rejecting = any(w.chall.state == "candidate" for w in workers)
    if reason:
        note = reason
    elif rejecting:
        note = "operator: candidate rejected, keep going"
    else:
        note = "operator: continue"
    for w in workers:
        w.continue_with(note)
    verb = "candidate rejected, re-deriving" if rejecting else "continuing"
    tail = f" (steer: {reason})" if reason else ""
    await ctx.send(f"{verb} - {len(workers)} agent(s) resuming{tail}")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context) -> None:
    lines = [
        "**CDDC bot commands** (run inside a challenge thread):",
        "`!start <desc>` + attachments - begin a challenge here (downloads files)",
        "`!fetch <url> [name]` - pull a BIG file (too large to attach) into /challenge; "
        "upload to litterbox/temp.sh/x0.at first (or `sh sandbox/share.sh <file>`)",
        "`!status` - snapshot every agent on this thread",
        "`!trace [@name]` - dump an agent's full message+tool trace as a file",
        "`!files` - list+upload agent files (`!files list` = list only; "
        "`!files a.txt, b.bin` = pull those by name)",
        "`!steer <text>` - nudge the agent(s); the ONLY way to reach them "
        "(`!steer @name ...` targets one). plain chat is ignored.",
        "`!race` - escalate to racing / confirm a held race-ask",
        "`!solo` - decline or undo a race (the negative of !race)",
        "`!pause` / `!resume` - idle / continue",
        "`!kill` - stop and stand down",
        "`!lane <name>` - reroute onto another lane",
        "",
        "**when a candidate flag is found** the thread halts for validation:",
        "`!solved` - confirm it; renames thread to [SOLVED], agents stand down",
        "`!continue <why it's wrong>` - reject it; reason is folded in, agents re-open",
        "",
        "**when an agent asks to escalate** (it hit something too hard):",
        "`!escalate [<lane>]` - respawn a specialist; a <lane> RE-SCOPES it (e.g. rev "
        "triage IDs pwn -> pwn specialist), handing over the triage findings",
        "`!escalate deep [<lane>] [4.6|4.8]` - Claude/Opus on <lane>'s playbook (else "
        "deep_solver); 4.6 falls back if 4.8 refuses on cyber grounds",
        "`!escalate race [<lane>] [n]` - fan out N ISOLATED racers (default 3); first flag wins",
        "`!deny` - refuse; the agent keeps going as triage",
        "",
        f"lanes: {', '.join(sorted(LANES))}",
    ]
    await ctx.send("\n".join(lines))


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set - copy cddc/.env.example to cddc/.env")
    bot.run(token)


if __name__ == "__main__":
    main()
