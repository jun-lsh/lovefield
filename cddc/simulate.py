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

    # Operator-foresight `hard` flag routes straight to deep_solver (not a model
    # guess) - beats category, loses only to an explicit !lane override.
    ch = Challenge(id="hd", name="x", category="crypto", thread_id=2002, hard=True)
    w = await disp.dispatch(ch, ConsoleChannel(echo=False), autostart=False)
    assert w.lane.name == "deep_solver", f"hard flag should route to deep_solver, got {w.lane.name}"
    ch = Challenge(id="hd2", name="x", category="crypto", thread_id=2003, hard=True)
    w = await disp.dispatch(ch, ConsoleChannel(echo=False), lane_override="web", autostart=False)
    assert w.lane.name == "web", "!lane override must still beat the hard flag"
    ok("hard flag -> deep_solver (operator foresight); !lane override still wins")


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


# --- scenario 5a2: agent flag hygiene (reject placeholder submit_flag) -----
async def scenario_agent_flag_hygiene() -> None:
    print("scenario: agent flag hygiene (shared with the harness)")
    ch = Challenge(id="fh", name="hyg", category="crypto", thread_id=558,
                   description="submit a placeholder, then the real flag")
    chan = ConsoleChannel("crypto", echo=False)
    script = [
        Reply(content="submitting the format placeholder by mistake", tokens=10,
              tool_calls=[ToolCall("t1", "submit_flag", {"flag": "CDDC{...}"})]),
        Reply(content="ok the verified one", tokens=10,
              tool_calls=[ToolCall("t2", "submit_flag", {"flag": "CDDC{real_one}"})]),
    ]
    w = AgentWorker(
        get_lane("crypto"), ch, chan, id="wfh", name="crypto-fh",
        model=FakeModel(script), workdir=os.path.join("_files", "sim558"),
        max_steps=40, max_tokens=200_000, shell_timeout=10,
    )
    task = asyncio.create_task(w.run())

    # a placeholder submit is rejected (not halted on) - same hygiene as the harness
    assert await wait_until(
        lambda: any("ignored non-flag submit" in p for p in chan.posts), timeout=5
    )
    ok("placeholder submit_flag is rejected, not treated as a candidate")

    # the real flag then halts as a candidate for validation
    assert await wait_until(lambda: ch.state == "candidate", timeout=5)
    assert any("CDDC{real_one}" in p and "CANDIDATE FLAG" in p for p in chan.posts), \
        "real flag not announced as candidate"
    ok("real submit_flag halts as a candidate")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("agent stands down on !solved")


# --- scenario 5b: CLI harness worker (fake tmux session, no docker/key) ---
async def scenario_harness() -> None:
    print("scenario: CLI harness worker (fake tmux session)")
    from cddc.harness import FakeHarness
    from cddc.harness_worker import HarnessWorker

    ch = Challenge(
        id="h1", name="harness-warmup", category="crypto", thread_id=606,
        description="run a real CLI agent in tmux",
    )
    chan = ConsoleChannel("crypto", echo=False)
    # Cumulative pane snapshots (append-mostly scrollback). They contain BOTH a
    # placeholder (CDDC{...}, the format echoed in the prompt) AND a test/canary
    # flag the agent printed while working - NEITHER may be announced. Only the
    # .cddc_solution sentinel (set below) is an authoritative declaration.
    f1 = "$ claude\nthinking about the challenge..."
    f2 = f1 + "\nthe flag will look like CDDC{...}, let me inspect the files"
    f3 = f2 + "\nset a test canary: wrote CDDC{test_canary}\ninspecting the files"
    session = FakeHarness([f1, f2, f3], stay_alive=True)
    w = HarnessWorker(
        get_lane("crypto"), ch, chan,
        id="wh", name="crypto-h", session=session, cli="claude",
        max_minutes=999, poll_interval=0.02,
    )
    # Operator steers immediately - it must reach the live CLI as keystrokes.
    chan.inject_steer("try the vigenere angle")
    task = asyncio.create_task(w.run())

    # start() fed the stacked skills prompt + challenge brief to the CLI
    assert await wait_until(lambda: session.started, timeout=3)
    assert "Challenge: harness-warmup" in session.start_prompt, "task brief not handed to CLI"
    ok("harness starts the CLI session with the composed task prompt")

    # the steer is forwarded into the CLI and recorded in status()
    assert await wait_until(lambda: "try the vigenere angle" in session.sent, timeout=3)
    assert "try the vigenere angle" in w.status()["steers"]
    ok("operator steer is forwarded to the CLI and shows in status()")

    # the worker tails the agent's screen and posts the cleaned output
    assert await wait_until(lambda: any("inspecting the files" in p for p in chan.posts), timeout=3)
    ok("harness posts the agent's screen output as it appears")

    # neither the placeholder NOR the on-screen test canary becomes a candidate;
    # the canary is recorded as an unconfirmed token (trace only), never announced
    assert await wait_until(
        lambda: any("unconfirmed token on screen: CDDC{test_canary}" in f
                    for f in w.status()["findings"]),
        timeout=3,
    )
    assert "CDDC{test_canary}" not in w.status()["candidates"], "canary wrongly announced"
    assert "CDDC{...}" not in w.status()["candidates"], "placeholder wrongly treated as a flag"
    assert not any("CANDIDATE FLAG" in p for p in chan.posts), "announced with no declaration"
    ok("on-screen test/placeholder flags are NOT announced (no false candidate)")

    # the agent DECLARES the real flag by writing the .cddc_solution sentinel
    session.solution = "CDDC{harness_solve}\n"
    assert await wait_until(lambda: "CDDC{harness_solve}" in w.status()["candidates"], timeout=3)
    assert any("CDDC{harness_solve}" in p and "CANDIDATE FLAG" in p for p in chan.posts), \
        "declared flag not announced"
    assert ch.state == "solving", f"agent halted on a candidate (should keep running): {ch.state}"
    ok("declared flag (sentinel) announced; agent keeps running (no halt)")

    # operator confirms with !solved -> stands down AND tears the session down
    w.mark_solved()
    await task
    assert ch.state == "solved"
    assert session.stopped, "harness session not stopped on solve"
    ok("harness stands down on !solved and stops the CLI session")


