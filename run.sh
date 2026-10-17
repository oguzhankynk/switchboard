#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="${DASHBOARD_PORT:-8787}"
# Bind to 127.0.0.1 only - on WSL2 this is still reachable from Windows at
# localhost:$PORT (via its automatic localhost forwarding), but not from
# other machines on the network.
exec python3 -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
