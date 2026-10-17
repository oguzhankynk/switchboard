const projectList = document.getElementById("project-list");
const generalTerminalSection = document.getElementById("general-terminal-section");
const projectsSection = document.getElementById("projects-section");
const detailSection = document.getElementById("detail-section");
const detailTitle = document.getElementById("detail-title");
const detailMeta = document.getElementById("detail-meta");
const detailReadme = document.getElementById("detail-readme");
const backBtn = document.getElementById("back-btn");
const generalPanesEl = document.getElementById("general-terminal-panes");
const generalNewAgentBtn = document.getElementById("general-new-agent-btn");
const generalNewShellBtn = document.getElementById("general-new-shell-btn");
const projectPanesEl = document.getElementById("project-terminal-panes");
const projectNewAgentBtn = document.getElementById("project-new-agent-btn");
const projectNewShellBtn = document.getElementById("project-new-shell-btn");
const fileTreeEl = document.getElementById("file-tree");
const fileContentEl = document.getElementById("file-content");
const gitGraphEl = document.getElementById("git-graph");
const addPathInput = document.getElementById("add-path-input");
const addNameInput = document.getElementById("add-name-input");
const addProjectBtn = document.getElementById("add-project-btn");
const addProjectError = document.getElementById("add-project-error");
const agentCmdInput = document.getElementById("agent-cmd-input");
const agentCmdSave = document.getElementById("agent-cmd-save");

let currentProjectId = null;

const authHeaders = { "X-Dashboard-Token": window.DASHBOARD_TOKEN || "" };

async function loadProjects() {
  const res = await fetch("/api/projects", { headers: authHeaders });
  const projects = await res.json();
  projectList.innerHTML = "";
  for (const p of projects) {
    const card = document.createElement("div");
    card.className = "project-card";

    const removeBtn = document.createElement("button");
    removeBtn.className = "remove-btn";
    removeBtn.textContent = "×";
    removeBtn.title = "Remove from dashboard";
    removeBtn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm(`Remove "${p.name}" from the dashboard? (files on disk are untouched)`)) return;
      await fetch(`/api/projects/${p.id}`, { method: "DELETE", headers: authHeaders });
      closeProjectPanes(p.id);
      loadProjects();
    });
    card.appendChild(removeBtn);

    const title = document.createElement("h3");
    title.textContent = p.name;
    if (p.dirty) {
      const badge = document.createElement("span");
      badge.className = "dirty-badge";
      badge.textContent = "uncommitted changes";
      title.appendChild(badge);
    }
    if (p.missing) {
      const badge = document.createElement("span");
      badge.className = "missing-badge";
      badge.textContent = "path missing";
      title.appendChild(badge);
    }

    const branch = document.createElement("div");
    branch.className = "branch";
    branch.textContent = p.branch || "";

    const commit = document.createElement("div");
    commit.className = "commit";
    commit.textContent = p.last_commit || "";

    card.append(title, branch, commit);
    card.addEventListener("click", () => openProject(p.id, p.name));
    projectList.appendChild(card);
  }
}

addProjectBtn.addEventListener("click", async () => {
  const path = addPathInput.value.trim();
  const name = addNameInput.value.trim();
  addProjectError.hidden = true;
  if (!path) return;
  const res = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({ path, name: name || null }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    addProjectError.textContent = data.detail || "Could not add project";
    addProjectError.hidden = false;
    return;
  }
  addPathInput.value = "";
  addNameInput.value = "";
  loadProjects();
});

async function openProject(id, name) {
  const res = await fetch(`/api/projects/${id}`, { headers: authHeaders });
  if (!res.ok) return;
  const data = await res.json();
  currentProjectId = id;
  detailTitle.textContent = name;
  detailMeta.textContent = `${data.branch || ""} · ${data.last_commit || ""}${data.dirty ? " · uncommitted changes" : ""}`;
  detailReadme.textContent = data.readme || "(no README)";
  generalTerminalSection.hidden = true;
  projectsSection.hidden = true;
  detailSection.hidden = false;
  showProjectPanes(id);
  loadFileTree();
  loadGitGraph();
}

// --- File explorer ---

async function fetchTree(relPath) {
  const res = await fetch(
    `/api/projects/${currentProjectId}/tree?path=${encodeURIComponent(relPath)}`,
    { headers: authHeaders },
  );
  if (!res.ok) return [];
  return res.json();
}

