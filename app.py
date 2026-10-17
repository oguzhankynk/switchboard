import asyncio
import fcntl
import json
import os
import pty
import re
import secrets
import shlex
import signal
import sqlite3
import struct
import subprocess
import termios
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).parent
# Optional: point this at a folder of git repos to seed the (empty) project
# list from on first run, and to use as the working directory for terminals
# opened with no project selected. Unset by default - nothing is assumed
# about where any given user keeps their projects.
_seed_env = os.environ.get("DASHBOARD_PROJECTS_DIR", "").strip()
SEED_DIR = Path(_seed_env).expanduser().resolve() if _seed_env else None
GENERAL_CWD = SEED_DIR if SEED_DIR else Path.home()
DEFAULT_AGENT_COMMAND = "claude"
MAX_FILE_BYTES = 1_000_000
HIDDEN_TOP_LEVEL = {".git"}

TOKEN_FILE = Path.home() / ".agent-dashboard-token"
DB_PATH = APP_DIR / "dashboard.db"


def load_or_create_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_hex(24)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


DASHBOARD_TOKEN = load_or_create_token()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            added_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('agent_command', ?)",
        (DEFAULT_AGENT_COMMAND,),
    )
    conn.commit()
    # One-time seed: if the table is empty and DASHBOARD_PROJECTS_DIR is set,
    # pick up every git repo already sitting there so the dashboard isn't
    # empty on first run. No-op if the env var isn't set.
    count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    if count == 0 and SEED_DIR and SEED_DIR.exists():
        now = datetime.now(timezone.utc).isoformat()
        for entry in sorted(SEED_DIR.iterdir()):
            if entry.is_dir() and (entry / ".git").exists():
                conn.execute(
                    "INSERT OR IGNORE INTO projects (name, path, added_at) VALUES (?, ?, ?)",
                    (entry.name, str(entry.resolve()), now),
                )
        conn.commit()
    conn.close()


init_db()

app = FastAPI(title="Agent Dashboard")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def require_token(x_dashboard_token: str = Header(default="")):
    if not secrets.compare_digest(x_dashboard_token, DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


class AddProjectRequest(BaseModel):
    path: str
    name: str | None = None


class SettingsRequest(BaseModel):
    agent_command: str


def get_agent_command() -> str:
    conn = db_connect()
    row = conn.execute("SELECT value FROM settings WHERE key = 'agent_command'").fetchone()
    conn.close()
    return row["value"] if row else DEFAULT_AGENT_COMMAND


def get_project_row(project_id: int) -> sqlite3.Row:
    conn = db_connect()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def project_dir(project_id: int) -> Path:
    row = get_project_row(project_id)
    path = Path(row["path"])
    if not path.is_dir() or not (path / ".git").exists():
        raise HTTPException(status_code=404, detail="Project directory is missing or no longer a git repo")
    return path


def git_info(path: Path) -> dict:
    def run(args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    return {
        "branch": run(["branch", "--show-current"]),
        "last_commit": run(["log", "-1", "--format=%h %s (%cr)"]),
        "dirty": bool(run(["status", "--short"])),
    }


def resolve_within(project_path: Path, rel_path: str) -> Path:
    candidate = (project_path / rel_path).resolve()
    if candidate != project_path and project_path not in candidate.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    if HIDDEN_TOP_LEVEL & {candidate.name} and candidate.parent == project_path:
        raise HTTPException(status_code=403, detail="Access denied")
    return candidate


ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
SGR_COLOR = {
    "30": "ansi-black", "31": "ansi-red", "32": "ansi-green", "33": "ansi-yellow",
    "34": "ansi-blue", "35": "ansi-magenta", "36": "ansi-cyan", "37": "ansi-white",
    "90": "ansi-bright-black", "91": "ansi-bright-red", "92": "ansi-bright-green",
    "93": "ansi-bright-yellow", "94": "ansi-bright-blue", "95": "ansi-bright-magenta",
    "96": "ansi-bright-cyan", "97": "ansi-bright-white",
}


def ansi_to_segments(text: str) -> list[dict]:
    segments = []
    current_class = None
    pos = 0
    for m in ANSI_RE.finditer(text):
        if m.start() > pos:
            chunk = text[pos:m.start()]
            if chunk:
                segments.append({"text": chunk, "cls": current_class})
        codes = m.group(1).split(";") if m.group(1) else ["0"]
        for code in codes:
            if code in ("0", ""):
                current_class = None
            elif code in SGR_COLOR:
                current_class = SGR_COLOR[code]
        pos = m.end()
    if pos < len(text):
        segments.append({"text": text[pos:], "cls": current_class})
    return segments


def readme_text(path: Path) -> str:
    for name in ("README.md", "README.MD", "readme.md", "README"):
        candidate = path / name
        if candidate.exists():
            try:
                return candidate.read_text(errors="replace")[:4000]
            except Exception:
                return ""
    return ""


@app.get("/api/settings", dependencies=[Depends(require_token)])
def api_get_settings():
    return {"agent_command": get_agent_command()}


@app.put("/api/settings", dependencies=[Depends(require_token)])
def api_set_settings(req: SettingsRequest):
    command = req.agent_command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="Agent command cannot be empty")
    conn = db_connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('agent_command', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (command,),
    )
    conn.commit()
    conn.close()
    return {"agent_command": command}


@app.get("/api/projects", dependencies=[Depends(require_token)])
def api_list_projects():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    result = []
    for row in rows:
        path = Path(row["path"])
        info = git_info(path) if path.is_dir() else {"branch": "", "last_commit": "", "dirty": False}
        result.append({
            "id": row["id"], "name": row["name"], "path": row["path"],
            "missing": not path.is_dir(), **info,
        })
    return result


@app.post("/api/projects", dependencies=[Depends(require_token)])
def api_add_project(req: AddProjectRequest):
    path = Path(req.path).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Path does not exist or is not a directory")
    if not (path / ".git").exists():
        raise HTTPException(status_code=400, detail="Path is not a git repository")
    name = req.name or path.name
    conn = db_connect()
    try:
        cur = conn.execute(
            "INSERT INTO projects (name, path, added_at) VALUES (?, ?, ?)",
            (name, str(path), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="This path is already in the dashboard")
    conn.close()
    return {"id": new_id, "name": name, "path": str(path)}


@app.delete("/api/projects/{project_id}", dependencies=[Depends(require_token)])
def api_remove_project(project_id: int):
    conn = db_connect()
    cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True}


@app.get("/api/projects/{project_id}", dependencies=[Depends(require_token)])
def api_project_detail(project_id: int):
    row = get_project_row(project_id)
    path = project_dir(project_id)
    return {"id": project_id, "name": row["name"], "path": str(path), **git_info(path), "readme": readme_text(path)}


@app.get("/api/projects/{project_id}/tree", dependencies=[Depends(require_token)])
def api_project_tree(project_id: int, path: str = ""):
    project_path = project_dir(project_id)
    target = resolve_within(project_path, path)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    entries = []
    for entry in target.iterdir():
        if entry.parent == project_path and entry.name in HIDDEN_TOP_LEVEL:
            continue
        entries.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "path": str(entry.relative_to(project_path)),
        })
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


