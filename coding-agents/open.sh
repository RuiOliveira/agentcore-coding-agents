#!/usr/bin/env bash
# Open what an agent wrote, in the browser where VS Code is signed in.
#
#   ./open.sh /mnt/s3files/lid        # serve that folder on port 8000 and print its link
#   ./open.sh /mnt/s3files/lid 8001   # another port
#
# The folder is on the shared S3 Files mount, so this host reads the same files the
# agent's microVM wrote. The server is a plain static file server on this host; it
# replaces whatever this script started on the same port before.
set -euo pipefail

folder="${1:-}"
port="${2:-8000}"
if [ -z "$folder" ]; then
  echo "Usage: $0 <folder> [port]" >&2
  exit 2
fi
if [ ! -d "$folder" ]; then
  echo "No folder $folder yet: the agent has not written it." >&2
  exit 1
fi
here="$(cd "$(dirname "$0")" && pwd)"
log="${TMPDIR:-/tmp}/open-$port.log"

pkill -f "http.server $port --bind 127.0.0.1" 2>/dev/null || true
nohup python3 -m http.server "$port" --bind 127.0.0.1 --directory "$folder" >"$log" 2>&1 &
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 - "$port" <<'PY' 2>/dev/null; then break; fi
import socket, sys
socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()
PY
  sleep 0.5
done
echo "Serving $folder"
python3 "$here/../orchestrator/workshop_urls.py" "$port"
