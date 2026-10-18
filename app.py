import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import pty_backend

APP_DIR = Path(__file__).parent
# Optional: point this at a folder of git repos to seed the (empty) project
# list from on first run, and to use as the working directory for terminals
# opened with no project selected. Unset by default - nothing is assumed
# about where any given user keeps their projects.
_seed_env = os.environ.get("SWITCHBOARD_PROJECTS_DIR", "").strip()
SEED_DIR = Path(_seed_env).expanduser().resolve() if _seed_env else None
GENERAL_CWD = SEED_DIR if SEED_DIR else Path.home()
DEFAULT_AGENT_COMMAND = "claude"
# How each known agent CLI takes an opening prompt while still starting an
# interactive session. "{prompt}" marks where the text goes; an empty
# template means the agent gets no opening prompt at all, which is also what
# unknown commands get - guessing wrong could break or misdirect the agent.
PROMPT_TEMPLATE_DEFAULTS = {
    "claude": "{prompt}",
    "opencode": "--prompt {prompt}",
    # A positional prompt makes cline run a one-shot, auto-approved task
    # instead of opening its TUI, so it gets no opening prompt.
    "cline": "",
}
MAX_FILE_BYTES = 1_000_000
HIDDEN_TOP_LEVEL = {".git"}

TOKEN_FILE = Path.home() / ".switchboard-token"
DB_PATH = APP_DIR / "switchboard.db"


def load_or_create_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_hex(24)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


SWITCHBOARD_TOKEN = load_or_create_token()


def split_command(command: str) -> list[str]:
    return shlex.split(command, posix=not pty_backend.IS_WINDOWS)


def default_prompt_template(command: str) -> str:
    try:
        argv = split_command(command)
    except ValueError:
        return ""
    if not argv:
        return ""
    # Path(...).stem so "/usr/bin/opencode" and "claude.exe" match too.
    return PROMPT_TEMPLATE_DEFAULTS.get(Path(argv[0]).stem.lower(), "")


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
    # Derive the prompt template from whatever command is already configured,
    # so upgrading with e.g. "opencode" set doesn't inherit claude's template.
    command = conn.execute("SELECT value FROM settings WHERE key = 'agent_command'").fetchone()["value"]
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('agent_prompt_template', ?)",
        (default_prompt_template(command),),
    )
    conn.commit()
    # One-time seed: if the table is empty and SWITCHBOARD_PROJECTS_DIR is set,
    # pick up every git repo already sitting there so switchboard isn't
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

app = FastAPI(title="Switchboard")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def require_token(x_switchboard_token: str = Header(default="")):
    if not secrets.compare_digest(x_switchboard_token, SWITCHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


class AddProjectRequest(BaseModel):
    path: str
    name: str | None = None


class SettingsRequest(BaseModel):
    agent_command: str
    # None means "use the known default for this command".
    prompt_template: str | None = None


def all_projects_context() -> str:
    conn = db_connect()
    rows = conn.execute("SELECT name, path FROM projects ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    if not rows:
        return ""
    lines = [f"- {row['name']}: {row['path']}" for row in rows]
    return (
        "Switchboard tracks the following projects:\n"
        + "\n".join(lines)
        + "\n\nWhen I ask you to do something across \"all projects\" or "
        "\"switchboard's projects\", apply it to each of the above, one "
        "at a time."
    )


def get_setting(key: str, default: str) -> str:
    conn = db_connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def get_agent_command() -> str:
    return get_setting("agent_command", DEFAULT_AGENT_COMMAND)


def get_prompt_template() -> str:
    return get_setting("agent_prompt_template", default_prompt_template(get_agent_command()))


def prompt_args(template: str, prompt: str) -> list[str]:
    return [token.replace("{prompt}", prompt) for token in split_command(template)]


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
    return {
        "agent_command": get_agent_command(),
        "prompt_template": get_prompt_template(),
        "prompt_defaults": PROMPT_TEMPLATE_DEFAULTS,
    }


@app.put("/api/settings", dependencies=[Depends(require_token)])
def api_set_settings(req: SettingsRequest):
    command = req.agent_command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="Agent command cannot be empty")
    template = req.prompt_template
    template = default_prompt_template(command) if template is None else template.strip()
    try:
        split_command(command)
        split_command(template)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Could not parse: {e}")
    conn = db_connect()
    for key, value in (("agent_command", command), ("agent_prompt_template", template)):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
    conn.close()
    return {"agent_command": command, "prompt_template": template}


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


@app.get("/api/browse", dependencies=[Depends(require_token)])
def api_browse(path: str = ""):
    target = Path(path).expanduser().resolve() if path else Path.home()
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        children = []
    for entry in children:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except PermissionError:
            continue
        entries.append({
            "name": entry.name,
            "path": str(entry),
            "is_git": (entry / ".git").exists(),
        })
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "entries": entries}


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
        raise HTTPException(status_code=409, detail="This path is already in switchboard")
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