# --- scenario 5c: candidate flag CAN still halt-and-validate (opt-in) ------
async def scenario_harness_halt() -> None:
    print("scenario: CLI harness worker (halt_on_flag opt-in)")
    from cddc.harness import FakeHarness
    from cddc.harness_worker import HarnessWorker

    ch = Challenge(id="h2", name="halt-warmup", category="crypto", thread_id=607)
    chan = ConsoleChannel("crypto", echo=False)
    session = FakeHarness(["working on it..."], stay_alive=True)
    # A NON-CDDC flag format: the sentinel is an explicit declaration, so its
    # contents are trusted regardless of prefix (different CTFs, different formats).
    session.solution = "NCO26{rsalcg?justd0-quadr4t1c!}"
    w = HarnessWorker(
        get_lane("crypto"), ch, chan,
        id="wh2", name="crypto-h2", session=session, cli="claude",
        max_minutes=999, poll_interval=0.02, halt_on_flag=True,
    )
    task = asyncio.create_task(w.run())
    assert await wait_until(lambda: ch.state == "candidate", timeout=3), "did not halt on flag"
    assert "NCO26{rsalcg?justd0-quadr4t1c!}" in w.status()["candidates"], \
        "non-CDDC sentinel flag not declared"
    ok("halt_on_flag=True halts on a declared flag (any format, e.g. NCO26{...})")
    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("halt path stands down on !solved")


# --- scenario 5d: harness model-summarizer (composes clean Discord lines) --
async def scenario_harness_summary() -> None:
    print("scenario: harness model-summarizer (composes Discord messages)")
    from cddc.harness import FakeHarness
    from cddc.harness_worker import HarnessWorker

    ch = Challenge(id="hsum", name="sum", category="crypto", thread_id=608)
    chan = ConsoleChannel("crypto", echo=False)
    # A raw TUI-ish frame: banner/box chrome + a real action line underneath.
    frame = (
        "╭─── Claude Code v2.1 ───╮\n│ Welcome back Mono! │\n╰────────────────────╯\n"
        "⏺ Bash(unzip chall.zip)\n  Archive: chall.zip\n  extracting strings.bin"
    )
    session = FakeHarness([frame], stay_alive=True)
    summ = FakeModel([Reply(content="unzipping chall.zip, extracting strings.bin", tokens=9)])
    w = HarnessWorker(
        get_lane("crypto"), ch, chan, id="whsum", name="crypto-hsum",
        session=session, cli="claude", max_minutes=999, poll_interval=0.02,
        summarizer=summ, summarize_every=0.0,  # compose as soon as there's output
    )
    task = asyncio.create_task(w.run())

    # a COMPOSED one-liner (from the summarizer) is posted, not the raw TUI frame
    assert await wait_until(
        lambda: any("unzipping chall.zip, extracting strings.bin" in p for p in chan.posts),
        timeout=3,
    )
    # the raw banner / box chrome never reaches Discord
    assert not any("Claude Code v2" in p or "Welcome back" in p for p in chan.posts), \
        "raw TUI chrome leaked to the feed"
    assert w.status().get("summary_tokens", 0) >= 9, "summary token accounting off"
    ok("summarizer composes a clean line; raw TUI chrome stays out of Discord")

    w.cancel()
    await task
    ok("summarizer harness stands down on !kill")


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
    # triage_report are always on (agent-handled), even on gated lanes.
    raw_tools = {s["function"]["name"] for s in _specs_for_lane(get_lane("raw"))}
    assert {"run_shell", "read_file", "write_file", "fetch_url", "submit_flag",
            "triage_report"} <= raw_tools, f"raw missing core tools: {raw_tools}"
    assert "triage_report" in rev_tools, "triage_report must be offered even on gated lanes"
    ok("rev gates out fetch_url; web grants it; raw offers all; submit_flag/triage_report always on")


