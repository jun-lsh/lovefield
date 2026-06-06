"""Local harness - proves the phase-1 control plane with NO Discord token.

Run:  python cddc/simulate.py   (or: python -m cddc.simulate)

Exercises the done-criteria: every category routes to the right lane; a Worker
streams scripted progress; an injected steer shows up in the next progress post
AND in status(); and !race / !pause / !resume / !kill each take effect.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Allow `python cddc/simulate.py` (adds repo root so `import cddc` resolves).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cddc.challenge import Challenge
from cddc.channel import ConsoleChannel
from cddc.dispatcher import Dispatcher
from cddc.lanes import get_lane
from cddc.registry import Registry
from cddc.worker import DummyWorker


async def wait_until(pred, timeout: float = 3.0, interval: float = 0.02) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if pred():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return pred()


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


# --- scenario 1: routing -------------------------------------------------
async def scenario_routing() -> None:
    print("scenario: routing")
    reg = Registry()
    disp = Dispatcher(reg)

    cases = [
        ("pwn", "pwn"),
        ("rev", "rev"),
        ("crypto", "crypto"),
        ("web", "web"),
        ("forensics", "forensics"),
        ("misc", "misc"),
        ("ai", "ai"),
        ("hardware", "hw_research"),
        ("research", "research_run"),
    ]
    for i, (cat, expect) in enumerate(cases):
        ch = Challenge(id=f"r{i}", name=f"chall-{cat}", category=cat, thread_id=1000 + i)
        w = await disp.dispatch(ch, ConsoleChannel(cat, echo=False), autostart=False)
        assert w.lane.name == expect, f"{cat} routed to {w.lane.name}, expected {expect}"
    ok(f"all {len(cases)} categories route to the right lane")

    # !lane override beats category.
    ch = Challenge(id="ov", name="x", category="rev", thread_id=2000)
    w = await disp.dispatch(ch, ConsoleChannel(echo=False), lane_override="pwn", autostart=False)
    assert w.lane.name == "pwn"
    ok("!lane override beats category (rev -> pwn)")

    # Unknown category falls back to raw.
    ch = Challenge(id="uk", name="x", category="who-knows", thread_id=2001)
    w = await disp.dispatch(ch, ConsoleChannel(echo=False), autostart=False)
    assert w.lane.name == "raw"
    ok("unknown category falls back to raw")


# --- scenario 2: progress + steer + race-hold + validation ---------------
async def scenario_steer_and_race() -> None:
    print("scenario: progress streaming + steer + race-hold + validation")
    ch = Challenge(id="s1", name="rev-chall", category="rev", thread_id=42)
    chan = ConsoleChannel("rev")
    w = DummyWorker(get_lane("rev"), ch, chan, id="w1", name="rev-1", tick=0.08)
    task = asyncio.create_task(w.run())

    # streams progress proactively
    assert await wait_until(lambda: w.current_step >= 1)
    assert any(p.startswith("[1/") for p in chan.posts), "no step-1 progress post"
    ok("worker streams scripted progress")

    # inject a steer mid-run -> must appear in next progress post AND in status()
    chan.inject_steer("focus on the license check")
    assert await wait_until(lambda: "focus on the license check" in w.status()["steers"])
    assert any("adjusting for: focus on the license check" in p for p in chan.posts)
    ok("injected steer shows in next progress post AND in status()")

    # at ~half budget the worker HOLDS at the race-ask - no timeout, no advance
    assert await wait_until(lambda: any("race 3 subagents" in p for p in chan.posts))
    held = w.current_step
    await asyncio.sleep(0.3)
    assert w.current_step == held, "worker advanced instead of holding at the race-ask"
    ok("race-ask HOLDS indefinitely until a decision (no timeout)")

    # operator decides: race -> releases the hold
    w.race_now()
    assert await wait_until(lambda: w.status()["racing"] is True)
    ok("!race releases the hold and flips to racing")

    # worker reaches a candidate flag and HALTS for validation (no insta-end)
    assert await wait_until(lambda: ch.state == "candidate")
    ok("worker reaches candidate flag and halts for validation")

    # reject it: !continue folds the reason as a steer and re-opens the worker,
    # which re-derives and emits a fresh candidate
    w.continue_with("flag format looks off, recheck the xor key")
    assert await wait_until(
        lambda: "flag format looks off, recheck the xor key" in w.status()["steers"]
    )
    assert await wait_until(lambda: ch.state == "candidate")  # re-derived a new one
    ok("!continue folds reason as a steer and re-opens -> new candidate")

    # confirm it: !solved stands the worker down
    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("!solved -> worker stands down (solved)")


# --- scenario 3: pause / resume / kill -----------------------------------
async def scenario_control() -> None:
    print("scenario: pause / resume / kill")
    ch = Challenge(id="c1", name="crypto-chall", category="crypto", thread_id=7)
    chan = ConsoleChannel("crypto", echo=False)
    w = DummyWorker(get_lane("crypto"), ch, chan, id="w2", name="crypto-1", tick=0.08)
    task = asyncio.create_task(w.run())

    assert await wait_until(lambda: w.current_step >= 1)

    w.pause()
    assert ch.state == "paused"
    at_pause = w.current_step
    await asyncio.sleep(0.3)
    assert w.current_step == at_pause, "worker advanced while paused"
    ok("!pause -> worker idles, holds state, does not advance")

    w.resume()
    assert ch.state in ("solving", "racing")
    assert await wait_until(lambda: w.current_step > at_pause), "did not resume"
    ok("!resume -> worker continues")

    w.cancel()
    await task
    assert ch.state == "killed", f"expected killed, got {ch.state}"
    ok("!kill -> clean exit to killed")


# --- scenario 4: !solo is the inverse of !race ---------------------------
async def scenario_solo() -> None:
    print("scenario: race decline / undo (!solo)")
    ch = Challenge(id="x1", name="n", category="rev", thread_id=99)
    w = DummyWorker(get_lane("rev"), ch, ConsoleChannel(echo=False), id="ws", name="rev-x")
    w.race_now()
    assert w.status()["racing"] is True and ch.state == "racing"
    w.go_solo()
    assert w.status()["racing"] is False and ch.state == "solving"
    ok("!solo flips racing off and drops back to solo (inverse of !race)")


async def main() -> None:
    await scenario_routing()
    print()
    await scenario_steer_and_race()
    print()
    await scenario_control()
    print()
    await scenario_solo()
    print("\nALL PHASE-1 SIM CHECKS PASSED [done]  (no Discord token used)")


if __name__ == "__main__":
    asyncio.run(main())