function buildTreeList(entries, container) {
  const ul = document.createElement("ul");
  for (const entry of entries) {
    const li = document.createElement("li");
    const node = document.createElement("div");
    node.className = "tree-node";

    const twisty = document.createElement("span");
    twisty.className = "tree-twisty";
    twisty.textContent = entry.type === "dir" ? "▸" : "";
    node.appendChild(twisty);

    const label = document.createElement("span");
    label.textContent = entry.name;
    node.appendChild(label);

    li.appendChild(node);

    if (entry.type === "dir") {
      let expanded = false;
      let childrenEl = null;
      node.addEventListener("click", async () => {
        expanded = !expanded;
        twisty.textContent = expanded ? "▾" : "▸";
        if (expanded) {
          if (!childrenEl) {
            const children = await fetchTree(entry.path);
            childrenEl = document.createElement("div");
            buildTreeList(children, childrenEl);
            li.appendChild(childrenEl);
          }
          childrenEl.hidden = false;
        } else if (childrenEl) {
          childrenEl.hidden = true;
        }
      });
    } else {
      node.addEventListener("click", () => openFile(entry.path, node));
    }

    ul.appendChild(li);
  }
  container.appendChild(ul);
}

async function loadFileTree() {
  fileTreeEl.innerHTML = "";
  fileContentEl.textContent = "(select a file)";
  const entries = await fetchTree("");
  buildTreeList(entries, fileTreeEl);
}

let selectedNode = null;

async function openFile(relPath, node) {
  if (selectedNode) selectedNode.classList.remove("selected");
  node.classList.add("selected");
  selectedNode = node;
  fileContentEl.textContent = "loading...";
  const res = await fetch(
    `/api/projects/${currentProjectId}/file?path=${encodeURIComponent(relPath)}`,
    { headers: authHeaders },
  );
  if (!res.ok) {
    fileContentEl.textContent = "(could not read file)";
    return;
  }
  const data = await res.json();
  if (data.binary) {
    fileContentEl.textContent = "(binary file, cannot display)";
    return;
  }
  fileContentEl.textContent = data.content + (data.truncated ? "\n\n... (truncated)" : "");
}

// --- Git graph ---

async function loadGitGraph() {
  gitGraphEl.textContent = "loading...";
  const res = await fetch(
    `/api/projects/${currentProjectId}/git-graph`,
    { headers: authHeaders },
  );
  if (!res.ok) {
    gitGraphEl.textContent = "(could not load)";
    return;
  }
  const data = await res.json();
  gitGraphEl.innerHTML = "";
  for (const seg of data.segments) {
    if (seg.cls) {
      const span = document.createElement("span");
      span.className = seg.cls;
      span.textContent = seg.text;
      gitGraphEl.appendChild(span);
    } else {
      gitGraphEl.appendChild(document.createTextNode(seg.text));
    }
  }
}

backBtn.addEventListener("click", () => {
  currentProjectId = null;
  detailSection.hidden = true;
  projectsSection.hidden = false;
  generalTerminalSection.hidden = false;
  generalPanes.refitAll();
});

// --- Terminal panes (xterm.js <-> WebSocket <-> PTY running a real agent session) ---
// Each pane owns its own WebSocket + PTY on the backend, so several agents can
// run side by side. Panes can be added, closed, collapsed, and reordered by
// dragging their header.

