"""HTTP server, REST API, and self-contained HTML page for the Trello kanban board.

The TrelloServer class starts a lightweight HTTP server (stdlib http.server)
in a background daemon thread, serving a single-page kanban app with three
columns: Loaded (backlog), Processing (in progress), Done (completed). Cards
move by drag-and-drop or buttons, both directions.

The server *instance* is the single source of truth for task state (guarded by
a lock) and is attached to the HTTPServer so the request handler reads it via
``self.server.backend`` — no class-level mutable state.

REST API:
    GET    /api/state        — {tasks: [...], auto_advance: bool, running_id: str|None}
    GET    /api/tasks        — list all tasks
    POST   /api/tasks        — create a task        (body: {"description": "..."})
    PATCH  /api/tasks/<id>   — move a task          (body: {"status": "processing"})
    DELETE /api/tasks/<id>   — remove a task
    POST   /api/settings     — board settings       (body: {"auto_advance": true})

The CLI watch loop (see trello_handler.py) reads task state directly: it picks
the oldest task in "processing" (FIFO), or, when auto-advance is on and nothing
is processing, pulls the next "loaded" card. No fragile notification queue.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from http.server import HTTPServer
from typing import Any

_VALID_STATUS = ("loaded", "processing", "done")


def _now() -> str:
    return datetime.now(UTC).isoformat()


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nova Kanban Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg-deep: #0a0b0f;
    --bg-surface: #11131a;
    --bg-elevated: #181b24;
    --border-subtle: #1e2230;
    --border-mid: #2a2f42;
    --text-primary: #e2e4ed;
    --text-secondary: #8b90a5;
    --text-muted: #555a6e;
    --amber: #fbbf24;
    --amber-glow: rgba(251, 191, 36, 0.25);
    --cyan: #22d3ee;
    --cyan-glow: rgba(34, 211, 238, 0.25);
    --emerald: #34d399;
    --emerald-glow: rgba(52, 211, 153, 0.25);
    --radius: 12px;
    --radius-sm: 8px;
  }

  html { height: 100%; }

  body {
    font-family: 'Space Grotesk', system-ui, sans-serif;
    background: var(--bg-deep);
    color: var(--text-primary);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    position: relative;
    overflow-x: hidden;
  }

  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(34, 211, 238, 0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(34, 211, 238, 0.03) 1px, transparent 1px);
    background-size: 48px 48px;
    pointer-events: none;
    z-index: 0;
  }

  body::after {
    content: '';
    position: fixed;
    inset: 0;
    background: radial-gradient(ellipse 80% 60% at 50% -20%, rgba(34, 211, 238, 0.06), transparent 70%);
    pointer-events: none;
    z-index: 0;
  }

  .grain {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 1;
    opacity: 0.035;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
    background-repeat: repeat;
    background-size: 256px 256px;
  }

  header {
    position: relative;
    z-index: 2;
    padding: 24px 28px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    border-bottom: 1px solid var(--border-subtle);
  }

  .header-left { display: flex; align-items: center; gap: 14px; }

  .logo-icon {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    background: linear-gradient(135deg, var(--cyan), #0891b2);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: 700;
    color: #fff;
    box-shadow: 0 0 20px var(--cyan-glow);
  }

  header h1 {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.5px;
    background: linear-gradient(135deg, #e2e4ed, var(--cyan));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  header .subtitle { font-size: 13px; color: var(--text-secondary); font-weight: 400; }

  .header-right { display: flex; align-items: center; gap: 16px; }

  .toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-secondary);
    cursor: pointer;
    user-select: none;
  }
  .toggle input { display: none; }
  .toggle .track {
    width: 38px;
    height: 20px;
    border-radius: 20px;
    background: var(--border-mid);
    position: relative;
    transition: background 0.25s ease;
  }
  .toggle .track::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--text-secondary);
    transition: transform 0.25s ease, background 0.25s ease;
  }
  .toggle input:checked + .track { background: rgba(34, 211, 238, 0.3); }
  .toggle input:checked + .track::after { transform: translateX(18px); background: var(--cyan); }

  .server-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 20px;
    background: rgba(34, 211, 238, 0.08);
    border: 1px solid rgba(34, 211, 238, 0.15);
    color: var(--cyan);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .server-badge .live-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--cyan);
    animation: pulse-dot 2s ease-in-out infinite;
  }

  @keyframes pulse-dot {
    0%, 100% { opacity: 1; box-shadow: 0 0 4px var(--cyan-glow); }
    50% { opacity: 0.4; box-shadow: 0 0 8px var(--cyan-glow); }
  }

  .add-task {
    position: relative;
    z-index: 2;
    display: flex;
    gap: 10px;
    padding: 16px 28px;
    border-bottom: 1px solid var(--border-subtle);
  }

  .add-task input {
    flex: 1;
    padding: 12px 16px;
    border: 1px solid var(--border-mid);
    border-radius: var(--radius-sm);
    background: var(--bg-surface);
    color: var(--text-primary);
    font-family: 'Space Grotesk', sans-serif;
    font-size: 14px;
    outline: none;
    transition: all 0.25s ease;
  }
  .add-task input:focus { border-color: var(--cyan); box-shadow: 0 0 0 3px var(--cyan-glow); }
  .add-task input::placeholder { color: var(--text-muted); }

  .add-task button {
    padding: 12px 24px;
    border: none;
    border-radius: var(--radius-sm);
    background: linear-gradient(135deg, var(--cyan), #0891b2);
    color: #fff;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.25s ease;
    letter-spacing: 0.3px;
  }
  .add-task button:hover { transform: translateY(-1px); box-shadow: 0 4px 20px var(--cyan-glow); }
  .add-task button:active { transform: translateY(0); }

  .board {
    position: relative;
    z-index: 2;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 20px;
    padding: 24px 28px;
    flex: 1;
    min-height: 0;
  }

  @media (max-width: 900px) {
    .board { grid-template-columns: 1fr; }
    header { flex-direction: column; align-items: flex-start; }
  }

  .column {
    background: var(--bg-surface);
    border-radius: var(--radius);
    border: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    min-height: 320px;
    transition: border-color 0.3s ease;
  }
  .column:hover { border-color: var(--border-mid); }

  .column-header {
    padding: 16px 18px 14px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    border-bottom: 1px solid var(--border-subtle);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .column-header .count {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    padding: 2px 10px;
    border-radius: 12px;
    font-weight: 500;
  }

  .column-header.loaded { color: var(--amber); }
  .column-header.loaded .count { background: rgba(251, 191, 36, 0.1); color: var(--amber); }
  .column-header.processing { color: var(--cyan); }
  .column-header.processing .count { background: rgba(34, 211, 238, 0.1); color: var(--cyan); }
  .column-header.done { color: var(--emerald); }
  .column-header.done .count { background: rgba(52, 211, 153, 0.1); color: var(--emerald); }

  .task-list {
    flex: 1;
    padding: 10px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    transition: background 0.2s ease;
  }
  .task-list.drag-over {
    background: rgba(34, 211, 238, 0.04);
    outline: 1px dashed var(--border-mid);
    outline-offset: -4px;
    border-radius: var(--radius-sm);
  }

  .task-list::-webkit-scrollbar { width: 4px; }
  .task-list::-webkit-scrollbar-track { background: transparent; }
  .task-list::-webkit-scrollbar-thumb { background: var(--border-mid); border-radius: 4px; }
  .task-list::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

  .task-card {
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 14px 16px;
    cursor: grab;
    transition: border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
    position: relative;
    animation: card-enter 0.35s ease both;
  }
  .task-card:active { cursor: grabbing; }

  @keyframes card-enter {
    from { opacity: 0; transform: translateY(8px) scale(0.97); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  .task-card:hover { border-color: var(--border-mid); background: #1c1f2c; transform: translateY(-1px); }
  .task-card.dragging { opacity: 0.4; }

  .task-card .task-text {
    font-size: 14px;
    line-height: 1.45;
    word-break: break-word;
    padding-right: 24px;
    font-weight: 450;
  }

  .task-card .task-meta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: var(--text-muted);
    margin-top: 8px;
    letter-spacing: 0.2px;
  }

  .task-card .delete-btn {
    position: absolute;
    top: 8px;
    right: 8px;
    width: 22px;
    height: 22px;
    border: none;
    background: none;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 16px;
    line-height: 1;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: all 0.2s ease;
  }
  .task-card:hover .delete-btn { opacity: 1; }
  .task-card .delete-btn:hover { color: #f87171; background: rgba(248, 113, 113, 0.1); }

  .task-card.loaded { border-left: 3px solid var(--amber); }
  .task-card.loaded:hover { box-shadow: 0 0 16px var(--amber-glow); }
  .task-card.processing { border-left: 3px solid var(--cyan); background: linear-gradient(135deg, var(--bg-elevated), rgba(34, 211, 238, 0.03)); }
  .task-card.processing:hover { box-shadow: 0 0 16px var(--cyan-glow); }
  .task-card.running { box-shadow: 0 0 18px var(--cyan-glow); border-color: var(--cyan); }
  .task-card.done { border-left: 3px solid var(--emerald); opacity: 0.78; }
  .task-card.done:hover { opacity: 1; box-shadow: 0 0 16px var(--emerald-glow); }

  .run-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: var(--cyan);
    margin-top: 8px;
    display: flex;
    align-items: center;
    gap: 5px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .run-label.queued { color: var(--text-muted); }
  .run-label .live-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--cyan);
    animation: pulse-dot 1.2s ease-in-out infinite;
  }

  .card-actions { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }

  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    padding: 3px 8px;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    letter-spacing: 0.3px;
    text-transform: uppercase;
    border: 1px solid transparent;
  }
  .status-badge.amber { background: rgba(251, 191, 36, 0.1); color: var(--amber); border-color: rgba(251, 191, 36, 0.15); }
  .status-badge.amber:hover { background: rgba(251, 191, 36, 0.18); box-shadow: 0 0 12px var(--amber-glow); }
  .status-badge.cyan { background: rgba(34, 211, 238, 0.1); color: var(--cyan); border-color: rgba(34, 211, 238, 0.15); }
  .status-badge.cyan:hover { background: rgba(34, 211, 238, 0.18); box-shadow: 0 0 12px var(--cyan-glow); }
  .status-badge.emerald { background: rgba(52, 211, 153, 0.1); color: var(--emerald); border-color: rgba(52, 211, 153, 0.15); }
  .status-badge.emerald:hover { background: rgba(52, 211, 153, 0.18); box-shadow: 0 0 12px var(--emerald-glow); }
  .status-badge.neutral { background: rgba(139, 144, 165, 0.08); color: var(--text-secondary); border-color: var(--border-mid); }
  .status-badge.neutral:hover { background: rgba(139, 144, 165, 0.16); color: var(--text-primary); }

  .empty-state {
    padding: 32px 16px;
    text-align: center;
    color: var(--text-muted);
    font-size: 13px;
    font-weight: 400;
    letter-spacing: 0.3px;
    pointer-events: none;
  }
  .empty-state .empty-icon { font-size: 28px; margin-bottom: 8px; opacity: 0.5; }

  .status-bar {
    position: relative;
    z-index: 2;
    padding: 12px 28px;
    border-top: 1px solid var(--border-subtle);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-secondary);
    display: flex;
    gap: 24px;
    align-items: center;
  }
  .status-bar span { display: flex; align-items: center; gap: 6px; }
  .status-bar strong { color: var(--text-primary); font-weight: 500; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
  .dot.amber { background: var(--amber); box-shadow: 0 0 6px var(--amber-glow); }
  .dot.cyan { background: var(--cyan); box-shadow: 0 0 6px var(--cyan-glow); }
  .dot.emerald { background: var(--emerald); box-shadow: 0 0 6px var(--emerald-glow); }
  .status-bar .divider { width: 1px; height: 14px; background: var(--border-subtle); }
  .status-bar .hint { margin-left: auto; color: var(--text-muted); }

  /* Detail modal */
  .modal-overlay {
    position: fixed;
    inset: 0;
    z-index: 50;
    background: rgba(5, 6, 9, 0.72);
    backdrop-filter: blur(4px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .modal-overlay.open { display: flex; animation: fade 0.2s ease; }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }

  .modal {
    background: var(--bg-surface);
    border: 1px solid var(--border-mid);
    border-radius: var(--radius);
    max-width: 680px;
    width: 100%;
    max-height: 82vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.55);
  }
  .modal-head {
    padding: 18px 22px;
    border-bottom: 1px solid var(--border-subtle);
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
  }
  .modal-head h2 { font-size: 16px; font-weight: 600; line-height: 1.4; word-break: break-word; }
  .modal-close { background: none; border: none; color: var(--text-muted); font-size: 24px; cursor: pointer; line-height: 1; }
  .modal-close:hover { color: var(--text-primary); }
  .modal-body { padding: 18px 22px; overflow-y: auto; }
  .modal-meta {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-secondary);
    margin-bottom: 16px;
  }
  .modal-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); margin-bottom: 8px; }
  .modal-result {
    background: var(--bg-deep);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 14px 16px;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-primary);
    max-height: 44vh;
    overflow-y: auto;
  }
  .modal-result.empty { color: var(--text-muted); font-style: italic; }
</style>
</head>
<body>
<div class="grain"></div>

<header>
  <div class="header-left">
    <div class="logo-icon">N</div>
    <div>
      <h1>Kanban Board</h1>
      <span class="subtitle">Drag cards to Processing — the agent works them one at a time</span>
    </div>
  </div>
  <div class="header-right">
    <label class="toggle" title="When on, the agent automatically pulls the next Loaded card while idle">
      <input type="checkbox" id="auto-toggle">
      <span class="track"></span>
      <span>Auto-advance</span>
    </label>
    <div class="server-badge"><span class="live-dot"></span> LIVE</div>
  </div>
</header>

<div class="add-task">
  <input type="text" id="task-input" placeholder="Describe a task for the agent..." autofocus>
  <button id="add-btn">Add Task</button>
</div>

<div class="board">
  <div class="column">
    <div class="column-header loaded"><span>Loaded</span><span class="count" id="loaded-count">0</span></div>
    <div class="task-list" id="loaded-list" data-status="loaded"></div>
  </div>
  <div class="column">
    <div class="column-header processing"><span>Processing</span><span class="count" id="processing-count">0</span></div>
    <div class="task-list" id="processing-list" data-status="processing"></div>
  </div>
  <div class="column">
    <div class="column-header done"><span>Done</span><span class="count" id="done-count">0</span></div>
    <div class="task-list" id="done-list" data-status="done"></div>
  </div>
</div>

<div class="status-bar">
  <span><span class="dot amber"></span> Loaded: <strong id="loaded-total">0</strong></span>
  <span class="divider"></span>
  <span><span class="dot cyan"></span> Processing: <strong id="processing-total">0</strong></span>
  <span class="divider"></span>
  <span><span class="dot emerald"></span> Done: <strong id="done-total">0</strong></span>
  <span class="hint">Click a card for details &middot; drag to move</span>
</div>

<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-head">
      <h2 id="modal-title"></h2>
      <button class="modal-close" id="modal-close" title="Close">&times;</button>
    </div>
    <div class="modal-body">
      <div class="modal-meta" id="modal-meta"></div>
      <div class="modal-label">Result</div>
      <div class="modal-result" id="modal-result"></div>
    </div>
  </div>
</div>

<script>
const state = { tasks: [], auto_advance: false, running_id: null };
let lastSnapshot = '';
let isDragging = false;
let modalTaskId = null;

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : text;
  return div.innerHTML;
}

// ---- data ----------------------------------------------------------------
async function fetchState() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    state.tasks = data.tasks || [];
    state.auto_advance = !!data.auto_advance;
    state.running_id = data.running_id || null;
    maybeRender();
  } catch (e) { /* server gone — keep last view */ }
}

async function addTask(description) {
  try {
    await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Nova-Request': '1' },
      body: JSON.stringify({ description })
    });
    await fetchState();
  } catch (e) { console.error('add failed', e); }
}

async function moveTask(id, status) {
  // optimistic local update so the board feels instant
  const t = state.tasks.find(x => x.id === id);
  if (t) { t.status = status; lastSnapshot = ''; render(); }
  try {
    await fetch('/api/tasks/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-Nova-Request': '1' },
      body: JSON.stringify({ status })
    });
    await fetchState();
  } catch (e) { console.error('move failed', e); await fetchState(); }
}

async function deleteTask(id) {
  try {
    await fetch('/api/tasks/' + id, { method: 'DELETE', headers: {'X-Nova-Request': '1'} });
    await fetchState();
  } catch (e) { console.error('delete failed', e); }
}

async function setAutoAdvance(on) {
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Nova-Request': '1' },
      body: JSON.stringify({ auto_advance: on })
    });
    await fetchState();
  } catch (e) { console.error('settings failed', e); }
}

// ---- render --------------------------------------------------------------
function maybeRender() {
  if (isDragging) return;                 // never rebuild cards mid-drag
  const snap = JSON.stringify(state);
  if (snap === lastSnapshot) { if (modalTaskId) refreshModal(); return; }
  lastSnapshot = snap;
  render();
  if (modalTaskId) refreshModal();
}

function render() {
  const cols = { loaded: [], processing: [], done: [] };
  for (const t of state.tasks) { (cols[t.status] || cols.loaded).push(t); }

  document.getElementById('auto-toggle').checked = state.auto_advance;

  for (const s of ['loaded', 'processing', 'done']) {
    document.getElementById(s + '-count').textContent = cols[s].length;
    document.getElementById(s + '-total').textContent = cols[s].length;
    renderList(s + '-list', cols[s], s);
  }
}

function renderList(listId, items, status) {
  const el = document.getElementById(listId);
  if (items.length === 0) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9744;</div>No tasks here</div>';
    return;
  }
  el.innerHTML = items.map((t, i) => cardHtml(t, status, i)).join('');
}

function cardHtml(t, status, i) {
  const running = (t.id === state.running_id);
  let actions = '';
  if (status === 'loaded') {
    actions = `<span class="status-badge amber" data-action="move" data-target="processing">&#9654; Start</span>`;
  } else if (status === 'processing') {
    actions = `<span class="status-badge cyan" data-action="move" data-target="done">&#10003; Complete</span>`
            + `<span class="status-badge neutral" data-action="move" data-target="loaded">&#8592; Back</span>`;
  } else {
    actions = `<span class="status-badge neutral" data-action="move" data-target="processing">&#8634; Reopen</span>`;
  }

  let runLabel = '';
  if (status === 'processing') {
    runLabel = running
      ? `<div class="run-label"><span class="live-dot"></span> running</div>`
      : `<div class="run-label queued">queued</div>`;
  }

  const meta = [
    t.created_at ? 'Added ' + fmtTime(t.created_at) : '',
    t.completed_at ? 'Done ' + fmtTime(t.completed_at) : '',
    t.result ? '✓ result' : ''
  ].filter(Boolean).join(' · ');

  return `<div class="task-card ${status}${running ? ' running' : ''}" draggable="true" data-id="${t.id}" style="animation-delay:${Math.min(i * 0.04, 0.3)}s">
    <div class="task-text">${escapeHtml(t.description)}</div>
    ${runLabel}
    <div class="task-meta">${meta}</div>
    <div class="card-actions">${actions}</div>
    <button class="delete-btn" data-action="delete" title="Delete task">&times;</button>
  </div>`;
}

// ---- detail modal --------------------------------------------------------
function openModal(id) {
  const t = state.tasks.find(x => x.id === id);
  if (!t) return;
  modalTaskId = id;
  fillModal(t);
  document.getElementById('modal').classList.add('open');
}

function fillModal(t) {
  document.getElementById('modal-title').textContent = t.description;
  const meta = [
    'Status: ' + t.status,
    t.created_at ? 'Added ' + fmtTime(t.created_at) : '',
    t.started_at ? 'Started ' + fmtTime(t.started_at) : '',
    t.completed_at ? 'Done ' + fmtTime(t.completed_at) : ''
  ].filter(Boolean).map(s => `<span>${escapeHtml(s)}</span>`).join('');
  document.getElementById('modal-meta').innerHTML = meta;

  const r = document.getElementById('modal-result');
  if (t.result) {
    r.textContent = t.result;
    r.classList.remove('empty');
  } else {
    r.classList.add('empty');
    if (state.running_id === t.id) r.textContent = 'Running… the result will appear here when the agent finishes.';
    else if (t.status === 'done') r.textContent = 'No output was captured for this task.';
    else r.textContent = 'Not started yet.';
  }
}

function refreshModal() {
  if (!modalTaskId) return;
  const t = state.tasks.find(x => x.id === modalTaskId);
  if (t) fillModal(t);
  else closeModal();
}

function closeModal() {
  modalTaskId = null;
  document.getElementById('modal').classList.remove('open');
}

// ---- events (delegated, bound once) --------------------------------------
const board = document.querySelector('.board');

board.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action]');
  const card = e.target.closest('.task-card');
  if (btn && card) {
    e.stopPropagation();
    const id = card.dataset.id;
    if (btn.dataset.action === 'delete') deleteTask(id);
    else if (btn.dataset.action === 'move') moveTask(id, btn.dataset.target);
    return;
  }
  if (card) openModal(card.dataset.id);
});

board.addEventListener('dragstart', (e) => {
  const card = e.target.closest('.task-card');
  if (!card) return;
  isDragging = true;
  e.dataTransfer.setData('text/plain', card.dataset.id);
  e.dataTransfer.effectAllowed = 'move';
  card.classList.add('dragging');
});

board.addEventListener('dragend', (e) => {
  isDragging = false;
  const card = e.target.closest('.task-card');
  if (card) card.classList.remove('dragging');
  document.querySelectorAll('.task-list').forEach(l => l.classList.remove('drag-over'));
  lastSnapshot = '';
  maybeRender();
});

['loaded-list', 'processing-list', 'done-list'].forEach(id => {
  const list = document.getElementById(id);
  list.addEventListener('dragover', (e) => { e.preventDefault(); list.classList.add('drag-over'); });
  list.addEventListener('dragleave', (e) => { if (!list.contains(e.relatedTarget)) list.classList.remove('drag-over'); });
  list.addEventListener('drop', (e) => {
    e.preventDefault();
    list.classList.remove('drag-over');
    const taskId = e.dataTransfer.getData('text/plain');
    if (taskId) moveTask(taskId, list.dataset.status);
  });
});

document.getElementById('add-btn').addEventListener('click', submitTask);
document.getElementById('task-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitTask(); });
function submitTask() {
  const input = document.getElementById('task-input');
  const text = input.value.trim();
  if (text) { addTask(text); input.value = ''; }
}

document.getElementById('auto-toggle').addEventListener('change', (e) => setAutoAdvance(e.target.checked));
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && modalTaskId) closeModal(); });

fetchState();
setInterval(fetchState, 2000);
</script>
</body>
</html>"""