# --- scenario 6b: docker-socket gating (role-gated, no docker) -----------
async def scenario_docker_socket_gating() -> None:
    print("scenario: docker socket - role helper + uniform shared box (no docker)")
    from cddc import config
    from cddc.sandbox import Sandbox

    # 1) role policy: triage withheld, any other role granted; "" disables all,
    #    CDDC_TRIAGE_SOCKET arms triage (the explicit exception knob).
    assert config.docker_sock_for_role("triage") == "", "triage must not get the socket"
    assert config.docker_sock_for_role("specialist") == config.DOCKER_SOCK, "specialist should get the socket"
    saved_sock = config.DOCKER_SOCK
    try:
        config.DOCKER_SOCK = ""
        assert config.docker_sock_for_role("specialist") == "", "empty CDDC_DOCKER_SOCK disables everywhere"
    finally:
        config.DOCKER_SOCK = saved_sock
    saved_tri = config.TRIAGE_SOCKET
    try:
        config.TRIAGE_SOCKET = True
        assert config.docker_sock_for_role("triage") == config.DOCKER_SOCK, "CDDC_TRIAGE_SOCKET should arm triage"
    finally:
        config.TRIAGE_SOCKET = saved_tri
    ok("role policy: triage withheld, specialist granted, both knobs flip it")

    # 2) _run_argv reflects the socket: bound + compose scope when set, absent when not.
    plain = Sandbox("ctf-sandbox", 777, os.path.join("_files", "simsock"))
    argv = plain._run_argv("/abs/wd")
    assert not any(a.endswith("docker.sock") for a in argv), "plain sandbox must not bind the socket"
    assert not any(a.startswith("COMPOSE_PROJECT_NAME") for a in argv), "plain sandbox should not set compose scope"
    socked = Sandbox(
        "ctf-sandbox", 778, os.path.join("_files", "simsock"),
        docker_sock="/var/run/docker.sock",
    )
    argv2 = socked._run_argv("/abs/wd")
    assert "/var/run/docker.sock:/var/run/docker.sock" in argv2, "socket not bound into run argv"
    assert "COMPOSE_PROJECT_NAME=cddc-778" in argv2, "compose scope missing"
    assert "CDDC_THREAD=778" in argv2, "thread id not exported for manual-run labeling"
    ok("Sandbox._run_argv binds the socket + compose scope only when docker_sock is set")

    # 3) the SHARED per-challenge box (#12): the dispatcher grants the box the
    #    socket UNIFORMLY (host opt-in, not role-gated) so any tier can adopt it,
    #    and a second dispatch on the SAME thread REUSES the very same box object
    #    (a handoff takes over the live container, it doesn't build a new one).
    reg = Registry()
    disp = Dispatcher(reg)
    saved_sb = config.CDDC_SANDBOX
    try:
        config.CDDC_SANDBOX = "docker"
        ch = Challenge(id="dk1", name="svc", category="web", thread_id=7001)
        triage = await disp.dispatch(
            ch, ConsoleChannel("web", echo=False),
            kind="agent", model=FakeModel([]), autostart=False,
        )
        assert triage.sandbox is not None and triage.sandbox.docker_sock == config.DOCKER_SOCK, \
            "triage's box must carry the socket (uniform access -> a persistent socket box)"
        # Escalation on the SAME thread (a fresh Challenge, same thread_id) must take
        # over the SAME box object - the heart of #12.
        ch_esc = Challenge(id="dk1", name="svc", category="web", thread_id=7001)
        spec = await disp.dispatch(
            ch_esc, ConsoleChannel("web", echo=False),
            kind="agent", model=FakeModel([]), autostart=False,
            role_override="specialist",
        )
        assert spec.sandbox is triage.sandbox, "escalation must REUSE the challenge's box, not build a new one (#12)"
        # REGRESSION (no-docker): the worker must hand its OWN sandbox to its
        # toolbox, else run_shell falls through to host execution while the
        # started container sits unused. This is checkable without docker.
        assert triage.toolbox.sandbox is triage.sandbox, "triage toolbox not wired to its sandbox -> run_shell would hit host"
        assert spec.toolbox.sandbox is spec.sandbox, "specialist toolbox not wired to its sandbox -> run_shell would hit host"
        # A different thread gets its OWN box; explicit docker_sock='' forces it off.
        ch3 = Challenge(id="dk3", name="svc", category="web", thread_id=7003)
        forced = await disp.dispatch(
            ch3, ConsoleChannel("web", echo=False),
            kind="agent", model=FakeModel([]), autostart=False,
            role_override="specialist", docker_sock="",
        )
        assert forced.sandbox is not triage.sandbox, "a different challenge must get its own box"
        assert forced.sandbox.docker_sock is None, "explicit docker_sock='' should force the socket off"
        # release_box destroys + forgets the box (challenge end: !kill / !solved).
        await reg.release_box(7001)
        assert 7001 not in reg.boxes, "release_box should forget the challenge's box"
    finally:
        config.CDDC_SANDBOX = saved_sb
    ok("shared box: socket uniform, escalation reuses the live box, release_box reaps it")


