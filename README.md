# Agent Dashboard

A local web dashboard for running AI coding agents (or plain shells) against
your git repos from the browser - multiple terminals per project, a file
explorer, and a git history view. Runs the same way on a native Linux
desktop or inside WSL2 (reachable from Windows via its automatic localhost
forwarding, no extra setup) - `run.sh` detects which one it's on and prints
the right URL to open.

## Requirements

- Python 3.10+
- An agent CLI on your `PATH` that supports an interactive terminal session
  (e.g. [Claude Code](https://claude.com/claude-code), `opencode`, `cline`, ...)
- `git`

## Setup

```bash
pip install -r requirements.txt
```

## Running

```bash
./run.sh
```

Open `http://localhost:8787` in your browser. On first run a random access
token is generated at `~/.agent-dashboard-token` and a `dashboard.db`
SQLite file is created next to `app.py` (both are gitignored - they're
per-install, not part of the repo).

### Optional environment variables

- `DASHBOARD_PROJECTS_DIR` - a folder of git repos. If set and the project
  list is empty, it's used to seed the list on first run, and it's the
  working directory for terminals opened with no project selected. Unset by
  default - nothing is assumed about where you keep your projects; add them
  one by one from the dashboard's "Add project" box instead.
- `DASHBOARD_PORT` - port to listen on (default `8787`).

## Notes

- The server binds to `127.0.0.1` only - reachable from this machine (and,
  on WSL2, from Windows), never from other machines on the network.
- Every request to `/api/*` and `/ws/terminal` requires the access token
  (sent automatically by the page itself); this is mainly to stop other
  tabs/sites in your browser from silently hitting the API.
- The agent command used for new "Agent" terminals is configurable from the
  dashboard UI (defaults to `claude`).
- Terminals keep running in the background as you switch between projects
  or go back to the main page; closing the browser tab or the server
  process ends them.
