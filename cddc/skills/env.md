# Current Environment

SWAPPABLE SNIPPET - facts about the box you are on RIGHT NOW, not doctrine. Update
this when the toolchain changes; do not bake it into the role/lane prompts.

- Your `run_shell` runs inside the **ctf-sandbox Linux container** - a FULL CTF
  toolchain (r2, Ghidra, pwntools, sage, volatility (`vol`), ...), NOT a bare host.
  **NEVER state a tool is missing without running `command -v <tool>` first.**
  Assuming "probably no decompiler here" and escalating on that basis is a triage
  ERROR - the tool is almost certainly installed. Only if `command -v` actually
  returns nothing (AND it's absent from the manifest) is it really not here.
- **Your toolset is listed in `/opt/cddc-*.txt`** - `cat /opt/cddc-rev.txt` (or
  pwn / crypto / stego / web / forens / recon) for exact names before deciding
  something can't be done here. NAMING TRAPS: the decompiler is the shared
  **`decompiler`** MCP service (or `ghidra-headless` for a manual run) - there is no
  bare `ghidra` command, and `ghidra-rpc` / `dc` no longer exist. The disassembler is
  **`r2`** / `radare2`. Check the real name before you write it off.
- `web_search` / `read_url` are available (Serper or DuckDuckGo, + Jina) - look up
  a CVE, version, error string, attack name, or writeup. `fetch_url` is for the
  challenge's OWN target/host only.

# Installed Packages

Distro: Ubuntu 22.04

```
gcc/g++/make
binutils (objdump/nm/readelf/strings)
gdb
radare2
checksec
patchelf
file/xxd
python3
```

Lane-specific packages and libraries are listed under their respective
`cddc/skills/lanes/*/SKILL.md` files.

**Need a tool that isn't here? INSTALL it** - don't get stuck: `pip install <x>` /
`uv pip install --system <x>`, `gem install <x>`, or (you have passwordless sudo)
`sudo apt-get update && sudo apt-get install -y <pkg>`.

- **stego / media:** steghide, stegseek (passphrase cracker), zsteg, binwalk,
  foremost, exiftool, zbarimg, tesseract, imagemagick, ffmpeg, sox, stegolsb.
- **pwn:** pwntools, gef (bata24 fork, auto-loaded in gdb), pwninit,
  glibc-all-in-one + libc-database (in /opt), one_gadget, seccomp-tools, ROPgadget,
  ropper, angr/angrop, gdb-multiarch + qemu (cross-arch / kernel). For the EXACT
  remote libc/ld, `docker compose up` the challenge's own container (see Docker).
- **rev:** decompile via the shared **`decompiler`** MCP service (one always-warm
  headless-Ghidra server with GolangAnalyzer for Go). The Claude harness has it wired:
  `import_binary` the binary's `/files/<...>` path (analyses ONCE, cached for the whole
  challenge), then list/decompile functions BY NAME (read the exact name first - Go funcs
  are `main.main`, not `main`). For a manual decompile in-box, `ghidra-headless` runs
  Ghidra directly. (`ghidra-rpc` and the old `dc` client are gone.) Also: jadx (Android),
  ilspycmd (.NET), pycdc (python bytecode), frida, qiling, lief, pefile. Rust: `oxidizer`
  runs as a sidecar container via the docker socket.
- **forens:** tshark (pcap), volatility (`vol`, memory dumps), sleuthkit (disk images).
  Eric Zimmerman `MFTECmd`/`EvtxECmd`/`RECmd` are baked; pull MORE net9 EZ tools on
  demand (the how-to is in `/opt/cddc-forens.txt`). Light stego/media tools
  (steghide, zsteg, binwalk, exiftool, zbarimg...) are in the stego set above.
- **ai:** an ISOLATED venv - run ML/torch with **`ai-python`** (NOT system
  `python3`): torch + torchvision (GPU when the host exposes one, else CPU),
  transformers, scikit-learn, scipy, pandas. (Isolated so its numpy can't clash
  with Sage's in the system python.)

- **Docker = the challenge's REAL environment. USE IT.** If the challenge ships a
  `Dockerfile` or `docker-compose.yml`, that is exactly how it is meant to run -
  BUILD AND RUN IT, do NOT approximate it on the bare box. (pwn: the container pins
  the exact libc/ld your exploit must match; web: it IS the live target.) The host
  docker socket is bound in, so:
  - **service** -> run it **DETACHED** so the call returns at once, then connect:
    `docker compose up -d`  or  `docker build -t chal . && docker run -d -p 1337:1337 chal`.
  - the **build is slow** and will blow the 30s default - pass a generous
    **`timeout_sec`** (e.g. 300) to run_shell for the build, or build detached + poll.
  - containers you start are auto-labeled `cddc.thread=$CDDC_THREAD` and reaped per
    challenge - no cleanup needed; if you make one another way (python docker SDK,
    raw API) label it `cddc.thread=$CDDC_THREAD` yourself or it leaks.
  If `docker` is "permission denied" / "not found", your box wasn't given the socket
  - say so rather than silently faking the environment locally.