# --- scenario 6b2: handoff dossier (state continuity across takeover) -----
async def scenario_handoff_dossier() -> None:
    print("scenario: handoff dossier (state continuity across takeover, no docker)")
    from cddc.worker import append_dossier, read_dossier

    wd = os.path.join("_files", "simdossier")
    dpath = os.path.join(wd, ".cddc", "dossier.md")
    os.makedirs(os.path.dirname(dpath), exist_ok=True)
    if os.path.exists(dpath):
        os.remove(dpath)

    # 1) a worker's dossier_text captures its read + attempts + findings.
    ch = Challenge(id="hd1", name="cont", category="crypto", thread_id=9100, description="x")
    ch.difficulty = 4
    ch.technique = "RSA/Franklin-Reiter"
    ch.gist = "related messages; recover m"
    ch.recommendation = "deep_solver"
    chan = ConsoleChannel("crypto", echo=False)
    prior = AgentWorker(
        get_lane("crypto"), ch, chan, id="wp", name="crypto-triage",
        model=FakeModel([]), workdir=wd, sandbox=None,
        max_steps=10, max_tokens=10_000, role="triage",
    )
    prior.tried = ["ran factordb", "tried small-e cube root"]
    prior.findings = ["n is 2048-bit", "e=3, two related ciphertexts"]
    sect = prior.dossier_text()
    assert "RSA/Franklin-Reiter" in sect and "factordb" in sect and "related ciphertexts" in sect, sect
    ok("dossier_text captures the worker's read, attempts, and findings")

    # 2) append accumulates across a ladder; read_dossier returns the whole history.
    p = append_dossier(wd, sect)
    assert p == "/challenge/.cddc/dossier.md", p
    append_dossier(wd, "## handoff from crypto-spec (specialist)\ntried: lattice reduction")
    body = read_dossier(wd)
    assert "crypto-triage" in body and "crypto-spec" in body, "dossier should ACCUMULATE handoffs"
    ok("append_dossier accumulates triage -> specialist; read_dossier returns the whole history")

    # 3) the INCOMING worker seeds the dossier into its starting context.
    ch2 = Challenge(id="hd2", name="cont", category="crypto", thread_id=9100, description="x")
    chan2 = ConsoleChannel("crypto", echo=False)
    script = [Reply(content="reading the handoff first", tokens=10,
                    tool_calls=[ToolCall("h1", "submit_flag", {"flag": "CDDC{cont}"})])]
    incoming = AgentWorker(
        get_lane("crypto"), ch2, chan2, id="wi", name="crypto-deep",
        model=FakeModel(script), workdir=wd, sandbox=None,
        max_steps=5, max_tokens=10_000, role="specialist",
    )
    task = asyncio.create_task(incoming.run())
    assert await wait_until(
        lambda: any("handoff dossier" in str(m.get("content", "")) for m in incoming.messages),
        timeout=5), "incoming worker did not seed the dossier into its context"
    assert any("lattice reduction" in str(m.get("content", "")) for m in incoming.messages), \
        "dossier content (prior attempts) missing from the incoming context"
    incoming.mark_solved()
    await task
    ok("incoming worker reads the persisted dossier into its starting context")


