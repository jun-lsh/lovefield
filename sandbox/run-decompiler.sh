#!/usr/bin/env sh
# Bring up the shared pyghidra-mcp decompiler service for the fleet.
#
# Run from the repo root, in the SAME environment as the bot, so the /files mount
# matches the dir the bot writes challenge binaries to (CDDC_FILES_DIR). Agents reach
# this service over MCP across the docker network (the Claude harness's `decompiler`
# MCP); it reads binaries from /files/<thread> (= each challenge's workdir).
#
# Prereq - build the decompiler image:
#   docker build -f sandbox/Dockerfile.sandbox --target decompiler -t ctf-sandbox:decompiler sandbox
set -eu

# Read CDDC_FILES_DIR / CDDC_SANDBOX_NETWORK from the bot's own cddc/.env (unless set
# in the shell) so /files mounts the SAME dir the bot writes challenge binaries to.
# A mismatch here = import "cannot be found" (the service can't see the binary).
ENV_FILE="${CDDC_ENV_FILE:-cddc/.env}"
_envget() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//; s/[[:space:]]*$//'; }
NET="${CDDC_SANDBOX_NETWORK:-$(_envget CDDC_SANDBOX_NETWORK)}"; NET="${NET:-cddc-net}"
FILES_DIR="${CDDC_FILES_DIR:-$(_envget CDDC_FILES_DIR)}"; FILES_DIR="${FILES_DIR:-_files}"
case "$FILES_DIR" in "~"*) FILES_DIR="$HOME${FILES_DIR#\~}" ;; esac  # expand ~ like the bot's expanduser()
IMAGE="${CDDC_DECOMPILER_IMAGE:-ctf-sandbox:decompiler}"

mkdir -p "$FILES_DIR"
FILES_ABS="$(cd "$FILES_DIR" && pwd)"
echo "mounting files: $FILES_ABS -> /files  (must match the bot's CDDC_FILES_DIR)"

docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET"
docker rm -f cddc-decompiler >/dev/null 2>&1 || true
docker run -d --name cddc-decompiler --network "$NET" \
  -v "$FILES_ABS":/files:ro \
  -v cddc-ghidra-proj:/projects \
  -v cddc-dc-cache:/root/.cache \
  "$IMAGE"

echo "cddc-decompiler up on '$NET' (MCP :8000, files <- $FILES_ABS)"
echo "Set in cddc/.env:  CDDC_SANDBOX_NETWORK=$NET"
echo "Logs:  docker logs -f cddc-decompiler   |   it serves MCP on :8000 (/mcp)"
