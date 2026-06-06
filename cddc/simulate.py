"""Local harness - proves the phase-1 control plane with NO Discord token.

Run:  python cddc/simulate.py   (or: python -m cddc.simulate)
      CDDC_SIM_DOCKER=1 python -m cddc.simulate  # also runs the Docker sandbox

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

from cddc.agent_worker import AgentWorker, _specs_for_lane, load_system
from cddc.challenge import Challenge
from cddc.channel import ConsoleChannel, WebhookChannel
from cddc.dispatcher import Dispatcher
from cddc.lanes import get_lane
from cddc.models import FakeModel, Reply, ToolCall
from cddc.registry import Registry
from cddc.tools import Toolbox
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


# --- scenario 5: real AgentWorker loop (fake model, no key, no cost) ------
async def scenario_agent() -> None:
    print("scenario: real agent tool-loop (fake model)")
    ch = Challenge(
        id="a1", name="warmup", category="crypto", thread_id=555,
        description="decode the blob and find the flag",
    )
    chan = ConsoleChannel("crypto", echo=False)
    # Scripted model: inspect -> echo a flag-ish string -> submit_flag.
    script = [
        Reply(content="checking the workdir", tokens=80,
              tool_calls=[ToolCall("t1", "run_shell", {"command": "echo aGVsbG8="})]),
        Reply(content="that base64 looks like the flag, verifying", tokens=90,
              tool_calls=[ToolCall("t2", "run_shell", {"command": "echo CDDC{fake_solve}"})]),
        Reply(content="confirmed", tokens=70,
              tool_calls=[ToolCall("t3", "submit_flag", {"flag": "CDDC{fake_solve}"})]),
    ]
    w = AgentWorker(
        get_lane("crypto"), ch, chan,
        id="wa", name="crypto-a", model=FakeModel(script),
        workdir=os.path.join("_files", "sim555"),
        max_steps=40, max_tokens=200_000, shell_timeout=10,
    )
    task = asyncio.create_task(w.run())

    assert await wait_until(lambda: ch.state == "candidate", timeout=5)
    assert any("CDDC{fake_solve}" in p for p in chan.posts), "candidate flag not posted"
    assert w.status()["tokens"] == 240, "token accounting off"
    ok("agent runs tools, accounts tokens, emits a candidate flag")

    # !trace dumps the full message+tool log, not just progress
    trace = w.trace_text()
    assert "full message log" in trace, "agent trace missing the message log"
    assert "[tool result]" in trace, "agent trace missing tool results"
    ok("trace_text() includes the full message+tool log")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("agent stands down on !solved")


# --- scenario 6: sandbox routing + per-lane tool gating (no docker) -------
class _FakeSandbox:
    """Stands in for cddc.sandbox.Sandbox - records the command, returns canned."""

    def __init__(self, canned: str) -> None:
        self.canned = canned
        self.calls: list[str] = []

    async def exec(self, command: str, timeout: int) -> str:
        self.calls.append(command)
        return self.canned


async def scenario_sandbox_and_gating() -> None:
    print("scenario: sandbox routing + per-lane tool gating (no docker)")

    # run_shell routes through the sandbox; read/write stay host-side on the mount.
    sb = _FakeSandbox("uid=0(root) gid=0(root)")
    tb = Toolbox(os.path.join("_files", "simgate"), shell_timeout=10, sandbox=sb)
    out = await tb.run("run_shell", {"command": "id"})
    assert out == "uid=0(root) gid=0(root)", f"sandbox digest wrong: {out!r}"
    assert sb.calls == ["id"], f"command not routed to sandbox: {sb.calls}"
    await tb.run("write_file", {"path": "note.txt", "content": "hi"})  # host-side, no sandbox call
    assert sb.calls == ["id"], "write_file should not hit the sandbox"
    ok("run_shell routes to the sandbox; write_file stays host-side")

    # local Toolbox (no sandbox) still runs on the host.
    tb_local = Toolbox(os.path.join("_files", "simlocal"), shell_timeout=10)
    out_local = await tb_local.run("run_shell", {"command": "echo hostside"})
    assert "hostside" in out_local, f"local shell broken: {out_local!r}"
    ok("local path (no sandbox) still executes on the host")

    # per-lane gating: offline-analysis lanes withhold fetch_url; raw offers all.
    rev_tools = {s["function"]["name"] for s in _specs_for_lane(get_lane("rev"))}
    assert "fetch_url" not in rev_tools, "rev should not offer fetch_url"
    assert {"run_shell", "submit_flag"} <= rev_tools, "rev missing core/submit_flag"
    web_tools = {s["function"]["name"] for s in _specs_for_lane(get_lane("web"))}
    assert "fetch_url" in web_tools, "web should offer fetch_url"
    # raw has no allowlist -> it offers every advertised tool. submit_flag and
    # request_escalation are always on (agent-handled), even on gated lanes.
    raw_tools = {s["function"]["name"] for s in _specs_for_lane(get_lane("raw"))}
    assert {"run_shell", "read_file", "write_file", "fetch_url", "submit_flag",
            "request_escalation"} <= raw_tools, f"raw missing core tools: {raw_tools}"
    assert "request_escalation" in rev_tools, "escalation must be offered even on gated lanes"
    ok("rev gates out fetch_url; web grants it; raw offers all; submit_flag/escalation always on")


# --- scenario 7: real AgentWorker + real docker Sandbox (CDDC_SIM_DOCKER) -
async def scenario_sandbox_agent() -> None:
    print("scenario: real agent sandbox lifecycle (docker)")
    from cddc import config
    from cddc.sandbox import Sandbox

    ch = Challenge(
        id="sa1", name="sandbox-warmup", category="pwn", thread_id=556,
        description="prove run_shell executes in the Docker sandbox",
    )
    chan = ConsoleChannel("pwn", echo=False)
    workdir = os.path.join("_files", "sim556")
    marker = os.path.join(workdir, "sandbox_agent.txt")
    if os.path.exists(marker):
        os.remove(marker)

    # The marker proves the shell tool ran in the container: local execution
    # would write a host cwd and uid=1000-ish, not /challenge and uid=0.
    script = [
        Reply(content="checking sandbox workdir", tokens=80, tool_calls=[
            ToolCall(
                "s1",
                "run_shell",
                {
                    "command": (
                        "printf 'pwd=%s uid=%s\\n' \"$PWD\" \"$(id -u)\" "
                        "> sandbox_agent.txt"
                    ),
                },
            ),
        ]),
        Reply(content="checking shared mount", tokens=90, tool_calls=[
            ToolCall("s2", "read_file", {"path": "sandbox_agent.txt"}),
        ]),
        Reply(content="confirmed sandbox execution", tokens=70, tool_calls=[
            ToolCall("s3", "submit_flag", {"flag": "CDDC{sandbox_agent}"}),
        ]),
    ]
    sb = Sandbox(config.CDDC_SANDBOX_IMAGE, ch.thread_id, workdir)
    w = AgentWorker(
        get_lane("pwn"), ch, chan,
        id="wsa", name="pwn-sandbox-a", model=FakeModel(script),
        workdir=workdir, sandbox=sb,
        max_steps=40, max_tokens=200_000, shell_timeout=10,
    )
    task = asyncio.create_task(w.run())

    assert await wait_until(lambda: ch.state in ("candidate", "killed"), timeout=20), (
        f"agent never reached candidate; state={ch.state!r}; posts={chan.posts!r}"
    )
    assert ch.state == "candidate", f"agent failed before candidate; posts={chan.posts!r}"
    assert sb.started, "sandbox should still be running while candidate waits"
    with open(marker, encoding="utf-8") as f:
        marker_text = f.read().strip()
    assert marker_text == "pwd=/challenge uid=0", f"run_shell did not run in sandbox: {marker_text!r}"
    assert any("[sandbox] container `cddc-556` up" in p for p in chan.posts), "sandbox start not posted"
    assert any("CDDC{sandbox_agent}" in p for p in chan.posts), "candidate flag not posted"
    assert w.status()["tokens"] == 240, "token accounting off"
    ok("agent starts Docker sandbox, runs shell in it, and emits a candidate flag")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    assert not sb.started, "sandbox should be marked stopped after teardown"
    assert any("[sandbox] container `cddc-556` removed" in p for p in chan.posts), "sandbox teardown not posted"
    ok("agent tears the Docker sandbox down on !solved")


# --- scenario 8: triage escalation (request -> deny -> continue -> solve) -
async def scenario_escalation() -> None:
    print("scenario: triage escalation (request -> deny -> continue -> solve)")
    ch = Challenge(
        id="e1", name="hard-rsa", category="crypto", thread_id=777,
        description="ciphertext + public params; smells like a known RSA attack",
    )
    chan = ConsoleChannel("crypto", echo=False)
    script = [
        Reply(content="inventorying the workdir", tokens=50,
              tool_calls=[ToolCall("t1", "run_shell", {"command": "echo workdir"})]),
        Reply(content="this is a lattice/Franklin-Reiter job - beyond a cheap triage solve",
              tokens=60,
              tool_calls=[ToolCall("t2", "request_escalation",
                                   {"difficulty": 4, "technique": "RSA/Franklin-Reiter",
                                    "reason": "needs a specialist with lattice tooling"})]),
        Reply(content="operator says grind it - retrying via the related-message angle",
              tokens=60,
              tool_calls=[ToolCall("t3", "run_shell", {"command": "echo CDDC{related_msg}"})]),
        Reply(content="that decrypts cleanly, submitting", tokens=50,
              tool_calls=[ToolCall("t4", "submit_flag", {"flag": "CDDC{related_msg}"})]),
    ]
    w = AgentWorker(
        get_lane("crypto"), ch, chan,
        id="we", name="crypto-e", model=FakeModel(script),
        workdir=os.path.join("_files", "sim777"),
        max_steps=40, max_tokens=200_000, shell_timeout=10, role="triage",
    )
    task = asyncio.create_task(w.run())

    # triage hits the wall and HALTS with an escalation request (needs_human),
    # recording its difficulty read on the challenge for the handoff
    assert await wait_until(lambda: ch.state == "needs_human", timeout=5)
    assert ch.difficulty == 4, f"difficulty not captured: {ch.difficulty}"
    assert ch.technique == "RSA/Franklin-Reiter", "technique not captured"
    assert any("ESCALATION REQUEST" in p for p in chan.posts), "no escalation block posted"
    ok("triage requests escalation -> halts (needs_human), difficulty captured")

    # operator DENIES (the !deny path == continue_with): fold a note + re-open;
    # the worker pushes on as triage and reaches a candidate
    w.continue_with("deny: no specialist free, push the related-message angle")
    assert await wait_until(
        lambda: "deny: no specialist free, push the related-message angle"
        in w.status()["steers"]
    )
    assert await wait_until(lambda: ch.state == "candidate", timeout=5)
    assert any("CDDC{related_msg}" in p for p in chan.posts), "no candidate after deny"
    ok("!deny folds a note + re-opens -> triage pushes on to a candidate")

    # tool RESULTS now post to the thread (the truncation fix), not just actions
    assert any("-> CDDC{related_msg}" in p for p in chan.posts), "tool result not posted"
    ok("tool results are posted to the thread, not only fed to the model")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("agent stands down on !solved after a denied escalation")


# --- scenario 9: WebhookChannel - the on-site path ------------------------
async def scenario_webhook() -> None:
    print("scenario: WebhookChannel (on-site: narrate out via webhook + local steer)")
    sent: list[str] = []  # what WOULD hit the webhook (injected sender, no network)
    ch = Challenge(
        id="wk1", name="onsite-warmup", category="crypto", thread_id=888,
        description="a teammate's local on-site agent",
    )
    chan = WebhookChannel(
        "https://discord.example/api/webhooks/fake", thread_id=888,
        username="alice-crypto", sender=sent.append,
    )
    script = [
        Reply(content="recon the workdir", tokens=40,
              tool_calls=[ToolCall("t1", "run_shell", {"command": "echo hi"})]),
        Reply(content="that's the flag", tokens=40,
              tool_calls=[ToolCall("t2", "submit_flag", {"flag": "CDDC{onsite}"})]),
    ]
    w = AgentWorker(
        get_lane("crypto"), ch, chan,
        id="wk", name="alice-crypto", location="onsite", operator="alice",
        model=FakeModel(script), workdir=os.path.join("_files", "sim888"),
        max_steps=40, max_tokens=200_000, shell_timeout=10,
    )
    task = asyncio.create_task(w.run())

    # on-site operator steers LOCALLY (no Discord) -> worker folds it in
    chan.push_steer("try the affine-cipher angle")
    assert await wait_until(lambda: "try the affine-cipher angle" in w.status()["steers"])
    ok("local push_steer() reaches the worker (on-site steering, no Discord)")

    # narration goes OUT through the webhook sender (Discord thread in prod),
    # under the agent's own identity - no bot token, no gateway
    assert await wait_until(lambda: ch.state == "candidate", timeout=5)
    assert any("CDDC{onsite}" in s for s in sent), "candidate not sent via webhook"
    assert any("alice-crypto" in s for s in sent), "agent identity missing from narration"
    ok("narration posts out via the webhook sender (own identity, no gateway)")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("on-site agent stands down on operator confirm - same Worker, different seat")


# --- scenario 10: prompt composition - role isolation ---------------------
async def scenario_prompts() -> None:
    print("scenario: skills/ prompt composition (role isolation)")
    triage = load_system("crypto", "triage").lower()
    specialist = load_system("rev", "specialist").lower()

    # triage gets the move-fast doctrine + the crypto playbook + common rules
    assert "triage agent" in triage, "triage role doctrine not loaded"
    assert "anti-rabbit-hole" in triage, "triage hard rule missing"
    assert "never submit a guess" in triage, "common rules not loaded"
    assert "rsa" in triage, "crypto lane playbook not loaded"
    ok("triage = common + env + triage role + lane playbook")

    # specialist is NOT poisoned by the triage 'bail fast' bias
    assert "specialist solver" in specialist, "specialist role doctrine not loaded"
    assert "anti-rabbit-hole" not in specialist, "triage bias leaked into specialist!"
    assert "reverse engineering" in specialist, "rev lane playbook not loaded"
    ok("specialist NOT poisoned by triage 'move fast' doctrine")


# --- scenario 11: skill docs are readable, but only through the skill root --
async def scenario_skill_tools() -> None:
    print("scenario: skill-library tools")
    toolbox = Toolbox(
        os.path.join("_files", "simskills"),
        skills_dir=os.path.join("cddc", "skills"),
    )

    docs = await toolbox.run("list_skill_docs", {"path": "lanes/ctf-crypto"})
    assert "lanes/ctf-crypto/SKILL.md" in docs, "crypto skill docs not listed"

    body = await toolbox.run("read_skill_doc", {"path": "lanes/ctf-crypto/SKILL.md"})
    assert "rsa" in body.lower(), "crypto skill doc not readable"

    escaped = await toolbox.run("read_skill_doc", {"path": "../README.md"})
    assert "path escapes skills dir" in escaped, "skill reader allowed path escape"
    ok("agents can list/read skill docs without escaping cddc/skills")


async def main() -> None:
    await scenario_routing()
    print()
    await scenario_steer_and_race()
    print()
    await scenario_control()
    print()
    await scenario_solo()
    print()
    await scenario_agent()
    print()
    await scenario_sandbox_and_gating()
    print()
    await scenario_escalation()
    print()
    await scenario_webhook()
    print()
    await scenario_prompts()
    print()
    await scenario_skill_tools()
    # The real-Docker scenario is opt-in (needs the ctf-sandbox image built).
    if os.environ.get("CDDC_SIM_DOCKER"):
        print()
        await scenario_sandbox_agent()
    print("\nALL CHECKS PASSED [done]  (no Discord token, no model key, no cost)")


if __name__ == "__main__":
    asyncio.run(main())