@app.get("/api/projects/{project_id}/file", dependencies=[Depends(require_token)])
def api_project_file(project_id: int, path: str):
    project_path = project_dir(project_id)
    target = resolve_within(project_path, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    raw = target.read_bytes()
    if b"\0" in raw[:8000]:
        return {"binary": True, "content": "", "truncated": False}
    truncated = len(raw) > MAX_FILE_BYTES
    text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return {"binary": False, "content": text, "truncated": truncated}


@app.get("/api/projects/{project_id}/git-graph", dependencies=[Depends(require_token)])
def api_project_git_graph(project_id: int):
    project_path = project_dir(project_id)
    result = subprocess.run(
        ["git", "-C", str(project_path), "log", "--graph", "--oneline",
         "--decorate", "--all", "--color=always", "-n", "300"],
        capture_output=True, text=True, timeout=10,
    )
    output = result.stdout or result.stderr
    return {"segments": ansi_to_segments(output)}


@app.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket, project: str = "", token: str = "", kind: str = "agent"):
    if not secrets.compare_digest(token, DASHBOARD_TOKEN):
        await websocket.close(code=4401)
        return

    if project:
        try:
            cwd = project_dir(int(project))
        except (ValueError, HTTPException):
            await websocket.close(code=4404)
            return
    else:
        cwd = GENERAL_CWD

    await websocket.accept()

    if kind == "shell":
        argv = [os.environ.get("SHELL", "/bin/bash")]
    else:
        argv = shlex.split(get_agent_command())
    if not argv:
        await websocket.close(code=4400)
        return

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(str(cwd))
            os.environ["TERM"] = "xterm-256color"
            os.execvp(argv[0], argv)
        finally:
            os._exit(1)

    loop = asyncio.get_event_loop()

    def on_pty_output():
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if data:
            loop.create_task(websocket.send_bytes(data))
        else:
            loop.remove_reader(fd)

    loop.add_reader(fd, on_pty_output)

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                os.write(fd, msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    control = json.loads(msg["text"])
                except ValueError:
                    continue
                if control.get("type") == "resize":
                    rows, cols = int(control["rows"]), int(control["cols"])
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except WebSocketDisconnect:
        pass
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


@app.get("/", response_class=HTMLResponse)
def index():
    html = (APP_DIR / "static" / "index.html").read_text()
    return html.replace("__DASHBOARD_TOKEN__", DASHBOARD_TOKEN)
