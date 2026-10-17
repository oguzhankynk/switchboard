#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Bind to 127.0.0.1 only - WSL2's localhost forwarding makes it reachable
# from Windows at localhost:8787, but not from other machines on the network.
exec python3 -m uvicorn app:app --host 127.0.0.1 --port 8787