# --- scenario 6b3: headless Claude Code worker (stream-json events) -------
async def scenario_cc_headless() -> None:
    print("scenario: headless Claude Code worker (stream-json events, no docker)")
    import json as _json

    from cddc.cc_worker import CCWorker
    from cddc.headless import Event, FakeHeadless

    ch = Challenge(id="cc1", name="cc-chal", category="rev", thread_id=9200,
                   description="reverse the binary")
    chan = ConsoleChannel("rev", echo=False)
    wd = os.path.join("_files", "simcc")
    os.makedirs(wd, exist_ok=True)
    for fn in (".mcp.json", "CLAUDE.md", ".cddc_solution"):
        p = os.path.join(wd, fn)
        if os.path.exists(p):
            os.remove(p)

    session = FakeHeadless([[
        Event(kind="init", session_id="s1", text="model=deepseek-v4-pro mcp=['decompiler']"),
        Event(kind="text", text="Listing functions via the decompiler MCP."),
        Event(kind="tool", tool="mcp__decompiler__decompile_function", tool_input='{"name":"main"}'),
        Event(kind="result", text="Recovered the key; flag written.", cost_usd=0.0021, tokens=1200),
    ]], solution="CDDC{cc_headless}")
    w = CCWorker(
        get_lane("rev"), ch, chan, id="wcc", name="rev-cc",
        session=session, role="specialist",
        decompiler_url="http://cddc-decompiler:8000/mcp", workdir=wd, halt_on_flag=True,
    )
    task = asyncio.create_task(w.run())

    assert await wait_until(lambda: ch.state == "candidate", timeout=5), \
        f"cc worker never reached candidate; posts={chan.posts!r}"
    assert "CDDC{cc_headless}" in w.status()["candidates"], "declared flag not captured"
    assert any("tool `mcp__decompiler__decompile_function`" in p for p in chan.posts), \
        "structured tool call not narrated"
    assert any("turn done" in p and "$" in p for p in chan.posts), "result/cost not narrated"
    # box config written for MCP + standalone (docker exec -it box claude) use
    with open(os.path.join(wd, ".mcp.json"), encoding="utf-8") as f:
        mcp = _json.load(f)
    assert mcp["mcpServers"]["decompiler"]["headers"]["Host"] == "localhost:8000", mcp
    assert os.path.exists(os.path.join(wd, "CLAUDE.md")), "CLAUDE.md not written"
    ok("cc worker narrates events, writes .mcp.json(+Host)/CLAUDE.md, declares flag via sentinel")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    assert session.stopped, "session not stopped on solve"
    ok("cc worker stands down on !solved and stops the session")

    # triage tier: the agent writes .cddc/triage.md -> worker posts a TRIAGE REPORT and
    # captures difficulty/recommendation onto the challenge (for !escalate + the dossier).
    wd2 = os.path.join("_files", "simcctri")
    os.makedirs(os.path.join(wd2, ".cddc"), exist_ok=True)
    sol = os.path.join(wd2, ".cddc_solution")
    if os.path.exists(sol):
        os.remove(sol)
    with open(os.path.join(wd2, ".cddc", "triage.md"), "w", encoding="utf-8") as f:
        f.write("gist: RSA with small e\ncategory: crypto\ntechnique: Hastad cube root\n"
                "difficulty: 3\nblockers: needs sage\nrecommendation: escalate\n")
    ch_t = Challenge(id="ct1", name="tri", category="crypto", thread_id=9300, description="x")
    chan_t = ConsoleChannel("crypto", echo=False)
    sess_t = FakeHeadless([[
        Event(kind="init", session_id="t1", text="model=deepseek-v4-flash"),
        Event(kind="text", text="assessing the challenge"),
        Event(kind="result", text="triage filed", tokens=200),
    ]], solution="")
    wt = CCWorker(get_lane("crypto"), ch_t, chan_t, id="wt", name="crypto-tri",
                  session=sess_t, role="triage", workdir=wd2)
    task_t = asyncio.create_task(wt.run())
    assert await wait_until(lambda: ch_t.state == "needs_human", timeout=5), \
        f"triage worker didn't halt with a report; posts={chan_t.posts!r}"
    assert ch_t.difficulty == 3 and ch_t.recommendation == "escalate", \
        f"triage fields not captured: diff={ch_t.difficulty} rec={ch_t.recommendation!r}"
    assert any("TRIAGE REPORT" in p for p in chan_t.posts), "no triage report block posted"
    ok("cc triage tier files .cddc/triage.md -> TRIAGE REPORT + captured difficulty/recommendation")
    wt.cancel()
    await task_t


