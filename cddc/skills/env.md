# Current Environment

SWAPPABLE SNIPPET - facts about the box you are on RIGHT NOW, not doctrine. Update
this when the toolchain changes; do not bake it into the role/lane prompts.

- Your `run_shell` runs inside the **ctf-sandbox Linux container** - a real CTF
  toolchain, not a bare host. Don't assume you lack a tool: check first. (If the
  tools below are "command not found", you're in a reduced/local shell - say so.)
- **Your installed toolset is listed in `/opt/cddc-*.txt`.** `cat /opt/cddc-pwn.txt`
  (or crypto / rev / stego / recon) to see exactly what you have for your lane
  before deciding something can't be done here.
- `web_search` / `read_url` are available (Serper or DuckDuckGo, + Jina) - look up
  a CVE, version, error string, attack name, or writeup. `fetch_url` is for the
  challenge's OWN target/host only.

What's installed, by area (cat the manifest for the full list):

- **recon (everywhere):** gcc/g++/make, binutils (objdump/nm/readelf/strings),
  gdb, radare2, checksec, patchelf, file/xxd.
- **web:** requests/httpx, beautifulsoup4/lxml, pyjwt + `jwt_tool`, flask (host an
  OOB/SSRF/XSS callback listener), websocket-client, name-that-hash, node/npm. No
  scanners/brute - jeopardy web is crafting one precise request.
- **crypto:** SageMath IS available - use it (lattices, poly GCD over Z_n[x], ECC).
  Also pycryptodome, sympy, z3, gmpy2, fpylll, pari-gp, RsaCtfTool, hashcat, john.
- **stego / media:** steghide, stegseek (passphrase cracker), zsteg, binwalk,
  foremost, exiftool, zbarimg, tesseract, imagemagick, ffmpeg, sox, stegolsb.
- **pwn:** pwntools, gef (bata24 fork, auto-loaded in gdb), pwninit,
  glibc-all-in-one + libc-database (in /opt), one_gadget, seccomp-tools, ROPgadget,
  ropper, angr/angrop, gdb-multiarch + qemu (cross-arch / kernel). For the EXACT
  remote libc/ld, `docker compose up` the challenge's own container (see Docker).
- **rev:** Ghidra headless + **`ghidra-rpc`** (decompile over JSON - `ghidra-rpc
  start --detach`, `load <bin>`, `decompile <prog> <func>`; Go analyzer included),
  jadx (Android), ilspycmd (.NET), pycdc (python bytecode), frida, qiling, lief,
  pefile. Rust: `oxidizer` runs as a sidecar container (build + run /opt/oxidizer
  via the docker socket).
- **forens:** tshark (pcap), volatility3 (memory dumps), sleuthkit (disk images).
  Eric Zimmerman `MFTECmd`/`EvtxECmd`/`RECmd` are baked; pull MORE net9 EZ tools on
  demand (the how-to is in `/opt/cddc-forens.txt`). Light stego/media tools
  (steghide, zsteg, binwalk, exiftool, zbarimg...) are in the stego set above.
- **ai:** an ISOLATED venv - run ML/torch with **`ai-python`** (NOT system
  `python3`): torch + torchvision (GPU when the host exposes one, else CPU),
  transformers, scikit-learn, scipy, pandas. (Isolated so its numpy can't clash
  with Sage's in the system python.)

- **Docker** (only if `docker` works - the host socket is bound for specialist /
  deep roles, not triage): `docker compose up` / `docker run` a service challenge
  against the host daemon. Containers you start are auto-labeled
  `cddc.thread=$CDDC_THREAD` and reaped per-challenge - no cleanup needed. If you
  create one another way (python docker SDK, raw API, podman), label it
  `cddc.thread=$CDDC_THREAD` yourself or it leaks.
