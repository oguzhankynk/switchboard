#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="${DASHBOARD_PORT:-8787}"

# Bind to 127.0.0.1 only: reachable from this machine (and, on WSL2, from
# Windows via its automatic localhost forwarding), never from the network.
if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  echo "Detected WSL2. Open http://localhost:$PORT from your Windows browser."
else
  echo "Open http://localhost:$PORT in your browser."
fi

exec python3 -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
