#!/usr/bin/env sh
# share.sh - upload a BIG challenge file (too large for a Discord attachment) to an
# anonymous host and print the direct URL + the ready-to-paste `!fetch` command.
#
#   sh sandbox/share.sh <file> [litterbox|temp|x0]
#
# Hosts (anonymous, direct curl-able URL, as of 2026):
#   litterbox  - <=1GB, kept 72h   (default)
#   temp       - temp.sh, <=4GB, 3d (use for 1-4GB files)
#   x0         - x0.at, <=1GB, 3-100d retention
# (0x0.st and transfer.sh are dead in 2026.)
set -eu

F="${1:?usage: sh sandbox/share.sh <file> [litterbox|temp|x0]}"
HOST="${2:-litterbox}"
[ -f "$F" ] || { echo "no such file: $F" >&2; exit 1; }
echo "uploading $F ($(du -h "$F" | cut -f1)) to $HOST ..." >&2

case "$HOST" in
  litterbox)
    URL=$(curl -fsS -F "reqtype=fileupload" -F "time=72h" -F "fileToUpload=@$F" \
          https://litterbox.catbox.moe/resources/internals/api.php) ;;
  temp)
    URL=$(curl -fsS -F "file=@$F" https://temp.sh/upload) ;;
  x0)
    URL=$(curl -fsS -F "file=@$F" https://x0.at/) ;;
  *)
    echo "unknown host '$HOST' (use litterbox | temp | x0)" >&2; exit 1 ;;
esac

URL=$(printf '%s' "$URL" | tr -d '\r\n ')
[ -n "$URL" ] || { echo "upload failed (empty response)" >&2; exit 1; }
echo "$URL"
echo "-> paste in the challenge thread:  !fetch $URL"