from novacode_cli.security.local_http import LocalRequestHandler


class TrelloRequestHandler(LocalRequestHandler):
    """HTTP request handler. Reads task state from ``self.server.backend``."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress default HTTP server logging."""

    @property
    def _backend(self) -> TrelloServer:
        return self.server.backend  # type: ignore[attr-defined]

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str, status: int = 200) -> None:
        from novacode_cli.security.local_http import secure_html
        html = secure_html(html)
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _path_parts(self) -> list[str]:
        return [p for p in self.path.rstrip("/").split("?")[0].split("/") if p]

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parts = self._path_parts()
        if not parts:
            self._send_html(HTML_PAGE)
            return
        if parts == ["favicon.ico"]:
            self.send_response(204)
            self.end_headers()
            return
        if parts == ["api", "state"]:
            self._send_json(self._backend.get_state())
            return
        if parts == ["api", "tasks"]:
            self._send_json(self._backend.get_tasks())
            return
        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        parts = self._path_parts()
        if parts == ["api", "tasks"]:
            description = str(self._read_body().get("description", "")).strip()
            if not description:
                self._send_json({"error": "description is required"}, 400)
                return
            self._send_json(self._backend.add_task(description), 201)
            return
        if parts == ["api", "settings"]:
            body = self._read_body()
            self._backend.set_auto_advance(bool(body.get("auto_advance", False)))
            self._send_json(self._backend.get_state())
            return
        self._send_json({"error": "Not found"}, 404)

    def do_PATCH(self) -> None:
        parts = self._path_parts()
        if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            new_status = str(self._read_body().get("status", "")).strip()
            if new_status not in _VALID_STATUS:
                self._send_json({"error": f"status must be one of {_VALID_STATUS}"}, 400)
                return
            task = self._backend.move_task(parts[2], new_status)
            self._send_json(task) if task else self._send_json({"error": "Task not found"}, 404)
            return
        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        parts = self._path_parts()
        if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            task = self._backend.delete_task(parts[2])
            self._send_json(task) if task else self._send_json({"error": "Task not found"}, 404)
            return
        self._send_json({"error": "Not found"}, 404)


