"""Dispatcher - per-thread, lightweight: pick lane, build worker, register, run.

Discord-agnostic: takes a Challenge + a Channel, returns a started Worker. The
bot calls this on thread-create; simulate.py calls it directly.
"""

from __future__ import annotations

import asyncio
import os

from . import config
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

    def pick_lane(self, chall: Challenge, *, override: str | None = None) -> Lane:
        """Resolve the lane: explicit `!lane` override > smell hooks > category.

        Smell hooks are STUBBED in phase 1 (the handoff's `# TODO` pattern) - the
        thin-triage doctrine says a worker self-assesses and may self-escalate,
        but none of that routing is built yet. Wired as comments so phase 4 has
        the hook points.
        """
        if override:
            return get_lane(override)

        # --- TODO smell hooks (phase 4; not implemented) ------------------
        # if chall.hard:                         -> deep_solver (operator foresight)
        # if smells_cve_or_version(chall):       -> research_run
        # if smells_windows_pe_or_anti_wine(...): -> windows
        # if smells_trivial(chall):              -> raw
        # Self-escalation (mid-run, with justification) lives in the worker,
        # not here - e.g. pwn recon reveals kernel pwn -> request deep_solver.

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
        autostart: bool = True,
    ) -> Worker:
        """Build a worker for the challenge, register it, and start its loop.

        kind="dummy" -> scripted DummyWorker (no key/cost).
        kind="agent" -> real AgentWorker driving `model` (a ModelClient).
        """
        lane = self.pick_lane(chall, override=lane_override)

        self._n += 1
        wid = f"w{self._n}"
        name = f"{lane.name}-{self._n}"

        if kind == "agent":
            workdir = os.path.join(config.DOWNLOAD_DIR, str(chall.thread_id))
            sandbox = None
            if config.CDDC_SANDBOX == "docker":
                from .sandbox import Sandbox

                sandbox = Sandbox(config.CDDC_SANDBOX_IMAGE, chall.thread_id, workdir)
            # Role drives which skills/roles/<role>.md doctrine loads. Thin-
            # triage-always by default; a specialist-mode lane (deep_solver,
            # windows) gets the deep-solver doctrine instead. Escalation / !lane
            # onto such a lane flips the role for free.
            role = "specialist" if lane.default_mode == "specialist" else "triage"
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
                max_steps=config.AGENT_MAX_STEPS,
                max_tokens=config.AGENT_MAX_TOKENS,
                shell_timeout=config.SHELL_TIMEOUT,
                checkpoint_every=config.AGENT_CHECKPOINT,
                role=role,
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

        if autostart:
            # Run as a task - NEVER block the gateway heartbeat (bot.py footgun).
            worker._task = asyncio.create_task(worker.run())  # type: ignore[attr-defined]

        return worker
