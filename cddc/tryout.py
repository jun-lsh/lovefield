"""Headless triage tryout - run the REAL triage agent on a local challenge folder.

No Discord, no dispatcher, so NO handoff/escalation can fire: the worker recons,
maybe lands a flag, or files a triage report, then we print the outcome and stop.
For eyeballing triage quality on real challenges before the backtest (which leans
on the heavy docker layers).

  python -m cddc.tryout <challenge_dir> --category crypto [--desc "..."]
         [--lane <name>] [--role triage] [--no-docker] [--max-steps N]

Env is read from cddc/.env (same as the bot): DEEPSEEK_API_KEY + the flash model,
CDDC_WEB_SEARCH / SERPER_API_KEY for web_search, etc. The challenge files live in
<challenge_dir> (bind-mounted to /challenge in the sandbox); the agent reads and
writes there. run_shell runs inside the ctf-sandbox container unless --no-docker.

Discord-agnostic; the agent narrates to the console as it works.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import zlib

from . import config, search
from .agent_worker import AgentWorker
from .challenge import Challenge
from .channel import ConsoleChannel
from .lanes import LANES, get_lane
from .models import DeepSeekClient

# States at which the worker either finished or is blocked waiting for an operator
# (a decision that, by design, never comes here - we stop instead of handing off).
_TERMINAL = {"candidate", "needs_human", "solved", "killed"}


def _build_model() -> DeepSeekClient:
    if not config.DEEPSEEK_API_KEY:
        raise SystemExit("DEEPSEEK_API_KEY is empty - set it in cddc/.env to run a real triage")
    return DeepSeekClient(
        config.DEEPSEEK_API_KEY,
        config.DEEPSEEK_BASE_URL,
        config.CHURN_MODEL,
        thinking=config.CHURN_THINKING,
    )


async def run(args: argparse.Namespace) -> None:
    workdir = os.path.abspath(args.challenge_dir)
    if not os.path.isdir(workdir):
        raise SystemExit(f"no such challenge dir: {workdir}")
    name = os.path.basename(workdir.rstrip("/\\")) or "challenge"
    # Stable per-folder id (so re-runs reuse the same container name, not pile up).
    thread_id = zlib.crc32(name.encode()) % 1_000_000
    files = [
        os.path.join(workdir, f)
        for f in sorted(os.listdir(workdir))
        if os.path.isfile(os.path.join(workdir, f)) and not f.startswith(".cddc")
    ]

    lane_name = args.lane or config.lane_for_category(args.category)
    lane = get_lane(lane_name if lane_name in LANES else config.DEFAULT_LANE)

    sandbox = None
    if not args.no_docker:
        from .sandbox import Sandbox

        # triage role -> no docker socket (config gates it); a --role specialist
        # tryout would get one. Recon tools come from the image, not the socket.
        sandbox = Sandbox(
            config.sandbox_image_for_lane(lane.name), thread_id, workdir,
            docker_sock=config.docker_sock_for_role(args.role) or None,
            mount_flag=config.CDDC_SANDBOX_MOUNT_FLAG,
            gpu=config.CDDC_SANDBOX_GPU,
            network=config.CDDC_SANDBOX_NETWORK,
            decompiler_url=config.CDDC_DECOMPILER_URL,
        )

    searcher = search.make_searcher(
        config.WEB_SEARCH_PROVIDER,
        serper_key=config.SERPER_API_KEY,
        num_results=config.WEB_SEARCH_RESULTS,
    )
    reader = search.make_reader(jina_key=config.JINA_API_KEY)

    ch = Challenge(
        id="try", name=name, category=args.category, thread_id=thread_id,
        description=args.desc or "", files=files,
    )
    chan = ConsoleChannel(args.category, echo=True)
    worker = AgentWorker(
        lane, ch, chan,
        id="t1", name=f"{lane.name}-tryout", role=args.role,
        model=_build_model(), workdir=workdir, sandbox=sandbox,
        max_steps=args.max_steps, max_tokens=config.AGENT_MAX_TOKENS,
        shell_timeout=config.SHELL_TIMEOUT, checkpoint_every=config.AGENT_CHECKPOINT,
        searcher=searcher, reader=reader,
    )

    print("==== triage tryout ====")
    print(f"  challenge : {name}  ({len(files)} file(s)) -> {workdir}")
    print(f"  lane/role : {lane.name} / {args.role}")
    print(f"  model     : {config.CHURN_MODEL} (thinking={config.CHURN_THINKING})")
    print(f"  sandbox   : {'docker:' + config.CDDC_SANDBOX_IMAGE if sandbox else 'local (no docker)'}")
    print(f"  web_search: {config.WEB_SEARCH_PROVIDER}")
    print("  handoff   : DISABLED (no dispatcher - it files a report and stops)\n")

    task = asyncio.create_task(worker.run())
    try:
        # Poll until the worker finishes or halts at a terminal/decision state.
        while not task.done() and ch.state not in _TERMINAL:
            await asyncio.sleep(0.5)
        await asyncio.sleep(0.3)  # let the terminal block finish posting
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n(interrupted)")

    print("\n==== outcome ====")
    print(f"  state     : {ch.state}")
    print(f"  steps/tok : {worker.current_step} / {getattr(worker, '_tokens', '?')}")
    if ch.state == "needs_human" and ch.recommendation:
        print(f"  TRIAGE REPORT - difficulty {ch.difficulty}/5 "
              f"(confidence {ch.confidence or '?'}), recommend: {ch.recommendation}")
        print(f"    gist     : {ch.gist or '-'}")
        print(f"    technique: {ch.technique or '?'}")
        print(f"    blockers : {ch.escalation_reason or '-'}")
        print("  (handoff disabled - not escalating)")
    elif ch.state == "candidate":
        print("  CANDIDATE FLAG produced (see the block above) - not auto-submitting")
    elif ch.state == "needs_human":
        print("  halted for a human (budget/error) - see the block above")

    worker.cancel()  # release the validation gate so the task can unwind
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, Exception):
        pass
    # The box now PERSISTS past a worker (#12), so this standalone tool must reap
    # it explicitly - there's no bot/registry here to release it on challenge end.
    if sandbox is not None:
        await sandbox.release()


def main() -> None:
    p = argparse.ArgumentParser(description="Run the real triage agent on a local challenge folder (no Discord, no handoff).")
    p.add_argument("challenge_dir", help="folder holding the challenge's files")
    p.add_argument("--category", default="misc", help="pwn|rev|crypto|web|forensics|misc|ai|hardware|research (default misc)")
    p.add_argument("--desc", default="", help="the challenge prompt/description text")
    p.add_argument("--lane", default="", help="force a lane (default: derived from --category)")
    p.add_argument("--role", default="triage", help="triage (default) | specialist")
    p.add_argument("--no-docker", action="store_true", help="run run_shell on the host instead of the ctf-sandbox container")
    p.add_argument("--max-steps", type=int, default=config.AGENT_MAX_STEPS, help=f"step cap (default {config.AGENT_MAX_STEPS})")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
