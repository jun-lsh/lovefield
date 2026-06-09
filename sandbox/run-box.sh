#!/usr/bin/env sh
# run-box.sh - spin up an interactive CTF sandbox box, standalone (no Discord, no bot).
# Pick an agent (claude / codex / bash), mount your files, get a session inside the
# full ctf-sandbox toolchain. See sandbox/BOX.md for the full guide.
#
#   sh sandbox/run-box.sh [claude|codex|bash] [files-dir]
#
# Examples:
#   sh sandbox/run-box.sh bash   ./mychall      # shell in the box, files at /challenge
#   sh sandbox/run-box.sh codex  ./mychall      # Codex on your ChatGPT/Google login
#   sh sandbox/run-box.sh claude ./mychall      # Claude Code on your subscription login
#   CDDC_DEEPSEEK=1 sh sandbox/run-box.sh claude ./mychall   # Claude on DeepSeek (cheap)
#
# Env overrides:
#   IMAGE         image/tag to run (default ctf-sandbox; e.g. IMAGE=ctf-sandbox:ai, or a
#                 lean layer you built with `--target crypto -t ctf-sandbox:crypto`)
#   AGENT_CMD     override the launch command (e.g. AGENT_CMD='codex --full-auto')
#   CDDC_DEEPSEEK 1 -> run claude against DeepSeek's Anthropic endpoint (needs DEEPSEEK_API_KEY)
#   KEEP          1 -> leave the box running on exit (re-enter / inspect) instead of removing it
#   BOX_NAME      container name (default cddc-box-<agent>)
# Reads CDDC_SANDBOX_NETWORK / CDDC_DECOMPILER_URL / DEEPSEEK_API_KEY from cddc/.env.
set -eu

AGENT="${1:-bash}"
FILES="${2:-}"
IMAGE="${IMAGE:-ctf-sandbox}"
USERN=ctf
HOME_C="/home/$USERN"
BOX="${BOX_NAME:-cddc-box-$AGENT}"
ENV_FILE="${CDDC_ENV_FILE:-cddc/.env}"

_envget() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//; s/[[:space:]]*$//'; }

case "$AGENT" in
  claude|codex|bash|sh) ;;
  *) echo "usage: sh sandbox/run-box.sh [claude|codex|bash] [files-dir]"; exit 1 ;;
esac
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image '$IMAGE' not found. Build it first, e.g.:"
  echo "  docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox sandbox"
  exit 1
}

# --- mounts: files + your CLI login + the skills library --------------------
MOUNTS=""
FILES_ABS=""
if [ -n "$FILES" ]; then
  FILES_ABS="$(cd "$FILES" && pwd)"
  MOUNTS="$MOUNTS -v $FILES_ABS:/challenge"
fi
if [ -f "$HOME/.claude/.credentials.json" ]; then
  MOUNTS="$MOUNTS -v $HOME/.claude/.credentials.json:$HOME_C/.claude/.credentials.json"
fi
if [ -f "$HOME/.claude.json" ]; then
  MOUNTS="$MOUNTS -v $HOME/.claude.json:$HOME_C/.claude.json"
fi
if [ -d "$HOME/.codex" ]; then
  MOUNTS="$MOUNTS -v $HOME/.codex:$HOME_C/.codex"
fi
SKILLS="$(cd "$(dirname "$0")/../cddc/skills" 2>/dev/null && pwd || true)"
if [ -n "$SKILLS" ]; then
  MOUNTS="$MOUNTS -v $SKILLS:/opt/cddc-skills:ro"
fi
NET="${CDDC_SANDBOX_NETWORK:-$(_envget CDDC_SANDBOX_NETWORK || true)}"
NETARG=""
[ -n "$NET" ] && NETARG="--network $NET"
# If a decompiler net is set, also mirror the files dir at /files - the SAME path the
# decompiler container reads - so a binary is `/files/<name>` in both, and claude's MCP
# `import_binary /files/<bin>` just works (no path guessing). (Run the decompiler pointed
# at this same dir: `CDDC_FILES_DIR=<that dir> sh sandbox/run-decompiler.sh` - see BOX.md.)
if [ -n "$NET" ] && [ -n "$FILES_ABS" ]; then
  MOUNTS="$MOUNTS -v $FILES_ABS:/files:ro"
