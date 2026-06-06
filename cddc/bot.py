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
import os
import pathlib
import re

import discord
from discord.ext import commands

from .challenge import Challenge
from .config import (
    ALERT_MODE,
    ALERT_USER_ID,
    CATEGORY_LANE,
    CHURN_MODEL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DOWNLOAD_DIR,
    IGNORE_CHANNELS,
    STATUS_CHANNEL,
    WORKER_KIND,
    category_for_channel,
)
from .dispatcher import Dispatcher
from .lanes import LANES
from .models import DeepSeekClient
from .registry import Registry

# .env is loaded by config.py (imported above) before its env reads.

registry = Registry()
dispatcher = Dispatcher(registry)

# Build the model client once if we're in agent mode and a key is present;
# otherwise fall back to dummy workers (scripted, no key/cost).
_model = None
if WORKER_KIND == "agent":
    if DEEPSEEK_API_KEY:
        _model = DeepSeekClient(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, CHURN_MODEL)
    else:
        print("WORKER_KIND=agent but DEEPSEEK_API_KEY is empty -> using dummy workers")

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
        """'big' for candidate-flag / needs-human, 'halt' for the race-ask."""
        if ("CANDIDATE FLAG" in content) or ("NEEDS HUMAN" in content):
            return "big"
        if "[ask]" in content:  # any worker question = a halt awaiting a human
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
        elif "NEEDS HUMAN" in content:
            kind = "NEEDS HUMAN"
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
    await ctx.send(f"killing {len(hit)} worker(s)" if hit else "no worker here")


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
    kind = "agent" if _model is not None else "dummy"
    worker = await dispatcher.dispatch(
        chall, DiscordChannel(thread, bot), on_candidate=_candidate_hook,
        kind=kind, model=_model,
    )

    note = f"downloaded {len(local)} file(s)" if local else "no attachments"
    if links:
        note += f", {len(links)} link(s) recorded (not fetched yet)"
    await ctx.send(
        f"started **{worker.name}** ({kind}) on lane `{worker.lane.name}` "
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


@bot.command(name="continue")
async def cmd_continue(ctx: commands.Context, *, reason: str = "") -> None:
    """Reject a pending candidate flag: fold the reason in and re-open."""
    workers = registry.workers(ctx.channel.id)
    if not workers:
        await ctx.send("no worker here")
        return
    note = reason or "operator: candidate rejected, keep going"
    for w in workers:
        w.continue_with(note)
    await ctx.send(f"re-opened - {len(workers)} agent(s) resuming. folded in: {note}")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context) -> None:
    lines = [
        "**CDDC bot commands** (run inside a challenge thread):",
        "`!start <desc>` + attachments - begin a challenge here (downloads files)",
        "`!status` - snapshot every agent on this thread",
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