# --- scenario 6c: web_search + read_url (provider-agnostic, fake backend) -
async def scenario_web_search() -> None:
    print("scenario: web_search + read_url (provider-agnostic, no network)")
    from cddc import search

    # factory routing: none disables search; ddg/serper return a callable; the
    # Jina reader is always available (keyless).
    assert search.make_searcher("none") is None, "provider 'none' must disable web search"
    assert search.make_searcher("") is None, "empty provider must disable web search"
    assert callable(search.make_searcher("ddg")), "ddg should yield a searcher"
    assert callable(search.make_searcher("serper", serper_key="k")), "serper should yield a searcher"
    assert callable(search.make_reader()), "reader (jina) is always available"
    ok("make_searcher routes provider + disables on none/empty; reader always on")

    # Toolbox with injected fakes - no network, no key.
    async def fake_search(query, num=None):
        hits = [
            {"title": "CVE-2024-1234 in libfoo", "url": "https://nvd/x", "snippet": "RCE in libfoo < 2.1"},
            {"title": "ctf writeup: padding oracle", "url": "https://blog/y", "snippet": "solved via CBC padding oracle"},
            {"title": "third", "url": "https://z", "snippet": ""},
        ]
        return hits[: (num or 5)]

    async def fake_read(url):
        return f"# clean markdown of {url}\n\nthe full extracted body text"

    tb = Toolbox(os.path.join("_files", "simsearch"), searcher=fake_search, reader=fake_read)
    out = await tb.run("web_search", {"query": "libfoo CVE", "num_results": 2})
    assert "CVE-2024-1234" in out and "https://nvd/x" in out, f"web_search digest wrong: {out!r}"
    assert "padding oracle" in out, "second result missing from digest"
    assert "https://z" not in out, "num_results not honored (should cap at 2)"
    ok("web_search returns a normalized title/url/snippet digest, honoring num_results")

    rd = await tb.run("read_url", {"url": "https://blog/y"})
    assert "clean markdown of https://blog/y" in rd, f"read_url wrong: {rd!r}"
    ok("read_url returns clean extracted text")

    # unconfigured Toolbox -> clear message, never a crash.
    tb_off = Toolbox(os.path.join("_files", "simsearch_off"))
    off = await tb_off.run("web_search", {"query": "x"})
    assert "not configured" in off, f"unconfigured web_search should say so: {off!r}"
    ok("web_search degrades cleanly when no provider is configured")

    # gating: web_search + read_url are broadly offered even on a gated lane
    # (rev), while fetch_url stays gated out of offline-analysis lanes.
    rev_tools = {s["function"]["name"] for s in _specs_for_lane(get_lane("rev"))}
    assert {"web_search", "read_url"} <= rev_tools, "web tools must be offered on gated lanes too"
    assert "fetch_url" not in rev_tools, "fetch_url should still be gated out of rev"
    ok("web_search + read_url broadly granted (even rev); fetch_url stays gated")


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
    await sb.release()  # drop any stale box from a prior run (start() now REUSES a live one)
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
    # The box now PERSISTS past the worker (#12): a worker exit must NOT remove it
    # (a fresh brain could take over). It's reaped explicitly at challenge end.
    assert sb.started, "box must persist after the worker exits (takeover), not be torn down"
    assert not any("removed" in p for p in chan.posts), "worker should no longer announce a teardown"
    await sb.release()
    assert not sb.started, "release() must actually remove the box"
    ok("box persists across the worker's exit; release() reaps it at challenge end")


# --- scenario 7b: real harness pipeline (docker + claude CLI in tmux) ------
# Opt-in (CDDC_SIM_HARNESS=1): needs docker, the ctf-sandbox image, a host tmux,
# and a real claude login on the host (~/.claude). It drives the ACTUAL claude
# CLI on a real challenge, so it spends real subscription quota.
async def scenario_harness_docker() -> None:
    print("scenario: real harness pipeline (docker + claude CLI on a real challenge)")
    import shutil

    from cddc import config
    from cddc.harness import TmuxHarness, credential_mounts
    from cddc.harness_worker import HarnessWorker
    from cddc.sandbox import Sandbox

    src = os.path.expanduser(os.environ.get(
        "CDDC_SIM_CHALLENGE",
        "~/ctf/grey26/dist-elite_ball_knowledge/elite_ball_knowledge.zip",
    ))
    assert os.path.exists(src), f"challenge file not found: {src}"
    max_minutes = float(os.environ.get("CDDC_SIM_HARNESS_MINUTES", "10"))

    thread_id = 909
    workdir = os.path.join("_files", f"sim{thread_id}")
    os.makedirs(workdir, exist_ok=True)
    dst = os.path.join(workdir, os.path.basename(src))
    shutil.copy(src, dst)  # drop the distfile into the bind-mounted workdir

    ch = Challenge(
        id="ebk", name="baby_bof", category="pwn", thread_id=thread_id,
        description=(
            f"The distribution file {os.path.basename(src)} is in your working "
            "directory. Unzip it if needed, analyze the challenge, and recover the "
            "flag. Flags look like grey{...}. There is a dummy flag.txt in the folder for your testing, there is no real flag in the distribution."
        ),
        files=[dst],
    )
    chan = ConsoleChannel("pwn", echo=True)  # echo live so we watch it work
    sandbox = Sandbox(
        config.CDDC_SANDBOX_IMAGE, thread_id, workdir,
        extra_mounts=credential_mounts(config.HARNESS_USER),
    )
    keys = [k.strip() for k in config.CLAUDE_STARTUP_KEYS.split(",") if k.strip()]
    session = TmuxHarness(
        "claude", sandbox, workdir,
        launch_cmd=config.CLAUDE_CLI_CMD,
        session_name=f"cddc-{thread_id}-claude",
        user=config.HARNESS_USER,
        startup_keys=keys,
    )
    # Cheap DeepSeek summarizer narrates claude's TUI into clean lines (set
    # DEEPSEEK_API_KEY to see it; otherwise the feed is the cleaned raw output).
    summ = None
    if config.HARNESS_SUMMARIZE and config.DEEPSEEK_API_KEY:
        from cddc.models import DeepSeekClient
        summ = DeepSeekClient(config.DEEPSEEK_API_KEY, config.DEEPSEEK_BASE_URL, config.CHURN_MODEL)
    w = HarnessWorker(
        get_lane("misc"), ch, chan, id="whd", name="misc-harness",
        session=session, cli="claude", max_minutes=max_minutes, poll_interval=5,
        summarizer=summ, summarize_every=config.HARNESS_SUMMARIZE_SECS,
    )
    task = asyncio.create_task(w.run())

    # "Runs properly" = claude cleared its startup gate and is actively working:
    # any `[claude] ...` post means real output flowed (a summarized line or, with
    # no summarizer, a cleaned delta). Gate/sandbox posts are `[harness]`/
    # `[sandbox]`, so a `[claude]` post is an unambiguous "it's producing" signal.
    def has_cli_output() -> bool:
        return any(p.startswith("[claude] ") and len(p) > len("[claude] ") for p in chan.posts)

    assert await wait_until(has_cli_output, timeout=max_minutes * 60, interval=2), (
        "claude never produced CLI output - it is likely stuck on a startup gate; "
        f"last posts: {chan.posts[-3:]}"
    )
    ok("claude cleared the startup gate and is producing output")

    # Candidate flags don't halt by default - let it run until it announces a real
    # flag, the worker stands down on its own, or the budget elapses.
    await wait_until(
        lambda: w.status()["candidates"] or ch.state in ("killed", "solved", "needs_human"),
        timeout=max_minutes * 60, interval=2,
    )
    cands = w.status()["candidates"]
    print(f"  [info] state={ch.state}; steps={w.current_step}; candidates={cands}")
    assert "CDDC{...}" not in cands, "placeholder leaked into candidates"
    if cands:
        ok(f"agent announced candidate flag(s) and kept running: {cands}")
    else:
        ok(f"agent ran the full pipeline (state={ch.state})")
    w.cancel()  # stop the live agent + tear down the container
    await task
    ok("harness pipeline completed and tore down the container")