class TermSession:
    """A PTY + its live subscribers, kept alive independently of any single
    WebSocket connection so a page refresh (new socket) can reattach to the
    same running agent instead of killing and restarting it."""

    def __init__(self, session_id: str, pty_session: "pty_backend.PtySession", loop: asyncio.AbstractEventLoop):
        self.id = session_id
        self.pty_session = pty_session
        self.loop = loop
        self.buffer = bytearray()
        self.queues: set[asyncio.Queue] = set()
        self.lock = threading.Lock()


SESSIONS: dict[str, TermSession] = {}
sessions_lock = threading.Lock()
MAX_SESSION_BUFFER = 1_000_000


def session_reader(sess: TermSession) -> None:
    # One dedicated thread per session for its whole lifetime - POSIX pty fds
    # and Windows ConPTY handles don't share a common async-readable
    # primitive, so we don't try to hook either into the event loop directly.
    while True:
        data = sess.pty_session.read(65536)
        if not data:
            break
        with sess.lock:
            sess.buffer.extend(data)
            if len(sess.buffer) > MAX_SESSION_BUFFER:
                del sess.buffer[: len(sess.buffer) - MAX_SESSION_BUFFER]
            queues = list(sess.queues)
        for q in queues:
            sess.loop.call_soon_threadsafe(q.put_nowait, data)
    # The PTY exited on its own (agent/shell quit) - clean up and let any
    # attached viewers know the stream ended.
    with sessions_lock:
        SESSIONS.pop(sess.id, None)
    with sess.lock:
        queues = list(sess.queues)
    for q in queues:
        sess.loop.call_soon_threadsafe(q.put_nowait, None)


@app.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket, project: str = "", token: str = "", kind: str = "agent", session: str = ""):
    if not secrets.compare_digest(token, SWITCHBOARD_TOKEN):
        await websocket.close(code=4401)
        return

    await websocket.accept()

    sess = SESSIONS.get(session) if session else None

    if sess is None:
        if project:
            try:
                cwd = project_dir(int(project))
            except (ValueError, HTTPException):
                await websocket.close(code=4404)
                return
        else:
            cwd = GENERAL_CWD

        if kind == "shell":
            argv = [pty_backend.default_shell()]
        else:
            argv = split_command(get_agent_command())
            if not project:
                # General scope: the agent has no built-in notion of
                # "switchboard's projects" (they can live anywhere on disk), so
                # tell it up front as its opening message - in whatever form
                # this agent CLI accepts one (see PROMPT_TEMPLATE_DEFAULTS).
                context = all_projects_context()
                if context:
                    argv = argv + prompt_args(get_prompt_template(), context)
        if not argv:
            await websocket.close(code=4400)
            return
        if shutil.which(argv[0]) is None:
            # Otherwise exec fails inside the forked child and the pane just
            # sits there blank with no hint why.
            await websocket.send_bytes(
                f"\x1b[31m'{argv[0]}' was not found on PATH.\x1b[0m\r\n"
                "Set the agent command at the top of the page, then open a new terminal.\r\n".encode()
            )
            await websocket.close(code=4400)
            return

        pty_session = pty_backend.PtySession(argv, str(cwd), {"TERM": "xterm-256color"})
        session_id = secrets.token_hex(16)
        sess = TermSession(session_id, pty_session, asyncio.get_event_loop())
        with sessions_lock:
            SESSIONS[session_id] = sess
        threading.Thread(target=session_reader, args=(sess,), daemon=True).start()
    else:
        session_id = session

    await websocket.send_text(json.dumps({"type": "session", "id": session_id}))

    # Snapshot the backlog and subscribe atomically (same lock the reader
    # thread uses) so no bytes are lost or duplicated at the join point.
    queue: asyncio.Queue = asyncio.Queue()
    with sess.lock:
        backlog = bytes(sess.buffer)
        sess.queues.add(queue)
    if backlog:
        await websocket.send_bytes(backlog)

    async def pump_output():
        while True:
            data = await queue.get()
            if data is None:
                # The process exited - say so and close the socket, rather
                # than leaving a dead pane that still looks connected.
                try:
                    await websocket.send_bytes(b"\r\n\x1b[2m[process exited]\x1b[0m\r\n")
                    await websocket.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass
                break
            await websocket.send_bytes(data)

    pump_task = asyncio.ensure_future(pump_output())

    explicit_close = False
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                sess.pty_session.write(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    control = json.loads(msg["text"])
                except ValueError:
                    continue
                if control.get("type") == "resize":
                    sess.pty_session.resize(int(control["rows"]), int(control["cols"]))
                elif control.get("type") == "close":
                    # The user closed this pane - end the agent/shell for
                    # real, rather than just dropping this one viewer.
                    explicit_close = True
                    break
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: receive() after pump_output already closed the socket.
        pass
    finally:
        with sess.lock:
            sess.queues.discard(queue)
        pump_task.cancel()
        if explicit_close:
            with sessions_lock:
                SESSIONS.pop(session_id, None)
            sess.pty_session.terminate()


@app.get("/", response_class=HTMLResponse)
def index():
    html = (APP_DIR / "static" / "index.html").read_text()
    return html.replace("__SWITCHBOARD_TOKEN__", SWITCHBOARD_TOKEN)