class TrelloServer:
    """Lightweight kanban HTTP server — the single source of truth for tasks.

    Runs in a background daemon thread. The server instance is attached to the
    HTTPServer so the request handler reads/mutates state through it under a
    lock. The CLI watch loop reads task state directly (no notification queue).
    """

    def __init__(self) -> None:
        self._tasks: list[dict] = []
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0
        self.is_running: bool = False
        self.auto_advance: bool = False
        self.running_id: str | None = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> int:
        """Start the HTTP server in a background daemon thread; return the port."""
        self._server = HTTPServer(("127.0.0.1", 0), TrelloRequestHandler)
        import secrets
        self._server.auth_token = secrets.token_urlsafe(32)
        self._server.backend = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self.is_running = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="trello-server",
        )
        self._thread.start()
        return self.port

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/?token={self._server.auth_token}" if self._server else ""

    def stop(self) -> None:
        """Stop the HTTP server."""
        self.is_running = False
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- task mutations (all guarded) --------------------------------------
    def add_task(self, description: str) -> dict:
        task = {
            "id": str(uuid.uuid4()),
            "description": description,
            "status": "loaded",
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
            "result": None,
        }
        with self._lock:
            self._tasks.append(task)
        return task.copy()

    def move_task(self, task_id: str, status: str) -> dict | None:
        if status not in _VALID_STATUS:
            return None
        with self._lock:
            for task in self._tasks:
                if task["id"] == task_id:
                    task["status"] = status
                    if status == "processing" and not task["started_at"]:
                        task["started_at"] = _now()
                    if status == "done":
                        task["completed_at"] = _now()
                    else:
                        task["completed_at"] = None
                    return task.copy()
        return None

    def delete_task(self, task_id: str) -> dict | None:
        with self._lock:
            for i, task in enumerate(self._tasks):
                if task["id"] == task_id:
                    return self._tasks.pop(i)
        return None

    def mark_done(self, task_id: str, result: str | None = None) -> None:
        """Mark a task done in-process and attach the agent's result."""
        with self._lock:
            for task in self._tasks:
                if task["id"] == task_id:
                    task["status"] = "done"
                    task["completed_at"] = _now()
                    if result is not None:
                        task["result"] = result
                    return

    def set_auto_advance(self, enabled: bool) -> None:
        self.auto_advance = enabled

    def set_running(self, task_id: str | None) -> None:
        self.running_id = task_id

    # -- reads -------------------------------------------------------------
    def get_tasks(self) -> list[dict]:
        with self._lock:
            return [t.copy() for t in self._tasks]

    def get_state(self) -> dict:
        with self._lock:
            return {
                "tasks": [t.copy() for t in self._tasks],
                "auto_advance": self.auto_advance,
                "running_id": self.running_id,
            }

    def next_processing_task(self) -> dict | None:
        """Return the oldest task in 'processing' (FIFO by creation), or None."""
        with self._lock:
            for task in self._tasks:
                if task["status"] == "processing":
                    return task.copy()
        return None

    def pop_next_loaded_task(self) -> dict | None:
        """Move the oldest 'loaded' task to 'processing' and return it (auto-advance)."""
        with self._lock:
            for task in self._tasks:
                if task["status"] == "loaded":
                    task["status"] = "processing"
                    if not task["started_at"]:
                        task["started_at"] = _now()
                    return task.copy()
        return None

    def get_task_counts(self) -> dict[str, int]:
        counts = {"loaded": 0, "processing": 0, "done": 0}
        with self._lock:
            for task in self._tasks:
                status = task.get("status", "loaded")
                if status in counts:
                    counts[status] += 1
        return counts

    def get_processing_task(self) -> dict | None:
        with self._lock:
            for task in self._tasks:
                if task["status"] == "processing":
                    return task.copy()
        return None
