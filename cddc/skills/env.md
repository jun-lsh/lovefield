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

- **stego / media:** steghide, stegseek (passphrase cracker), zsteg, binwalk,
  foremost, exiftool, zbarimg, tesseract, imagemagick, ffmpeg, sox, stegolsb.

- **Docker** (only if `docker` works - the host socket is bound for specialist /
  deep roles, not triage): `docker compose up` / `docker run` a service challenge
  against the host daemon. Containers you start are auto-labeled
  `cddc.thread=$CDDC_THREAD` and reaped per-challenge - no cleanup needed. If you
  create one another way (python docker SDK, raw API, podman), label it
  `cddc.thread=$CDDC_THREAD` yourself or it leaks.
