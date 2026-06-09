# Spin up a CTF sandbox box (standalone)

`run-box.sh` gives you an **interactive box** with the full CTF toolchain (r2, Ghidra,
pwntools, sage, volatility, decompilers, forensics, ...) and drops you into
**claude**, **codex**, or a **bash** shell, with your files mounted at `/challenge`.
No Discord, no bot — just you and a box.

```sh
sh sandbox/run-box.sh [claude|codex|bash] [files-dir]
```

## One-time prerequisites

1. **Build the image** (once; ~15-30 min the first time):
   ```sh
   docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox sandbox
   ```
2. **Log in to your CLI on the HOST** (so the box can read your login — needs a browser,
   which the box doesn't have). Skip this for `bash`.
   - **codex:** `npm i -g @openai/codex` then `codex login` (Google/ChatGPT). Writes `~/.codex`.
   - **claude:** install Claude Code on your host and `claude` once to log in (subscription).
     Writes `~/.claude/.credentials.json`.

   The script mounts whichever of `~/.codex` / `~/.claude` exist into the box and aligns
   the box user to your uid so the CLI is already authenticated. **It never sets
   `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`** — your *login* is used, so no metered API burn.

## Usage

```sh
sh sandbox/run-box.sh bash   ./mychall      # shell in the box; files at /challenge
sh sandbox/run-box.sh codex  ./mychall      # Codex on your ChatGPT/Google login
sh sandbox/run-box.sh claude ./mychall      # Claude Code on your subscription
CDDC_DEEPSEEK=1 sh sandbox/run-box.sh claude ./mychall   # Claude, but on DeepSeek (cheap)
```
The box is **removed when you exit** the CLI. Pass `KEEP=1` to leave it running (the script
prints the re-enter / remove commands).

## Picking the layers (image)

The everyday image is `ctf-sandbox` (the whole toolchain). For a leaner box, build just the
layer you want and point the script at it with `IMAGE=`:

| You want | Build | Run |
|---|---|---|
| everything (default) | `docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox sandbox` | `sh sandbox/run-box.sh bash ./x` |
| ML / LLM (torch) | `docker build -f sandbox/Dockerfile.sandbox --target ai -t ctf-sandbox:ai sandbox` | `IMAGE=ctf-sandbox:ai sh sandbox/run-box.sh bash ./x` |
| just crypto | `docker build -f sandbox/Dockerfile.sandbox --target crypto -t ctf-sandbox:crypto sandbox` | `IMAGE=ctf-sandbox:crypto sh sandbox/run-box.sh bash ./x` |
| just pwn / rev / forens / web | `--target pwn` (or rev/forens/web) `-t ctf-sandbox:pwn` | `IMAGE=ctf-sandbox:pwn ...` |

Cumulative chain (each includes the ones before it):
`base → recon → harness → crypto → web → stego → pwn → rev → forens → ai`.

## Options (env vars)

| var | effect |
|---|---|
| `IMAGE` | image/tag to run (default `ctf-sandbox`) |
| `AGENT_CMD` | override the launch command, e.g. `AGENT_CMD='codex --full-auto' sh sandbox/run-box.sh codex ./x` |
| `CDDC_DEEPSEEK=1` | run **claude** against DeepSeek's Anthropic endpoint (cheap); needs `DEEPSEEK_API_KEY` in `cddc/.env` |
| `KEEP=1` | don't remove the box on exit (re-enter / inspect) |
| `BOX_NAME` | container name (default `cddc-box-<agent>`) |

## Inside the box

- Your files are at **`/challenge`** (your working dir). The full toolchain is on `PATH`.
- **What's installed:** `cat /opt/cddc-*.txt` (recon/crypto/web/stego/pwn/rev/forens) for the
  exact tool names. `command -v <tool>` before assuming something's missing.
- **Lane playbooks** (curated approaches) are mounted read-only at `/opt/cddc-skills/lanes/ctf-<lane>/`.
- **Decompiler:** if you set `CDDC_SANDBOX_NETWORK` (the shared `cddc-decompiler` net), claude
  gets a `/challenge/.mcp.json` so its `decompiler` MCP works (`import_binary /files/<bin>`, then
  decompile by name); for codex/bash use `ghidra-headless`. See the Ghidra section below.
- **Docker-in-the-box:** the host socket is bound when the bot runs; in this standalone box it
  isn't, so `docker compose up` a challenge's own service from your host instead.

## Decompiler (Ghidra) — for rev challenges

The decompiler is a **separate, shared, always-warm container** (`cddc-decompiler`) running
headless Ghidra + GolangAnalyzer. Your box reaches it over a docker network; you analyse a
binary **once** and reuse it. Bring it up when you need rev.

1. **Build the decompiler image** (once; reuses the Ghidra from the rev layer):
   ```sh
   docker build -f sandbox/Dockerfile.sandbox --target decompiler -t ctf-sandbox:decompiler sandbox
   ```
2. **Start it, pointed at your challenge files dir** (creates the `cddc-net` network and the
   `cddc-decompiler` container; it reads that dir at `/files`):
   ```sh
   CDDC_FILES_DIR=./mychall sh sandbox/run-decompiler.sh
   # prints: "mounting files: <abs dir> -> /files"   |  logs: docker logs -f cddc-decompiler
   ```
3. **Run your box on that net, with the SAME files dir** — `run-box.sh` mirrors it at `/files`
   so the path you see is the path the decompiler sees:
   ```sh
   CDDC_SANDBOX_NETWORK=cddc-net sh sandbox/run-box.sh claude ./mychall   # or bash / codex
   ```
4. **Use it** (inside the box):
   - **claude** → the `decompiler` MCP is auto-wired (`/challenge/.mcp.json`). Tell it to
     `import_binary` the path **`/files/<bin>`** (that exact path exists in your box — `ls /files`),
     then list functions and decompile **by name** (Go: `main.main`, not `main`).
   - **codex / bash** → no MCP client is baked into the box. Run Ghidra directly with
     **`ghidra-headless`** (the `analyzeHeadless` CLI), or just use the `claude` box for rev.
     (codex can also be pointed at the MCP via its own `~/.codex/config.toml`.)
5. **Stop it** when done: `docker rm -f cddc-decompiler` (the analysis persists in the
   `cddc-ghidra-proj` volume; `docker volume rm cddc-ghidra-proj` to wipe it).

Notes: one decompiler per files-dir; the first `import_binary` runs analysis in the
background (the agent polls the MCP for status). For a *non*-rev challenge you don't need
any of this — skip it. (`dc` and `ghidra-rpc` are gone — the MCP is the decompiler now.)

## Troubleshooting

- **`image 'ctf-sandbox' not found`** → build it (see prerequisites).
- **codex errors about its sandbox** inside the container → `AGENT_CMD='codex --full-auto' sh sandbox/run-box.sh codex ./x` (or `--dangerously-bypass-approvals-and-sandbox`).
- **claude says "not logged in"** → you didn't `claude` login on the host, so `~/.claude/.credentials.json` is missing. Log in on the host first (or use `CDDC_DEEPSEEK=1` with a DeepSeek key).
- **codex says "not logged in"** → run `codex login` on the host first; the script mounts `~/.codex`.
- **`current working directory is outside of container mount namespace root`** → that's runc's
  CVE-2024-21626 guard; the script already works around it (`docker exec -w /` + `cd`). If you
  `docker exec` by hand, do the same: `docker exec -it -w / <box> sh -c 'cd /challenge; exec bash'`.
