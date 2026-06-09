"""Dispatcher - per-thread, lightweight: pick lane, build worker, register, run.

Discord-agnostic: takes a Challenge + a Channel, returns a started Worker. The
bot calls this on thread-create; simulate.py calls it directly.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import config

_log = logging.getLogger("cddc.dispatch")
from .agent_worker import AgentWorker
from .challenge import Challenge
from .channel import Channel
from .lanes import LANES, get_lane
from .lanes.base import Lane
from .registry import Registry
from .worker import DummyWorker, Worker


class Dispatcher:
    def __init__(self, registry: Registry, *, default_location: str = "offsite") -> None:
        self.registry = registry
        self.default_location = default_location
        self._n = 0  # monotonic worker counter -> ids/names

    def _build_box(self, chall: Challenge, lane: Lane, *, docker_sock: str | None):
        """Construct the per-challenge box with the UNIFORM superset of privileges.

        The box is SHARED by every tier on the challenge and created ONCE (task #12),
        so it must carry everything ANY tier might need - you cannot add a mount or a
        socket to a running container. Per the uniform-tool-access doctrine:
          - docker socket: granted whenever the host configured one (CDDC_DOCKER_SOCK),
            NOT role-gated, so triage's box is ALREADY a socket box the specialist
            adopts on handoff. An explicit `docker_sock` arg still overrides ("" = off).
          - harness CLI creds: mounted whenever credential-sharing is on (a no-op when
            the host has no claude/codex login), so a Claude/Codex harness can take
            over a box a DeepSeek triage created.
        Image + GPU stay lane-derived (an ai challenge stays on the ai image/GPU across
        tiers); a !lane reroute across the ai boundary keeps the original image - !kill
        + !start to switch images. Only builds the object; start() launches it lazily.
        """
        from .harness import credential_mounts  # pure-stdlib helper; libtmux is lazy
        from .sandbox import Sandbox

        workdir = os.path.abspath(os.path.join(config.DOWNLOAD_DIR, str(chall.thread_id)))
        sock = docker_sock if docker_sock is not None else config.DOCKER_SOCK
        creds = credential_mounts(config.HARNESS_USER) if config.HARNESS_SHARE_CREDS else []
        return Sandbox(
            config.sandbox_image_for_lane(lane.name), chall.thread_id, workdir,
            extra_mounts=creds, docker_sock=(sock or None),
            mount_flag=config.CDDC_SANDBOX_MOUNT_FLAG,
            gpu=config.CDDC_SANDBOX_GPU,
            network=config.CDDC_SANDBOX_NETWORK,
            decompiler_url=config.CDDC_DECOMPILER_URL,
        )

    def pick_lane(self, chall: Challenge, *, override: str | None = None) -> Lane:
        """Resolve the INITIAL lane: explicit `!lane` override > category.

        Deliberately dumb. By doctrine (see roles/triage.md) the cheap triage
        agent does NOT auto-route - it recons, web_searches, and files a
        `triage_report` that RECOMMENDS a tier (solo_finish/race/specialist/
        deep_solver/needs_human). The operator reads it and pulls the trigger
        (`!escalate [deep|race]`), which re-dispatches with a role/lane override.
        So "smell hooks" are SIGNALS the agent folds into its report, NOT
        autonomous reroutes here. The ONE up-front route is the human-set
        `chall.hard` flag -> straight to deep_solver: that's operator foresight at
        allocation ("this'll need a deep researcher"), not a model guess.
        """
        if override:
            return get_lane(override)
        if chall.hard:
            return get_lane("deep_solver")

        category = chall.category
        lane_name = config.lane_for_category(category)
        return get_lane(lane_name) if lane_name in LANES else get_lane(config.DEFAULT_LANE)

    async def dispatch(
        self,
        chall: Challenge,
        channel: Channel,
        *,
        lane_override: str | None = None,
        location: str | None = None,
        operator: str | None = None,
        on_candidate=None,
        tick: float | None = None,
        kind: str = "dummy",
        model=None,
        cli: str | None = None,
        summarizer=None,
        autostart: bool = True,
        role_override: str | None = None,
        budget_mult: float = 1.0,
        docker_sock: str | None = None,
    ) -> Worker:
        """Build a worker for the challenge, register it, and start its loop.

        kind="dummy"   -> scripted DummyWorker (no key/cost).
        kind="agent"   -> real AgentWorker driving `model` (a ModelClient).
        kind="harness" -> HarnessWorker driving a CLI agent (claude/codex) in a
                          tmux session inside the sandbox container; `cli` picks
                          which CLI (defaults to config.HARNESS_CLI).

        role_override forces the agent's doctrine (escalation respawns a
        "specialist"); budget_mult scales its step cap (a specialist grinds).
        """
        lane = self.pick_lane(chall, override=lane_override)

        self._n += 1
        wid = f"w{self._n}"
        name = f"{lane.name}-{self._n}"

        if kind == "agent":
            # Absolute: a relative workdir is a docker bind-mount footgun (the
            # daemon resolves -v against ITS cwd, not the bot's).
            workdir = os.path.abspath(os.path.join(config.DOWNLOAD_DIR, str(chall.thread_id)))
            # Role drives which skills/roles/<role>.md doctrine loads. Thin-
            # triage-always by default; a specialist-mode lane (deep_solver,
            # windows) gets the deep-solver doctrine instead. Escalation / !lane
            # onto such a lane flips the role for free.
            role = role_override or (
                "specialist" if lane.default_mode == "specialist" else "triage"
            )
            sandbox = None
            if config.CDDC_SANDBOX == "docker":
                # The ONE shared box for this challenge: created on the first worker,
                # REUSED (not recreated) by every later worker on handoff (#12).
                sandbox = self.registry.get_box(
                    chall.thread_id,
                    lambda: self._build_box(chall, lane, docker_sock=docker_sock),
                )
            # Web tools (provider-agnostic): DDG default / Serper keyed search +
            # Jina extraction. None searcher (provider="none") -> tool withheld.
            from . import search

            searcher = search.make_searcher(
                config.WEB_SEARCH_PROVIDER,
                serper_key=config.SERPER_API_KEY,
                num_results=config.WEB_SEARCH_RESULTS,
            )
            reader = search.make_reader(jina_key=config.JINA_API_KEY)
            worker: Worker = AgentWorker(
                lane,
                chall,
                channel,
                id=wid,
                name=name,
                location=location or self.default_location,
                operator=operator,
                on_candidate=on_candidate,
                model=model,
                workdir=workdir,
                sandbox=sandbox,
                max_steps=max(1, int(config.AGENT_MAX_STEPS * budget_mult)),
                max_tokens=config.AGENT_MAX_TOKENS,
                shell_timeout=config.SHELL_TIMEOUT,
                checkpoint_every=config.AGENT_CHECKPOINT,
                role=role,
                searcher=searcher,
                reader=reader,
            )
        elif kind == "harness":
            from .harness import TmuxHarness
            from .harness_worker import HarnessWorker

            workdir = os.path.abspath(os.path.join(config.DOWNLOAD_DIR, str(chall.thread_id)))
            which = (cli or config.HARNESS_CLI).lower()
            launch = config.CODEX_CLI_CMD if which == "codex" else config.CLAUDE_CLI_CMD
            keys_csv = config.CODEX_STARTUP_KEYS if which == "codex" else config.CLAUDE_STARTUP_KEYS
            startup_keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
            role = role_override or (
                "specialist" if lane.default_mode == "specialist" else "triage"
            )
            # The container is MANDATORY here (the CLI runs inside it via docker
            # exec), so we always take the shared box - reusing whatever a prior
            # triage/agent built (with its creds + socket), or creating it now (#12).
            sandbox = self.registry.get_box(
                chall.thread_id,
                lambda: self._build_box(chall, lane, docker_sock=docker_sock),
            )
            session = TmuxHarness(
                which, sandbox, workdir,
                launch_cmd=launch,
                session_name=f"cddc-{chall.thread_id}-{which}",
                user=config.HARNESS_USER,
                startup_keys=startup_keys,
            )
            worker = HarnessWorker(
                lane,
                chall,
                channel,
                id=wid,
                name=name,
                location=location or self.default_location,
                operator=operator,
                on_candidate=on_candidate,
                session=session,
                cli=which,
                max_minutes=config.HARNESS_MAX_MINUTES,
                poll_interval=config.HARNESS_POLL,
                role=role,
                checkpoint_every=config.AGENT_CHECKPOINT,
                halt_on_flag=config.HARNESS_HALT_ON_FLAG,
                flag_blacklist=config.FLAG_BLACKLIST,
                summarizer=summarizer,
                summarize_every=config.HARNESS_SUMMARIZE_SECS,
            )
        elif kind == "cc":
            # Headless Claude Code (stream-json, no tmux). Same shared box, but the
            # CLI runs inside it; the per-TIER model is just the env profile (DeepSeek
            # flash/pro, or the subscription's Opus 4.8 - never a metered Anthropic key).
            from .cc_worker import CCWorker
            from .headless import HeadlessClaude

            workdir = os.path.abspath(os.path.join(config.DOWNLOAD_DIR, str(chall.thread_id)))
            role = role_override or (
                "specialist" if lane.default_mode == "specialist" else "triage"
            )
            sandbox = self.registry.get_box(
                chall.thread_id,
                lambda: self._build_box(chall, lane, docker_sock=docker_sock),
            )
            tier = config.cc_tier_for(role, lane.name)
            env_profile, secret_env = config.cc_profile(tier)
            session = HeadlessClaude(
                sandbox, workdir,
                env_profile=env_profile, secret_env=secret_env,
                user=config.HARNESS_USER or "",
            )
            worker = CCWorker(
                lane, chall, channel,
                id=wid, name=name, location=location or self.default_location,
                operator=operator, on_candidate=on_candidate,
                session=session, role=role,
                decompiler_url=config.CDDC_DECOMPILER_URL,
                workdir=workdir,
                halt_on_flag=config.HARNESS_HALT_ON_FLAG,
                flag_blacklist=config.FLAG_BLACKLIST,
                checkpoint_every=config.AGENT_CHECKPOINT,
                # cap only the triage tier (it should be fast); solvers run uncapped.
                turn_cap_secs=config.CC_TRIAGE_TURN_SECS if tier == "triage" else 0,
            )
        else:
            worker = DummyWorker(
                lane,
                chall,
                channel,
                id=wid,
                name=name,
                location=location or self.default_location,
                operator=operator,
                on_candidate=on_candidate,
                tick=config.STEP_DELAY if tick is None else tick,
            )

        chall.channel = channel
        chall.state = "dispatched"
        self.registry.add(chall.thread_id, worker)
        _log.info("dispatched %s: lane=%s kind=%s thread=%s '%s'",
                  name, lane.name, kind, chall.thread_id, (chall.name or "?")[:40])

        if autostart:
            # Run as a task - NEVER block the gateway heartbeat (bot.py footgun).
            worker._task = asyncio.create_task(worker.run())  # type: ignore[attr-defined]

        return worker
