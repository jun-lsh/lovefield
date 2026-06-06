"""Lane registry: lane-name -> Lane config.

Phase 1 lanes carry a `dummy_script` (the steps the DummyWorker narrates) and
nothing else heavy - they're strategy/config, the Worker does the running.

Re-scope (see memory `lane-rescope-windows-specialist-hwresearch`):
  - `windows` is a MINOR lane - most Windows challs go to on-site laptops; we
    keep a stub for the odd detonate-and-revert.
  - heavy/resource-and-time work (kernel pwn, RF/radio, custom-arch) lives under
    the `deep_solver` tier, NOT under `windows`.
  - `hw_research` is the hardware-challenge research assist: identify the device
    -> recon how to interface -> run the on-the-spot commands (human-led, on-site).

Nothing here imports `discord`.
"""

from __future__ import annotations

from .base import Lane

LANES: dict[str, Lane] = {
    "research_run": Lane(
        "research_run",
        "solo",
        (
            "parse desc - pull software + version",
            "web_search public PoC / CVE",
            "stage PoC in disposable sandbox",
            "fire PoC at box via proxy",
            "capture candidate flag",
        ),
    ),
    "rev": Lane(
        "rev",
        "solo->race",
        (
            "triage_binary - file/checksec/strings/symbols",
            "decompile main",
            "crossverify control flow under gdb",
            "isolate flag-check logic",
            "derive + capture candidate flag",
        ),
    ),
    "pwn": Lane(
        "pwn",
        "solo->race",
        (
            "checksec_and_offsets",
            "find win()/leak primitive",
            "build exploit, test_exploit_locally",
            "run_remote through proxy (gated on local pass)",
            "capture candidate flag",
        ),
    ),
    "crypto": Lane(
        "crypto",
        "solo->race",
        (
            "sweep_encodings",
            "classify scheme",
            "run known attack (rsa_attacks / sage harness)",
            "verify decryption",
            "capture candidate flag",
        ),
    ),
    "web": Lane(
        # analysis-only on the remote tier; live exploitation is on-site.
        "web",
        "solo",
        (
            "recon(url) - fingerprint + paths",
            "static source review",
            "locate vuln, draft exploit",
            "hand interactive fire to on-site",
            "capture candidate flag",
        ),
        race_capable=False,
    ),
    "forensics": Lane(
        # flat + wide; guessy ones flag fast, don't grind. Not worth racing.
        "forensics",
        "solo",
        (
            "triage_file - binwalk/foremost/exiftool/strings/stego sweep",
            "follow the strongest lead",
            "carve / extract artifact",
            "capture candidate flag",
        ),
        race_capable=False,
    ),
    "ai": Lane(
        # ML/LLM challenges. Edge = a PC that won't die running pytorch.
        "ai",
        "solo->race",
        (
            "classify - prompt-injection / model-extraction / ML task",
            "probe the model / load the weights locally",
            "build the attack or train/run the task on GPU",
            "verify output",
            "capture candidate flag",
        ),
    ),
    "misc": Lane(
        # "dunno wtf" catch-all - weird programming / esoteric research. Thin
        # orchestration decides what kind of agent to throw at it.
        "misc",
        "solo",
        (
            "characterise the task - what is even being asked",
            "pick an approach (or ask the operator)",
            "implement + run",
            "capture candidate flag",
        ),
        race_capable=False,
    ),
    "deep_solver": Lane(
        # the heavy/privileged tier: kernel pwn, RF/radio, custom-arch, exotic
        # shellcode. Top model, big budget, long unattended, human glances.
        "deep_solver",
        "specialist",
        (
            "full-kit triage (qemu/angr/z3)",
            "open external notebook - checkpoint state",
            "reconstruct the hard primitive",
            "long grind with periodic thread checkpoints",
            "capture candidate flag",
        ),
    ),
    "windows": Lane(
        # MINOR lane - most Windows work goes to on-site laptops. Snapshottable
        # VM for the occasional detonate-and-revert only.
        "windows",
        "specialist",
        (
            "boot snapshot-clean Windows VM",
            "detonate / inspect under native tooling",
            "extract artifact",
            "revert snapshot, capture candidate flag",
        ),
        race_capable=False,
    ),
    "hw_research": Lane(
        # hardware-challenge research assist (on-site, human-led). Fast lookups +
        # interface recon for a human holding the device.
        "hw_research",
        "human-led",
        (
            "identify hardware (markings / FCC ID / datasheet search)",
            "recon interface (UART/SPI/I2C/JTAG/USB/RF - pinout + protocol)",
            "draft the commands to talk to it",
            "hand to on-site operator to run on the spot",
        ),
        race_capable=False,
    ),
    "raw": Lane(
        # escape hatch - trivial challs where harness overhead > the challenge.
        "raw",
        "raw",
        (
            "look - strings/base64/obvious",
            "grab the flag",
        ),
        race_capable=False,
    ),
}


def get_lane(name: str) -> Lane:
    try:
        return LANES[name]
    except KeyError:
        raise KeyError(f"unknown lane {name!r}; known: {sorted(LANES)}") from None