# --- scenario 8: triage report (advise -> deny -> continue -> solve) ------
async def scenario_escalation() -> None:
    print("scenario: triage report (advise -> deny -> continue -> solve)")
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
              tool_calls=[ToolCall("t2", "triage_report",
                                   {"gist": "RSA with related messages; recover m",
                                    "difficulty": 4, "technique": "RSA/Franklin-Reiter",
                                    "blockers": "needs lattice tooling / sage; no cheap one-shot",
                                    "recommendation": "deep_solver", "confidence": "medium"})]),
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

    # triage files a REPORT and HALTS (needs_human), recording its read +
    # recommendation on the challenge for the handoff
    assert await wait_until(lambda: ch.state == "needs_human", timeout=5)
    assert ch.difficulty == 4, f"difficulty not captured: {ch.difficulty}"
    assert ch.technique == "RSA/Franklin-Reiter", "technique not captured"
    assert ch.recommendation == "deep_solver", f"recommendation not captured: {ch.recommendation!r}"
    assert ch.gist, "gist not captured"
    assert any("TRIAGE REPORT" in p for p in chan.posts), "no triage report block posted"
    assert any("DEEP_SOLVER" in p for p in chan.posts), "recommendation not rendered"
    ok("triage files a report (recommend, not decide) -> halts; read + recommendation captured")

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

    # tool RESULTS now post to the thread (the truncation fix), not just actions -
    # code-fenced, so the result header + a fenced block both appear
    assert any("run_shell ->" in p for p in chan.posts), "tool result header not posted"
    assert any(p.startswith("```") and "CDDC{related_msg}" in p for p in chan.posts), "fenced tool result not posted"
    ok("tool results are posted to the thread (code-fenced), not only fed to the model")

    w.mark_solved()
    await task
    assert ch.state == "solved"
    ok("agent stands down on !solved after a denied escalation")