fi

# --- backend env: claude on DeepSeek (optional) -----------------------------
# Secret rides in the spawn env (inherited -e, off the argv); non-secret on argv.
ENVARGS=""
if [ "$AGENT" = "claude" ] && [ "${CDDC_DEEPSEEK:-0}" = "1" ]; then
  DS_KEY="$(_envget DEEPSEEK_API_KEY || true)"
  [ -z "$DS_KEY" ] && { echo "CDDC_DEEPSEEK=1 but DEEPSEEK_API_KEY missing in $ENV_FILE"; exit 1; }
  export ANTHROPIC_AUTH_TOKEN="$DS_KEY"
  ENVARGS="-e ANTHROPIC_AUTH_TOKEN \
    -e ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
    -e ANTHROPIC_MODEL=deepseek-v4-pro \
    -e ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4-pro \
    -e ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-pro \
    -e ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash \
    -e CLAUDE_CODE_SUBAGENT_MODEL=deepseek-v4-flash \
    -e CLAUDE_CODE_EFFORT_LEVEL=max"
  echo "(claude backend: DeepSeek)"
fi

# --- launch the box (sleeps; we exec the CLI into it) -----------------------
docker rm -f "$BOX" >/dev/null 2>&1 || true
# shellcheck disable=SC2086
docker run -d --name "$BOX" $NETARG $MOUNTS "$IMAGE" sleep infinity >/dev/null

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "box '$BOX' left running (KEEP=1)."
    echo "  re-enter: docker exec -it -w / -u $USERN -e HOME=$HOME_C $BOX sh -c 'cd /challenge; exec bash'"
    echo "  remove:   docker rm -f $BOX"
  else
    docker rm -f "$BOX" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "box '$BOX' up (image $IMAGE)${FILES_ABS:+, files <- $FILES_ABS}${NET:+, net $NET}"

# Align the ctf user to YOUR uid so it can read the mounted 0600 login + write the
# bind-mounted /challenge. (-w / not the bind-mount cwd: runc's CVE-2024-21626 guard
# rejects `docker exec -w <bind-mount>` on Docker Desktop/WSL.)
docker exec -w / "$BOX" sh -c "
  groupmod -o -g $(id -g) $USERN 2>/dev/null
  usermod  -o -u $(id -u) -g $USERN $USERN 2>/dev/null
  chown $(id -u):$(id -g) $HOME_C 2>/dev/null
  chown -R $(id -u):$(id -g) $HOME_C/.local $HOME_C/.cache $HOME_C/.npm $HOME_C/.claude $HOME_C/.codex 2>/dev/null
  true" || true

# Wire the shared decompiler MCP for claude (if a docker network is configured).
if [ "$AGENT" = "claude" ] && [ -n "$NET" ]; then
  URL="$(_envget CDDC_DECOMPILER_URL || true)"; URL="${URL:-http://cddc-decompiler:8000/mcp}"
  PORT="$(printf '%s' "$URL" | sed -nE 's#.*://[^:/]+:([0-9]+).*#\1#p')"; PORT="${PORT:-8000}"
  docker exec -w / -u "$USERN" "$BOX" sh -c \
    "printf '%s' '{\"mcpServers\":{\"decompiler\":{\"type\":\"http\",\"url\":\"$URL\",\"headers\":{\"Host\":\"localhost:$PORT\"}}}}' > /challenge/.mcp.json" 2>/dev/null || true
fi

# --- the interactive command per agent --------------------------------------
case "$AGENT" in
  claude) CMD="claude --dangerously-skip-permissions" ;;
  codex)  CMD="codex" ;;
  *)      CMD="bash" ;;
esac
CMD="${AGENT_CMD:-$CMD}"

echo "--- entering $AGENT  (exit the CLI to stop the box) ---"
# -w / + `cd` inside the shell (runc bind-mount-cwd workaround again).
# shellcheck disable=SC2086
docker exec -it -w / -u "$USERN" -e HOME="$HOME_C" $ENVARGS "$BOX" \
  sh -c "cd /challenge 2>/dev/null; exec $CMD"
