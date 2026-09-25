"""Handler for /chat command — launches a local **Council** web UI.

The ``/chat`` web UI runs a *council of agents*: five personas debate a topic
chatroom-style, then score each other; the highest-scoring answer is the
verdict. The server runs in a background thread
(``http.server.ThreadingHTTPServer``) and streams the council over **Server-Sent
Events** to the browser, driving the same LangGraph session's configured model
via ``asyncio.run_coroutine_threadsafe`` onto the CLI event loop.

The council logic itself lives in :mod:`novacode_cli.council`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from novacode_cli.config.config import COLORS, console

logger = logging.getLogger(__name__)

# Sentinel pushed onto the event queue when the council run finishes.
_STREAM_END = object()

# ---------------------------------------------------------------------------
# Module-level state shared with the background HTTP server thread.
# Set by set_agent_refs before the server thread starts.
# ---------------------------------------------------------------------------

_agent: Any = None
_assistant_id: str | None = None
_session_state: Any = None
_main_loop: asyncio.AbstractEventLoop | None = None

_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_server_port: int | None = None

# Only one council can be in session at a time (a single shared model/loop).
_run_lock = threading.Lock()

# Prior council rounds (this server's lifetime), so follow-up topics can build on
# the earlier discussion. Each round: {"topic", "transcript": [[name, text]...],
# "winner"}. Guarded by _run_lock (only one run mutates it at a time). Reset via
# GET /api/council/reset or when the server stops.
_council_history: list[dict[str, Any]] = []

# How many rounds to retain. The prompt only reads the last few
# (council._MAX_HISTORY_ROUNDS); a small margin over that lets the UI show a
# little more scrollback without the list growing without bound.
_MAX_KEPT_ROUNDS = 12


# ---------------------------------------------------------------------------
# Embedded HTML council page
# ---------------------------------------------------------------------------

def _make_chat_html() -> str:
    """Return a self-contained council UI styled after the Nova Textual TUI.

    Tokyo-night palette, JetBrains Mono, matrix-rain banner with the NOVA ASCII
    logo, accent-bar message cards, and a prompt-dock composer with the ``>``
    chevron — mirroring ``cowork/ui.py`` so the council feels like the TUI.
    """
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Nova — Council</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root{
    --bg:#13141d; --surface:#1a1b26; --panel:#24283b; --boost:#2f3346;
    --border:#3b4261; --fg:#c0caf5; --muted:#565f89;
    --primary:#7aa2f7; --secondary:#9ece6a; --accent:#bb9af7;
    --success:#73daca; --warning:#e0af68; --error:#f7768e;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: 'JetBrains Mono', ui-monospace, 'Cascadia Code', 'SF Mono', monospace;
    background: var(--bg); color: var(--fg);
    height: 100vh; display: flex; flex-direction: column; overflow: hidden;
  }
  ::selection { background: var(--primary); color: #0f0f16; }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

  /* --- matrix-rain banner (TUI home screen) --- */
  .banner { position: relative; flex: 0 0 auto; height: clamp(120px, 15vw, 180px);
    overflow: hidden; border-bottom: 1px solid var(--border); background: var(--bg);
    animation: fadein .5s ease both; }
  #rain { position: absolute; inset: 0; width: 100%; height: 100%; }
  .logo { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    padding-bottom: 18px; font-size: clamp(4.5px, 0.95vw, 11px); line-height: 1.3; white-space: pre;
    color: var(--primary); text-shadow: 0 0 16px rgba(122,162,247,.45);
    user-select: none; pointer-events: none; overflow: hidden; }
  .bootline { position: absolute; left: 0; right: 0; bottom: 8px; text-align: center;
    font-size: 11px; color: var(--muted); letter-spacing: .06em; min-height: 16px; }
  .bootline .ok { color: var(--success); }

  /* --- header (TUI info bar) --- */
  header { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
    background: var(--surface); border-bottom: 1px solid var(--border); flex: 0 0 auto;
    animation: rise .35s ease both; }
  .mark { color: var(--primary); font-weight: 800; letter-spacing: .02em; white-space: nowrap; }
  .mode { font-size: 10px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase;
    color: #b4c6ef; background: #161f33; border: 1px solid var(--primary); padding: 2px 8px; white-space: nowrap; }
  .state { font-size: 11px; padding: 2px 8px; border: 1px solid; white-space: nowrap; }
  .state.ok { color: var(--success); border-color: var(--success); }
  .state.deny { color: var(--warning); border-color: var(--warning); }
  .run-badge { display: none; font-size: 11px; padding: 2px 8px; border: 1px solid var(--accent);
    color: var(--accent); white-space: nowrap; animation: pulse 1.6s ease-in-out infinite; }
  .run-badge.show { display: inline-block; }
  .sid { margin-left: auto; color: var(--muted); font-size: 11px; white-space: nowrap; }
  #new-thread { background: transparent; color: var(--muted); border: 1px solid var(--border);
    font: inherit; font-size: 10px; padding: 3px 10px; cursor: pointer; text-transform: uppercase;
    letter-spacing: .1em; transition: border-color .15s, color .15s; white-space: nowrap; }
  #new-thread:hover { border-color: var(--primary); color: var(--fg); }

  /* --- the bench — advisor seats with live state + vote tally --- */
  #bench { display: none; flex-shrink: 0; gap: 8px; padding: 10px 14px;
    border-bottom: 1px solid var(--border); background: var(--surface); overflow-x: auto; }
  #bench.active { display: flex; }
  #bench .seat { flex: 1 1 0; min-width: 92px; display: flex; flex-direction: column;
    align-items: center; gap: 4px; padding: 8px 6px; border-radius: 4px;
    border: 1px solid var(--border); border-left: 3px solid var(--seat, var(--border));
    background: var(--panel); transition: border-color .3s, box-shadow .3s, opacity .3s, transform .2s;
    opacity: .5; }
  #bench .seat-av { position: relative; font-size: 20px; width: 36px; height: 36px;
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    border: 1px solid var(--seat, var(--border)); background: var(--bg); }
  #bench .seat-badge { position: absolute; top: -6px; right: -8px; min-width: 17px; height: 17px;
    padding: 0 4px; border-radius: 9px; font-size: 10px; font-weight: 700; line-height: 17px;
    text-align: center; background: var(--panel); color: var(--muted); font-family: inherit;
    opacity: 0; transform: scale(.6); transition: opacity .25s, transform .25s, background .25s, color .25s; }
  #bench .seat-badge.has { opacity: 1; transform: scale(1); background: var(--warning); color: #1a1410; }
  #bench .seat-name { font-size: 10.5px; font-weight: 600; color: var(--fg); text-align: center; }
  #bench .seat-state { font-size: 9px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  #bench .seat[data-state="speaking"] { opacity: 1; border-color: var(--seat); box-shadow: 0 0 0 1px var(--seat); }
  #bench .seat[data-state="speaking"] .seat-av { animation: pulse 1.3s ease-in-out infinite; }
  #bench .seat[data-state="answered"] { opacity: 1; }
  #bench .seat[data-state="answered"] .seat-state { color: var(--seat); }
  #bench .seat[data-state="voting"] { opacity: 1; }
  #bench .seat[data-state="voting"] .seat-state { color: var(--warning); }
  #bench .seat[data-state="winner"] { opacity: 1; border-color: var(--warning);
    box-shadow: 0 0 0 1px var(--warning), 0 0 22px rgba(224,175,104,.25); transform: translateY(-2px); }
  #bench .seat[data-state="winner"] .seat-state { color: var(--warning); font-weight: 700; }

  /* --- messages --- */
  #messages { flex: 1; overflow-y: auto; padding: 20px 16px; display: flex;
    flex-direction: column; gap: 12px; scroll-behavior: smooth; }
  #messages::-webkit-scrollbar { width: 8px; }
  #messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

  .topic-banner { align-self: center; max-width: 80%; text-align: center; color: var(--muted);
    font-size: 13px; padding: 6px 18px; border-bottom: 1px solid var(--border); }

  /* agent message — accent-bar card like the TUI transcript */
  .agent { max-width: 84%; align-self: flex-start; background: var(--surface);
    border-left: 3px solid var(--seat, var(--primary)); padding: 10px 14px;
    animation: rise .3s ease both; }
  .agent .who { display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
    font-weight: 700; font-size: 11px; color: var(--seat, var(--fg)); }
  .agent .who .avatar { font-size: 14px; }
  .agent .who-tag { margin-left: auto; font-size: 9px; font-weight: 500; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); border: 1px solid var(--border); padding: 1px 6px;
    border-radius: 10px; white-space: nowrap; }
  .agent .search-chip { font-size: 11px; color: var(--warning); margin-bottom: 6px; opacity: .9;
    word-break: break-word; }
  .agent .body { line-height: 1.6; font-size: 13px; color: var(--fg); }
  .agent .body p { margin: 0 0 8px; }
  .agent .body p:last-child { margin-bottom: 0; }
  .agent .body pre { background: var(--bg) !important; border: 1px solid var(--border);
    border-left: 3px solid var(--accent); border-radius: 3px; padding: 10px 12px; overflow-x: auto;
    margin: 8px 0; font-size: 12px; }
  .agent .body code { background: var(--boost); border: 1px solid var(--border); padding: 1px 5px;
    border-radius: 3px; font-size: 12px; color: var(--secondary); }
  .agent .body pre code { background: none !important; border: 0; padding: 0 !important; color: var(--fg); }
  .agent.streaming .body::after { content: '▍'; color: var(--seat, var(--primary));
    animation: blink 1s steps(1) infinite; margin-left: 1px; }

  /* phase divider */
  .phase { align-self: center; color: var(--accent); font-size: 10px; text-transform: uppercase;
    letter-spacing: .2em; font-weight: 700; margin: 8px 0 2px; display: flex; align-items: center; gap: 12px; }
  .phase::before, .phase::after { content: ''; height: 1px; width: 60px; background: var(--border); }
  .phase-sub { text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--muted);
    font-size: 10px; margin-left: 10px; }

  /* one-line democratic votes */
  .vote-row { align-self: stretch; max-width: 84%; display: flex; flex-wrap: wrap;
    align-items: baseline; gap: 8px; padding: 6px 12px; font-size: 12px;
    border-left: 3px solid var(--seat, var(--primary)); background: var(--surface);
    animation: rise .25s ease both; }
  .vote-row .v-voter { font-weight: 700; color: var(--seat, var(--fg)); }
  .vote-row .v-arrow { color: var(--muted); }
  .vote-row .v-choice { font-weight: 700; }
  .vote-row .v-choice.muted { color: var(--muted); font-weight: 400; font-style: italic; }
  .vote-row .v-why { flex-basis: 100%; color: var(--muted); font-size: 11px; }

  /* verdict */
  .verdict { align-self: stretch; margin: 8px 0; background: var(--surface);
    border: 1px solid var(--border); border-left: 4px solid var(--warning); border-radius: 4px;
    padding: 16px 18px; animation: rise .4s ease both; }
  .verdict h3 { font-size: 14px; color: var(--fg); margin-bottom: 10px; display: flex;
    align-items: center; gap: 10px; font-weight: 800; }
  .verdict h3 .w-name { color: var(--warning); }
  .verdict-sub { color: var(--muted); font-size: 11px; margin: -4px 0 12px; letter-spacing: .03em; }
  .leaderboard { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
  .lb-row { display: flex; align-items: center; gap: 10px; font-size: 12px; }
  .lb-row .lb-name { width: 150px; color: var(--muted); flex-shrink: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .lb-row.win .lb-name { color: var(--fg); font-weight: 700; }
  .lb-bar { flex: 1; height: 8px; background: var(--panel); border-radius: 4px; overflow: hidden; }
  .lb-fill { height: 100%; background: var(--seat, var(--primary)); border-radius: 4px;
    transition: width .6s ease; }
  .lb-row .lb-pts { width: 60px; text-align: right; color: var(--warning); font-weight: 600;
    font-variant-numeric: tabular-nums; }
  .winner-label { margin-top: 4px; font-size: 9.5px; text-transform: uppercase; letter-spacing: .12em;
    color: var(--muted); }
  .verdict .winner-answer { border-top: 1px solid var(--border); padding-top: 10px; line-height: 1.6;
    font-size: 13px; color: var(--fg); }
  .verdict .winner-answer pre { background: var(--bg) !important; border: 1px solid var(--border);
    border-left: 3px solid var(--accent); padding: 10px 12px; border-radius: 3px; overflow-x: auto; }

  .error-msg { align-self: center; color: var(--error); font-size: 12px; padding: 8px 18px;
    text-align: center; background: rgba(247,118,142,.08); border-radius: 4px;
    border: 1px solid rgba(247,118,142,.25); }

  /* welcome */
  .welcome { text-align: center; margin: auto; padding: 40px 24px; max-width: 520px;
    animation: rise .5s ease both; }
  .welcome h2 { font-weight: 800; font-size: 22px; color: var(--fg); margin-bottom: 6px; }
  .welcome .divider { width: 40px; height: 2px; background: var(--primary); margin: 14px auto; }
  .welcome p { font-size: 12.5px; line-height: 1.7; color: var(--muted); margin-bottom: 8px; }
  .welcome .seats { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 14px; }
  .welcome .seat { font-size: 11px; padding: 3px 10px; border: 1px solid var(--border);
    border-radius: 20px; color: var(--muted); }

  /* --- composer (TUI prompt dock) --- */
  #input-area { display: flex; align-items: stretch; border-top: 1px solid var(--border);
    background: var(--panel); flex-shrink: 0; }
  .mode-badge { display: flex; align-items: center; padding: 0 10px; font-size: 10px; font-weight: 700;
    letter-spacing: .14em; text-transform: uppercase; color: #b4c6ef; background: #161f33;
    border-right: 1px solid var(--primary); }
  .prefix { display: flex; align-items: center; padding: 0 10px; color: var(--accent); font-weight: 800;
    font-size: 16px; background: var(--panel); user-select: none; }
  #input-area textarea { flex: 1; resize: none; min-height: 56px; max-height: 180px; background: var(--panel);
    border: 0; color: var(--fg); font: inherit; font-size: 13px; padding: 16px 12px; outline: none;
    transition: background .2s; }
  #input-area textarea::placeholder { color: var(--muted); }
  #input-area textarea:focus { background: var(--boost); }
  #input-area textarea:disabled { opacity: .45; }
  #input-area button { background: var(--primary); color: #0f0f16; border: 0; font: inherit; font-weight: 700;
    font-size: 12px; padding: 0 18px; cursor: pointer; letter-spacing: .06em; transition: background .15s; }
  #input-area button:hover:not(:disabled) { background: #8fb4ff; }
  #input-area button:disabled { opacity: .4; cursor: not-allowed; }

  /* --- status bar --- */
  #status-bar { display: flex; align-items: center; gap: 16px; padding: 5px 14px;
    background: var(--surface); border-top: 1px solid var(--border); font-size: 11px; color: var(--muted);
    flex: 0 0 auto; animation: rise .4s ease both; animation-delay: .24s; }
  #status-bar .dot.on { color: var(--success); }
  #status-bar .dot.off { color: var(--muted); }
  #status-bar .spacer { flex: 1; }
  #status-bar .hint { color: var(--muted); }

  /* --- atmosphere --- */
  .scanlines { position: fixed; inset: 0; pointer-events: none; z-index: 9998; opacity: .5;
    background: repeating-linear-gradient(0deg, rgba(0,0,0,.14) 0 1px, transparent 1px 3px); }
  .vignette { position: fixed; inset: 0; pointer-events: none; z-index: 9997;
    background: radial-gradient(ellipse at center, transparent 58%, rgba(0,0,0,.38)); }

  /* --- motion --- */
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
  @keyframes blink { 0%,49% { opacity: 1; } 50%,100% { opacity: 0; } }
  @keyframes pulse { 0%,100% { opacity: .75; } 50% { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<div class="banner">
  <canvas id="rain"></canvas>
  <pre class="logo">⣿⣿⣿⣿⣟⠊⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿         ███╗   ██╗  ██████╗  ██╗   ██╗  █████╗
⣿⣿⣿⡏⠁⠀⠀⠀⠀⠀⠀⢀⣰⣶⣶⡄⠀⠀⠀⠀⠀⠀⢀⠀⠀⠈⢻        ████╗  ██║ ██╔═══██╗ ██║   ██║ ██╔══██╗
⣿⣿⣿⠁⠄⠀⠀⠀⠀⠀⣤⣾⣿⣿⣿⣿⡂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠽       ██╔██╗ ██║ ██║   ██║ ██║   ██║ ███████║
⣿⣿⡏⣸⠀⠀⠀⠀⢀⣼⣿⣿⣿⣿⣿⣿⣿⡆⠀⠀⠈⠀⠀⠀⠀⠀⠀⠰      ██║╚██╗██║ ██║   ██║ ╚██╗ ██╔╝ ██╔══██║
⣿⣿⡇⠁⠀⠀⠀⣤⣍⣙⣿⣿⣏⣠⠄⠲⠲⠦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻     ██║ ╚████║ ╚██████╔╝  ╚████╔╝  ██║  ██║
⣿⣿⠁⠀⠀⠀⠀⠀⢤⠙⣿⣿⣿⣇⣀⡐⢂⣠⡄⠠⠀⠀⠀⠀⠀⠀⡀⢠⢸     ╚═╝  ╚═══╝  ╚═════╝    ╚═══╝   ╚═╝  ╚═╝
⣿⣿⠀⠀⠐⠀⣶⣷⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠐⠈⠀⠀⠀⠉⠘⣼
⣿⣿⠀⠈⠀⠀⣿⣿⣿⡿⣿⠿⢿⣿⣿⣿⣿⣿⣿⣧⡀⠀⢄⠲⠀⠀⠀⣱      ~ Five advisors. One question.
⣿⣿⡆⠀⠀⠀⠈⣿⣿⣷⣶⣼⣾⣿⣿⣿⣿⣿⣿⣿⣷⠂⠀⠀⠂⢀⢲         Independent answers. One vote each.
⣿⣿⣿⡆⠀⠀⠀⠙⣿⠋⠠⠄⢀⠉⣹⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⣿          The majority decides.
⣿⣿⣿⣿⣦⠀⠀⠀⠘⣿⣤⣤⣶⣿⣿⣿⣿⣿⠟⣛⡽⠀⠀⠀⠠⣸            ♥︎ NOVA ~
⣿⣿⣿⣿⣿⣷⡀⠀⠀⠈⠻⣿⣿⣿⠿⠛⠋⠐⠚⠛⠃   ⣰⣿</pre>
  <div class="bootline"><span class="ok">✓</span> <span id="boot"></span></div>
</div>
<header>
  <span class="mark">◆ NOVA COUNCIL</span>
  <span class="mode">council</span>
  <span id="run-badge" class="run-badge">● convening</span>
  <span class="sid" id="sid"></span>
  <button id="new-thread" title="Clear the prior session and start fresh">⟲ New session</button>
</header>
<div id="bench"></div>
<div id="messages">
  <div class="welcome">
    <h2>The Council Awaits</h2>
    <div class="divider"></div>
    <p>Pose a question. Five advisors each answer it <b>independently</b>, then cast a single <b>democratic vote</b> for the best answer. The <b>majority</b> choice becomes the verdict. Follow-ups remember the session; <b>New session</b> starts fresh.</p>
    <div class="seats">
      <span class="seat">🏛️ Architect</span>
      <span class="seat">🛠️ Pragmatist</span>
      <span class="seat">🔍 Skeptic</span>
      <span class="seat">🚀 Innovator</span>
      <span class="seat">✂️ Minimalist</span>
    </div>
  </div>
</div>
<div id="input-area">
  <span class="mode-badge">council</span>
  <span class="prefix">&gt;</span>
  <textarea id="input" rows="1" placeholder="Pose a question to the council…" autofocus></textarea>
  <button id="send-btn">Convene</button>
</div>
<div id="status-bar">
  <span id="conn" class="dot off">○ ready</span>
  <span id="stat-run" class="dot off">○ idle</span>
  <span class="spacer"></span>
  <span class="hint">⏎ send · ⇧⏎ newline</span>
  <span id="clock"></span>
</div>
<div class="scanlines"></div>
<div class="vignette"></div>

<script>
const $ = id => document.getElementById(id);
const messages = $('messages');
const input = $('input');
const sendBtn = $('send-btn');
const statusBar = $('status-bar');
const bench = $('bench');
let es = null;

const agentEls = {};    // id -> {wrap, body, buf}
const benchSeats = {};  // id -> {seat, badge, votes}
let agentMeta = {};     // id -> {name, avatar, color}

const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function setStatus(m) { statusBar.textContent = m; }
function setRun(on) {
  $('run-badge').classList.toggle('show', on);
  $('stat-run').textContent = on ? '● running' : '○ idle';
  $('stat-run').className = 'dot ' + (on ? 'on' : 'off');
}
function atBottom() { return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100; }
function scrollDown(force) { if (force || atBottom()) messages.scrollTop = messages.scrollHeight; }
function dropWelcome() { const w = messages.querySelector('.welcome'); if (w) w.remove(); }
function highlight(el) { el.querySelectorAll('pre code').forEach(b => hljs.highlightElement(b)); }
function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }

function addError(text) {
  dropWelcome();
  const e = el('div', 'error-msg'); e.textContent = text;
  messages.appendChild(e); scrollDown(true);
}
function addPhase(text, sub) {
  const e = el('div', 'phase');
  const t = el('span', 'phase-title'); t.textContent = text; e.appendChild(t);
  if (sub) { const s = el('span', 'phase-sub'); s.textContent = sub; e.appendChild(s); }
  messages.appendChild(e); scrollDown(true);
}

/* --- matrix rain (theme-tinted, like the TUI home screen) --- */
(function(){
  const cv = document.getElementById('rain');
  if (!cv || !cv.getContext || REDUCED) return;
  const cx = cv.getContext('2d');
  const KATA = 'ｱｲｳｴｵｶｷｸｹｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜｦﾝ';
  const FS = 15;
  let cols = 0, drops = [];
  function resize(){
    cv.width = cv.clientWidth; cv.height = cv.clientHeight;
    cols = Math.max(1, Math.ceil(cv.width / FS));
    drops = Array.from({length: cols}, () => Math.floor(Math.random() * -40));
  }
  function frame(){
    cx.fillStyle = 'rgba(19,20,29,0.10)';
    cx.fillRect(0, 0, cv.width, cv.height);
    cx.font = FS + 'px "JetBrains Mono", monospace';
    for (let i = 0; i < cols; i++){
      const ch = KATA[Math.floor(Math.random() * KATA.length)];
      const y = drops[i] * FS;
      cx.fillStyle = 'rgba(122,162,247,0.9)';
      cx.fillText(ch, i * FS, y);
      cx.fillStyle = 'rgba(122,162,247,0.22)';
      cx.fillText(ch, i * FS, y - FS);
      if (y > cv.height + 40 && Math.random() > 0.975) drops[i] = Math.floor(Math.random() * -20);
      drops[i]++;
    }
    requestAnimationFrame(frame);
  }
  window.addEventListener('resize', resize);
  resize();
  frame();
})();

/* --- boot line typewriter --- */
(function(){
  const el = document.getElementById('boot');
  if (!el) return;
  const text = 'council chamber online · five advisors · one verdict';
  if (REDUCED){ el.textContent = text; return; }
  let i = 0;
  (function type(){
    el.textContent = text.slice(0, i);
    if (i < text.length){ el.textContent += '▊'; i++; setTimeout(type, 16); }
  })();
})();

/* --- clock --- */
(function(){
  const c = document.getElementById('clock');
  if (!c) return;
  function tick(){ c.textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
  tick(); setInterval(tick, 10000);
})();

/* --- the bench: the row of advisor seats at the top of the chamber --- */
function buildBench(agents) {
  bench.innerHTML = '';
  for (const k in benchSeats) delete benchSeats[k];
  agents.forEach(a => {
    agentMeta[a.id] = a;
    const seat = el('div', 'seat'); seat.dataset.state = 'idle';
    seat.style.setProperty('--seat', a.color || 'var(--primary)');
    const av = el('div', 'seat-av'); av.textContent = a.avatar || '•';
    const badge = el('div', 'seat-badge'); badge.textContent = '0'; badge.title = 'votes';
    av.appendChild(badge);
    const nm = el('div', 'seat-name'); nm.textContent = a.name || a.id;
    const st = el('div', 'seat-state'); st.textContent = 'waiting';
    seat.appendChild(av); seat.appendChild(nm); seat.appendChild(st);
    bench.appendChild(seat);
    benchSeats[a.id] = { seat, badge, state: st, votes: 0 };
  });
  bench.classList.add('active');
}
function seatState(id, state, label) {
  const s = benchSeats[id]; if (!s) return;
  s.seat.dataset.state = state;
  if (label) s.state.textContent = label;
}
function bumpSeatVote(id) {
  const s = benchSeats[id]; if (!s) return;
  s.votes += 1; s.badge.textContent = s.votes; s.badge.classList.add('has');
}

function startAgent(meta) {
  dropWelcome();
  const wrap = el('div', 'agent streaming');
  wrap.style.setProperty('--seat', meta.color || 'var(--primary)');
  const who = el('div', 'who');
  const av = el('span', 'avatar'); av.textContent = meta.avatar || '•';
  const nm = el('span'); nm.textContent = meta.name || meta.id;
  const tag = el('span', 'who-tag'); tag.textContent = 'independent answer';
  who.appendChild(av); who.appendChild(nm); who.appendChild(tag);
  const body = el('div', 'body');
  wrap.appendChild(who); wrap.appendChild(body);
  messages.appendChild(wrap);
  agentEls[meta.id] = { wrap, body, buf: '' };
  seatState(meta.id, 'speaking', 'answering…');
  scrollDown(true);
}

function deltaAgent(id, text) {
  const a = agentEls[id]; if (!a) return;
  a.buf += text;
  a.body.innerHTML = marked.parse(a.buf);
  highlight(a.body);
  scrollDown();
}

function finishAgent(id, text) {
  const a = agentEls[id]; if (!a) return;
  a.wrap.classList.remove('streaming');
  if (text) { a.body.innerHTML = marked.parse(text); highlight(a.body); }
  seatState(id, 'answered', 'answered');
  scrollDown();
}

function agentSearch(id, query) {
  const a = agentEls[id]; if (!a) return;
  const chip = el('div', 'search-chip');
  chip.textContent = '🔍 searched: ' + (query || '…');
  a.wrap.insertBefore(chip, a.body);
  scrollDown();
}

/* Each member casts ONE vote for the best (non-self) answer. */
function renderVote(ev) {
  const vm = agentMeta[ev.voter] || {};
  const cm = ev.choice ? (agentMeta[ev.choice] || {}) : {};
  const row = el('div', 'vote-row');
  row.style.setProperty('--seat', vm.color || 'var(--primary)');
  const voter = el('span', 'v-voter');
  voter.textContent = (vm.avatar ? vm.avatar + ' ' : '') + (ev.voter_name || ev.voter);
  const arrow = el('span', 'v-arrow'); arrow.textContent = '→';
  const choice = el('span', 'v-choice');
  if (ev.choice) {
    choice.style.color = cm.color || 'var(--fg)';
    choice.textContent = (cm.avatar ? cm.avatar + ' ' : '') + (ev.choice_name || '');
    bumpSeatVote(ev.choice);
  } else {
    choice.textContent = '(abstained)';
    choice.classList.add('muted');
  }
  row.appendChild(voter); row.appendChild(arrow); row.appendChild(choice);
  if (ev.reason) { const why = el('div', 'v-why'); why.textContent = ev.reason; row.appendChild(why); }
  messages.appendChild(row);
  scrollDown();
}

function renderVerdict(ev) {
  const wm = agentMeta[ev.winner_id] || {};
  const card = el('div', 'verdict');
  card.style.setProperty('--seat', wm.color || 'var(--warning)');
  if (ev.winner_id && benchSeats[ev.winner_id]) {
    seatState(ev.winner_id, 'winner', 'chosen');
  }

  const h = el('h3');
  h.innerHTML = '👑 The council chose ' +
    '<span class="w-name">' + (wm.avatar ? wm.avatar + ' ' : '') +
    (ev.winner_name || '') + '</span>';
  card.appendChild(h);

  const sub = el('div', 'verdict-sub');
  sub.textContent = 'by majority vote (' + (ev.tally ? (ev.tally[ev.winner_id] || 0) : 0) +
    ' of ' + (ev.votes || 0) + ' votes)';
  card.appendChild(sub);

  const tally = ev.tally || {};
  const max = Math.max(1, ...Object.values(tally));
  const board = el('div', 'leaderboard');
  Object.keys(tally).sort((a, b) => tally[b] - tally[a]).forEach(id => {
    const m = agentMeta[id] || {};
    const r = el('div', 'lb-row' + (id === ev.winner_id ? ' win' : ''));
    r.style.setProperty('--seat', m.color || 'var(--primary)');
    const name = el('div', 'lb-name'); name.textContent = (m.avatar ? m.avatar + ' ' : '') + (m.name || id);
    const bar = el('div', 'lb-bar'); const fill = el('div', 'lb-fill');
    fill.style.width = Math.round((tally[id] / max) * 100) + '%';
    bar.appendChild(fill);
    const pts = el('div', 'lb-pts'); pts.textContent = tally[id] + (tally[id] === 1 ? ' vote' : ' votes');
    r.appendChild(name); r.appendChild(bar); r.appendChild(pts);
    board.appendChild(r);
  });
  card.appendChild(board);

  if (ev.answer) {
    const lbl = el('div', 'winner-label'); lbl.textContent = 'Winning answer';
    const ans = el('div', 'winner-answer');
    ans.innerHTML = marked.parse(ev.answer); highlight(ans);
    card.appendChild(lbl); card.appendChild(ans);
  }
  messages.appendChild(card);
  scrollDown(true);
}

function endRun() {
  if (es) { es.close(); es = null; }
  input.disabled = false; sendBtn.disabled = false; input.focus();
  setRun(false);
}

function convene() {
  const topic = input.value.trim();
  if (!topic || es) return;
  input.value = ''; input.style.height = 'auto';
  input.disabled = true; sendBtn.disabled = true;
  setRun(true);

  for (const k in agentEls) delete agentEls[k];
  agentMeta = {};

  dropWelcome();
  const banner = el('div', 'topic-banner'); banner.textContent = '“' + topic + '”';
  messages.appendChild(banner);
  setStatus('convening the council…'); scrollDown(true);

  es = new EventSource('/api/council?topic=' + encodeURIComponent(topic));

  es.addEventListener('council_start', e => {
    const d = JSON.parse(e.data);
    buildBench(d.agents || []);
    setStatus('the council deliberates independently…');
  });
  es.addEventListener('agent_start', e => {
    const d = JSON.parse(e.data);
    agentMeta[d.id] = d; startAgent(d);
    setStatus((d.name || 'an advisor') + ' is forming an answer…');
  });
  es.addEventListener('agent_delta', e => { const d = JSON.parse(e.data); deltaAgent(d.id, d.text); });
  es.addEventListener('agent_tool', e => { const d = JSON.parse(e.data); agentSearch(d.id, d.query); });
  es.addEventListener('agent_done', e => { const d = JSON.parse(e.data); finishAgent(d.id, d.text); });
  es.addEventListener('vote_start', e => {
    addPhase('The Vote', 'each advisor casts one vote for the best answer');
    Object.keys(benchSeats).forEach(id => seatState(id, 'voting', 'voting…'));
    setStatus('the council is voting…');
  });
  es.addEventListener('vote', e => { renderVote(JSON.parse(e.data)); });
  es.addEventListener('verdict', e => { addPhase('The Verdict'); renderVerdict(JSON.parse(e.data)); });
  es.addEventListener('council_error', e => {
    let m = 'The council failed.';
    try { m = JSON.parse(e.data).message || m; } catch (_) {}
    addError(m); setStatus('error');
    // endRun(), not setRun(false): an error has to close the stream and
    // re-enable the composer exactly like `done` does. Leaving `es` non-null
    // made convene() return immediately on every later send — the input stayed
    // disabled and follow-up questions silently did nothing, with no clue why.
    endRun();
  });
  es.addEventListener('done', e => { setStatus('the council has spoken'); endRun(); });
  es.onerror = () => {
    if (!es || es.readyState === EventSource.CLOSED) {
      setStatus('connection closed'); endRun();
    }
  };
}

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); convene(); }
});
sendBtn.addEventListener('click', convene);

const newThreadBtn = $('new-thread');
function newThread() {
  if (es) return;  // don't reset mid-run
  fetch('/api/council/reset').catch(() => {});
  messages.innerHTML = '';
  bench.innerHTML = ''; bench.classList.remove('active');
  for (const k in agentEls) delete agentEls[k];
  for (const k in benchSeats) delete benchSeats[k];
  agentMeta = {};
  const w = el('div', 'welcome');
  w.innerHTML = '<h2>Fresh Council</h2><div class="divider"></div>' +
    '<p>Prior session cleared. Pose a new question to convene the council.</p>';
  messages.appendChild(w);
  setStatus('ready'); setRun(false);
  input.focus();
}
newThreadBtn.addEventListener('click', newThread);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _ChatHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the council server."""

    def log_message(self, format, *args):  # noqa: A002 - signature from stdlib
        """Silence the default stderr logging; we use Nova's console."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_html()
        elif parsed.path == "/api/council":
            qs = parse_qs(parsed.query)
            topic = (qs.get("topic") or [""])[0]
            self._stream_council(topic)
        elif parsed.path == "/api/council/reset":
            self._reset_history()
        else:
            self.send_response(404)
            self.end_headers()

    def _reset_history(self) -> None:
        """Clear the council's cross-round history (start a fresh thread)."""
        global _council_history
        with _run_lock:
            _council_history = []
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _serve_html(self):
        html = _make_chat_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(html.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---- SSE council stream ----

    def _stream_council(self, topic: str) -> None:
        """Run a council over *topic* and stream it to the browser as SSE."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        loop = _main_loop
        if loop is None or not loop.is_running():
            self._sse("council_error", {"message": "Agent event loop is not running"})
            self._sse("done", {})
            return
        if not topic.strip():
            self._sse("council_error", {"message": "A topic is required"})
            self._sse("done", {})
            return
        if not _run_lock.acquire(blocking=False):
            self._sse("council_error", {"message": "The council is already in session"})
            self._sse("done", {})
            return

        try:
            self._pump_council(topic, loop)
        finally:
            _run_lock.release()

    def _pump_council(self, topic: str, loop: asyncio.AbstractEventLoop) -> None:
        """Bridge the async council generator (on *loop*) to this thread as SSE.

        Carries prior rounds in so the advisors can follow up, and records this
        round into the shared history once it completes.
        """
        q: queue.Queue[Any] = queue.Queue()
        history = list(_council_history)  # snapshot for this run

        async def _produce() -> None:
            from novacode_cli.council import get_council_model, run_council

            try:
                model = get_council_model()
            except Exception as exc:  # noqa: BLE001
                q.put(("council_error", {"message": f"Model unavailable: {exc}"}))
                q.put(_STREAM_END)
                return

            try:
                async for event in run_council(topic, model, history=history):
                    etype = event.pop("type")
                    q.put((etype, event))
            except Exception as exc:  # noqa: BLE001
                q.put(("council_error", {"message": str(exc)}))
            finally:
                q.put(_STREAM_END)

        producer = asyncio.run_coroutine_threadsafe(_produce(), loop)

        # Accumulate this round from the event stream so we can append it to the
        # shared history once it completes cleanly.
        id_to_name: dict[str, str] = {}
        texts: dict[str, str] = {}
        order: list[str] = []
        winner_name: str | None = None
        completed = False

        while True:
            try:
                item = q.get(timeout=300)
            except queue.Empty:
                self._sse("council_error", {"message": "The council timed out"})
                producer.cancel()
                break

            if item is _STREAM_END:
                # Record the round before signalling done, so a follow-up request
                # (or a test) observing the terminal event sees it persisted.
                if completed and texts:
                    transcript = [[id_to_name.get(i, i), texts[i]] for i in order if i in texts]
                    _council_history.append(
                        {"topic": topic, "transcript": transcript, "winner": winner_name}
                    )
                    # Bounded: the prompt only carries the last few rounds
                    # (council._MAX_HISTORY_ROUNDS), but the list itself grew
                    # forever — a long-lived server accumulated every full
                    # transcript it had ever produced. Keep a small margin over
                    # what the prompt uses and drop the rest.
                    if len(_council_history) > _MAX_KEPT_ROUNDS:
                        del _council_history[:-_MAX_KEPT_ROUNDS]
                self._sse("done", {})
                break

            etype, data = item
            if etype == "council_start":
                for a in data.get("agents", []):
                    id_to_name[a["id"]] = a["name"]
                    order.append(a["id"])
            elif etype == "agent_done":
                texts[data["id"]] = data.get("text", "")
            elif etype == "verdict":
                winner_name = data.get("winner_name")
                completed = True

            if not self._sse(etype, data):
                producer.cancel()  # client disconnected
                break

    def _sse(self, event: str, data: dict) -> bool:
        """Write one SSE frame. Returns False if the client has disconnected."""
        try:
            frame = f"event: {event}\ndata: {json.dumps(data)}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server that doesn't dump a traceback when a client disconnects.

    Browsers (and the SSE client) routinely drop the connection mid-stream — on
    ``done``, on navigation, or on reset — which surfaces as a connection error
    in the handler thread. That's expected, not a server fault, so we swallow it.
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001, ARG002
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionError, OSError)):
            return
        logger.exception("Council server error handling %s", client_address)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _port_is_answered(port: int, *, timeout: float = 0.35) -> bool:
    """True if something already answers connections on *port*.

    A successful ``bind("localhost", port)`` is NOT proof the port is usable.
    When another process holds ``0.0.0.0:port`` (Docker Desktop's proxy is the
    common case, and WSL's relay behaves the same way), binding the narrower
    ``localhost`` address still succeeds, but connections are answered by that
    process — so /council opened to somebody else's 404
    (``{"detail":"Not Found"}``) instead of Nova's page.

    Connecting is the only honest test: if the connect succeeds, someone is
    listening and the port is not ours to take.
    """
    for family, addr in (
        (socket.AF_INET, ("127.0.0.1", port)),
        (socket.AF_INET6, ("::1", port)),
    ):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex(addr) == 0:
                    return True
        except OSError:
            continue  # e.g. no IPv6 stack — not evidence of a squatter
    return False


def _find_available_port(start_port: int = 8000, max_attempts: int = 100) -> int:
    """Find a port that is both bindable AND not already being answered."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
            except OSError:
                continue
        # Bindable is necessary but not sufficient — see _port_is_answered.
        if _port_is_answered(port):
            continue
        return port
    raise RuntimeError(
        f"No available ports in range {start_port}-{start_port + max_attempts}"
    )


# ---------------------------------------------------------------------------
# Server lifecycle (shared by CLI handler and TUI)
# ---------------------------------------------------------------------------

def want_restart() -> bool:
    """Return True if the server should restart (state reset requested)."""
    return False


def is_server_running() -> bool:
    """Check if the council server is currently running."""
    return _server is not None


def get_server_url() -> str | None:
    """Return the URL of the running server, or None."""
    if _server_port is None:
        return None
    return f"http://localhost:{_server_port}"


def set_agent_refs(
    agent: Any,
    assistant_id: str | None,
    session_state: Any,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Store session references for the background HTTP server thread.

    The council itself only needs the event ``loop`` (to run model calls), but we
    keep the agent/session references for parity with the CLI/TUI call sites.
    """
    global _agent, _assistant_id, _session_state, _main_loop
    _agent = agent
    _assistant_id = assistant_id
    _session_state = session_state
    _main_loop = loop


def start_chat_server() -> str:
    """Start the HTTP council server in a daemon thread.

    Must have called :func:`set_agent_refs` first.

    Returns:
        The URL the server is listening on.

    Raises:
        RuntimeError: If no available port is found.
    """
    global _server, _server_thread, _server_port

    if _server is not None:
        assert _server_port is not None
        return f"http://localhost:{_server_port}"

    port = _find_available_port(8000)
    _server_port = port

    _server = _QuietThreadingHTTPServer(("localhost", port), _ChatHandler)
    _server_thread = threading.Thread(
        target=_server.serve_forever, daemon=True, name="chat-http-server"
    )
    _server_thread.start()

    return f"http://localhost:{port}"


def stop_chat_server() -> bool:
    """Stop the running council server.

    Returns:
        True if the server was stopped, False if it wasn't running.
    """
    global _server, _server_thread, _server_port, _council_history

    if _server is None:
        return False

    _server.shutdown()
    _server.server_close()
    _server = None
    _server_thread = None
    _server_port = None
    _council_history = []
    return True


# ---------------------------------------------------------------------------
# CLI command handler
# ---------------------------------------------------------------------------

async def handle_chat_command() -> bool:
    """Handle the /chat command — start the council web UI (CLI entry point)."""
    if is_server_running():
        url = get_server_url()
        assert url is not None
        console.print()
        console.print(f"[green]Council UI already running at [bold]{url}[/bold][/green]")
        webbrowser.open(url)
        return True

    url = start_chat_server()
    console.print()
    console.print(f"[bold {COLORS['primary']}]== Nova Council ==[/bold {COLORS['primary']}]")
    console.print(f"[green]Council UI started at [bold]{url}[/bold][/green]")
    webbrowser.open(url)
    console.print("[dim]  Type /chat stop to stop the server.[/dim]")
    console.print()
    return True


async def handle_chat_stop_command() -> bool:
    """Handle the /chat stop command — stop the council web UI (CLI entry)."""
    if not is_server_running():
        console.print("[yellow]Council server is not running.[/yellow]")
        return True

    console.print("[dim]Shutting down council server...[/dim]")
    stop_chat_server()
    console.print("[green]Council server stopped.[/green]")
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def register_commands(registry) -> None:
    """Register the /council command."""
    from novacode_cli.commands import CommandContext

    async def _handle(ctx: CommandContext) -> bool:
        set_agent_refs(
            ctx.agent,
            ctx.assistant_id,
            ctx.session_state,
            asyncio.get_running_loop(),
        )

        args = (ctx.cmd_args or "").strip()
        if args == "stop":
            return await handle_chat_stop_command()
        return await handle_chat_command()

    registry.register("council", _handle)