function makePaneManager(containerEl, getScopeId) {
  const panes = [];
  let counter = 0;

  function addPane(kind = "agent") {
    counter += 1;
    const id = counter;
    const label = kind === "shell" ? "Shell" : "Agent";

    const el = document.createElement("div");
    el.className = "term-pane";

    const header = document.createElement("div");
    header.className = "term-pane-header";
    header.draggable = true;

    const handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = "⠿";
    header.appendChild(handle);

    const title = document.createElement("span");
    title.className = "term-pane-title";
    title.textContent = `${label} ${id}`;
    header.appendChild(title);

    const status = document.createElement("span");
    status.className = "term-pane-status";
    status.textContent = "connecting...";
    header.appendChild(status);

    const collapseBtn = document.createElement("button");
    collapseBtn.textContent = "▾";
    collapseBtn.title = "Collapse";
    header.appendChild(collapseBtn);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "×";
    closeBtn.title = "Close terminal";
    header.appendChild(closeBtn);

    el.appendChild(header);

    const body = document.createElement("div");
    body.className = "term-pane-body";
    el.appendChild(body);

    const term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      theme: { background: "#000000" },
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(body);

    const pane = { id, el, body, status, term, fitAddon, ws: null };

    function fitAndResize() {
      if (!el.classList.contains("collapsed")) {
        fitAddon.fit();
        if (pane.ws && pane.ws.readyState === WebSocket.OPEN) {
          pane.ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
        }
      }
    }

    function connect() {
      if (pane.ws) { try { pane.ws.close(); } catch (e) { /* noop */ } }
      term.reset();
      status.textContent = "connecting...";
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const params = new URLSearchParams({
        token: window.DASHBOARD_TOKEN || "",
        project: getScopeId() || "",
        kind,
      });
      const socket = new WebSocket(`${proto}://${location.host}/ws/terminal?${params.toString()}`);
      socket.binaryType = "arraybuffer";
      pane.ws = socket;
      socket.onopen = () => {
        const scope = getScopeId() ? `project #${getScopeId()}` : "general";
        status.textContent = `${scope} · ${label.toLowerCase()}`;
        fitAndResize();
      };
      socket.onmessage = (ev) => {
        if (ev.data instanceof ArrayBuffer) term.write(new Uint8Array(ev.data));
      };
      socket.onclose = () => { status.textContent = "closed"; };
      socket.onerror = () => { status.textContent = "error"; };
    }

    term.onData((data) => {
      if (pane.ws && pane.ws.readyState === WebSocket.OPEN) {
        pane.ws.send(new TextEncoder().encode(data));
      }
    });

    collapseBtn.addEventListener("click", () => {
      el.classList.toggle("collapsed");
      collapseBtn.textContent = el.classList.contains("collapsed") ? "▸" : "▾";
      if (!el.classList.contains("collapsed")) fitAndResize();
    });

    closeBtn.addEventListener("click", () => removePane(pane));

    header.addEventListener("dragstart", () => {
      el.classList.add("dragging");
      header.dataset.dragging = "1";
    });
    header.addEventListener("dragend", () => {
      el.classList.remove("dragging");
    });
    el.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      el.classList.add("drag-over");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    el.addEventListener("drop", (ev) => {
      ev.preventDefault();
      el.classList.remove("drag-over");
      const dragged = containerEl.querySelector(".term-pane.dragging");
      if (dragged && dragged !== el) containerEl.insertBefore(dragged, el);
    });

    panes.push(pane);
    containerEl.appendChild(el);
    connect();
    pane._fitAndResize = fitAndResize;
    return pane;
  }

  function removePane(pane) {
    if (pane.ws) { try { pane.ws.close(); } catch (e) { /* noop */ } }
    pane.term.dispose();
    pane.el.remove();
    const idx = panes.indexOf(pane);
    if (idx >= 0) panes.splice(idx, 1);
  }

  function closeAll() {
    while (panes.length) removePane(panes[panes.length - 1]);
  }

  function refitAll() {
    for (const pane of panes) if (pane._fitAndResize) pane._fitAndResize();
  }

  return { addPane, closeAll, refitAll, panes };
}

const generalPanes = makePaneManager(generalPanesEl, () => null);

// One pane manager per project, created lazily and kept alive for the whole
// browser session - switching projects or going back to the main menu only
// hides the container, it never closes the sockets/processes underneath.
const projectPaneManagers = new Map(); // id -> { container, manager }

function showProjectPanes(id) {
  for (const [pid, entry] of projectPaneManagers) {
    entry.container.hidden = pid !== id;
  }
  let entry = projectPaneManagers.get(id);
  if (!entry) {
    const container = document.createElement("div");
    container.className = "terminal-panes";
    projectPanesEl.appendChild(container);
    const manager = makePaneManager(container, () => id);
    entry = { container, manager };
    projectPaneManagers.set(id, entry);
    manager.addPane();
  } else {
    entry.manager.refitAll();
  }
}

function closeProjectPanes(id) {
  const entry = projectPaneManagers.get(id);
  if (!entry) return;
  entry.manager.closeAll();
  entry.container.remove();
  projectPaneManagers.delete(id);
}

generalNewAgentBtn.addEventListener("click", () => generalPanes.addPane("agent"));
generalNewShellBtn.addEventListener("click", () => generalPanes.addPane("shell"));
projectNewAgentBtn.addEventListener("click", () => {
  const entry = projectPaneManagers.get(currentProjectId);
  if (entry) entry.manager.addPane("agent");
});
projectNewShellBtn.addEventListener("click", () => {
  const entry = projectPaneManagers.get(currentProjectId);
  if (entry) entry.manager.addPane("shell");
});

window.addEventListener("resize", () => {
  generalPanes.refitAll();
  const entry = projectPaneManagers.get(currentProjectId);
  if (entry) entry.manager.refitAll();
});

async function loadAgentCommand() {
  const res = await fetch("/api/settings", { headers: authHeaders });
  if (!res.ok) return;
  const data = await res.json();
  agentCmdInput.value = data.agent_command;
}

agentCmdSave.addEventListener("click", async () => {
  const agent_command = agentCmdInput.value.trim();
  if (!agent_command) return;
  const res = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({ agent_command }),
  });
});

loadProjects();
loadAgentCommand();
generalPanes.addPane();