# --- scenario 8b: forced triage call at the cap + steer-while-halted ------
async def scenario_budget_force_report() -> None:
    print("scenario: budget cap FORCES a triage call; !steer answers while halted")
    ch = Challenge(
        id="bf1", name="gamal", category="crypto", thread_id=909,
        description="ElGamal-ish scheme; recover the flag",
    )
    chan = ConsoleChannel("crypto", echo=False)
    script = [
        # one scoping step (no narration -> findings would be empty without help)
        Reply(content="", tokens=50, tool_calls=[ToolCall("a1", "run_shell", {"command": "echo scoping"})]),
        # the FORCED triage_report at the cap (the _force_report call)
        Reply(content="", tokens=40, tool_calls=[ToolCall("a2", "triage_report", {
            "gist": "ElGamal with an LSB oracle via Legendre symbol",
            "difficulty": 3, "technique": "ElGamal/LSB-oracle",
            "blockers": "no remote target host:port in the files",
            "recommendation": "solo_finish", "confidence": "high"})]),
        # the answer to the operator's question while halted
        Reply(content="No host:port appears in the supplied files or description; "
              "you'd need the organizer's netcat endpoint.", tokens=30),
    ]
    w = AgentWorker(
        get_lane("crypto"), ch, chan,
        id="wbf", name="crypto-bf", model=FakeModel(script),
        workdir=os.path.join("_files", "sim909"),
        max_steps=1, max_tokens=10_000_000, shell_timeout=10, role="triage",
    )
    task = asyncio.create_task(w.run())

    # hits the cap after 1 step -> FORCED triage_report (difficulty call), halts
    assert await wait_until(lambda: ch.state == "needs_human", timeout=5), f"never halted; posts={chan.posts!r}"
    assert ch.recommendation == "solo_finish", f"forced report not captured: {ch.recommendation!r}"
    assert ch.difficulty == 3 and ch.gist, "difficulty/gist not captured from the forced report"
    blk = next((p for p in chan.posts if "[budget]" in p), "")
    assert "forced triage call" in blk and "TRIAGE REPORT" in blk, f"cap didn't force the report block: {blk!r}"
    assert "SOLO_FINISH" in blk, "recommendation not rendered in the block"
    ok("budget cap FORCES a triage_report (difficulty call), not a free-text summary")

    # while halted, !steer a question -> agent answers and STAYS halted
    w.steer("is there a remote target in the files?")
    assert await wait_until(lambda: any("[answer]" in p for p in chan.posts), timeout=5), "no answer to the halted-steer"
    assert any("netcat endpoint" in p for p in chan.posts), "answer content missing"
    assert ch.state == "needs_human", "asking a question must NOT consume the decision"
    ok("!steer while halted is answered as a question; the worker stays halted")

    w.cancel()
    await task
    assert ch.state == "killed"
    ok("halted worker stands down on !kill")


# --- scenario 8c: solve_ready pings like a flag (local solve, no remote) --
async def scenario_local_solve() -> None:
    print("scenario: solve_ready pings like a flag (working local solve, no remote)")
    # offered on every lane (always-on tool)
    assert "solve_ready" in {s["function"]["name"] for s in _specs_for_lane(get_lane("rev"))}, \
        "solve_ready must be offered everywhere"
    ch = Challenge(id="ls1", name="svc", category="crypto", thread_id=910,
                   description="ElGamal oracle service")
    chan = ConsoleChannel("crypto", echo=False)
    script = [Reply(content="exploit works locally against a dummy", tokens=40, tool_calls=[
        ToolCall("s1", "solve_ready", {
            "summary": "LSB-oracle key recovery + AES decrypt",
            "needs": "remote host:port for the oracle"})])]
    w = AgentWorker(
        get_lane("crypto"), ch, chan, id="wls", name="crypto-ls",
        model=FakeModel(script), workdir=os.path.join("_files", "sim910"),
        max_steps=40, max_tokens=200_000, shell_timeout=10, role="triage",
    )
    task = asyncio.create_task(w.run())
    assert await wait_until(lambda: ch.state == "needs_human", timeout=5), f"never pinged; posts={chan.posts!r}"
    blk = next((p for p in chan.posts if "LOCAL SOLVE" in p), "")
    assert blk and "remote host:port" in blk, f"LOCAL SOLVE block missing/incomplete: {blk!r}"
    ok("solve_ready posts a LOCAL SOLVE block (bot maps it to a big ping) and halts")
    w.cancel()
    await task
    assert ch.state == "killed"
    ok("local-solve halt stands down on !kill")


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

    # both tiers are wired to the ctf skill library (so they read the lane writeups)
    assert "skill library" in triage and "read_skill_doc" in triage, "triage not wired to the skill library"
    assert "skill library" in specialist, "specialist not wired to the skill library"
    ok("both tiers know the ctf skill library exists and how to read it")


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
    await scenario_agent_flag_hygiene()
    print()
    await scenario_harness()
    print()
    await scenario_harness_halt()
    print()
    await scenario_harness_summary()
    print()
    await scenario_sandbox_and_gating()
    print()
    await scenario_docker_socket_gating()
    print()
    await scenario_handoff_dossier()
    print()
    await scenario_cc_headless()
    print()
    await scenario_web_search()
    print()
    await scenario_escalation()
    print()
    await scenario_budget_force_report()
    print()
    await scenario_local_solve()
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
    # The real harness scenario is opt-in (needs docker + ctf-sandbox + host tmux
    # + a real claude login). It spends real subscription quota.
    if os.environ.get("CDDC_SIM_HARNESS"):
        print()
        await scenario_harness_docker()
    print("\nALL CHECKS PASSED [done]  (no Discord token, no model key, no cost)")


if __name__ == "__main__":
    asyncio.run(main())
