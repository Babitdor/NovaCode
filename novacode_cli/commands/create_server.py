"""HTTP server, REST API, and self-contained HTML page for the Skills & Agents web UI.

The CreateServer class starts a lightweight HTTP server (stdlib http.server)
in a background daemon thread, serving a single-page app with two tabs:
Skills and Agents. Users can browse, preview, edit, create, and delete
skills (SKILL.md) and agents (agent.md) across global and project scopes.

REST API endpoints:
    GET  /api/skills              — list all skills
    GET  /api/skills/<name>       — get SKILL.md content
    PUT  /api/skills/<name>       — update SKILL.md content
    POST /api/skills              — create a new skill
    DELETE /api/skills/<name>     — delete a skill
    GET  /api/agents              — list all agents
    GET  /api/agents/<name>       — get agent.md content
    PUT  /api/agents/<name>       — update agent.md content
    POST /api/agents              — create a new agent
    DELETE /api/agents/<name>     — delete an agent
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
from http.server import HTTPServer
from typing import Any

from novacode_cli.config.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_frontmatter_field(content: str, field: str) -> str | None:
    """Extract a YAML frontmatter field value from markdown content."""
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = parts[1]
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def _ensure_frontmatter(content: str, name: str, description: str | None = None) -> str:
    """Ensure content has YAML frontmatter with at least name and description."""
    if content.startswith("---"):
        # Already has frontmatter — ensure name field
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2]
            if not re.search(r"^name:\s*", frontmatter, re.MULTILINE):
                frontmatter = f"name: {name}\n{frontmatter}"
            if description and not re.search(r"^description:\s*", frontmatter, re.MULTILINE):
                frontmatter = f"{frontmatter}description: {description}\n"
            return f"---{frontmatter}---{body}"
    # No frontmatter — add one
    desc_line = f"\ndescription: {description}" if description else ""
    return f"---\nname: {name}{desc_line}\n---\n\n{content}"


def _is_valid_name(name: str) -> bool:
    """Validate a skill or agent name (alphanumeric, hyphens, underscores)."""
    return bool(name and re.match(r"^[a-zA-Z0-9_\-]+$", name))


# ---------------------------------------------------------------------------
# HTML Page
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nova Create — Skills & Agents</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@500;600;700;800&family=DM+Sans:ital,opsz,wght@0,9..40,300..700;1,9..40,300..700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    /* ── Nova TUI (tokyo-night) palette ─────────────────────── */
    --bg-deep: #13141d;        /* background */
    --bg-surface: #1a1b26;     /* surface */
    --bg-elevated: #24283b;    /* panel */
    --bg-hover: #2f3346;       /* boost */
    --border-subtle: #3b4261;  /* border */
    --border-mid: #565f89;     /* text-muted */
    --text-primary: #c0caf5;   /* foreground */
    --text-secondary: #a9b1d6;
    --text-muted: #565f89;
    --primary: #7aa2f7;        /* blue */
    --primary-dim: rgba(122, 162, 247, 0.12);
    --primary-glow: rgba(122, 162, 247, 0.25);
    --accent: #bb9af7;         /* purple */
    --accent-dim: rgba(187, 154, 247, 0.12);
    --accent-glow: rgba(187, 154, 247, 0.2);
    --secondary: #9ece6a;      /* green */
    --success: #73daca;        /* teal */
    --success-dim: rgba(115, 218, 202, 0.12);
    --success-glow: rgba(115, 218, 202, 0.2);
    --warning: #e0af68;        /* orange */
    --error: #f7768e;          /* red */
    --error-dim: rgba(247, 118, 142, 0.12);
    --radius: 14px;
    --radius-sm: 8px;
    --radius-xs: 6px;
  }

  html, body { height: 100%; }

  body {
    font-family: 'JetBrains Mono', 'DM Sans', system-ui, sans-serif;
    background: var(--bg-deep);
    color: var(--text-primary);
    display: flex;
    flex-direction: column;
    position: relative;
    overflow: hidden;
  }

  /* ── Background: tokyo-night navy with subtle blue/purple flares ── */

  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse 90% 50% at 55% -10%, rgba(122, 162, 247, 0.08), transparent 70%),
      radial-gradient(ellipse 60% 40% at 30% 90%, rgba(115, 218, 202, 0.05), transparent 60%),
      radial-gradient(ellipse 50% 50% at 80% 20%, rgba(187, 154, 247, 0.05), transparent 50%);
    pointer-events: none;
    z-index: 0;
  }

  /* Matrix-rain grid — the TUI's signature, rendered as a faint falling grid */
  body::after {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(122, 162, 247, 0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(122, 162, 247, 0.03) 1px, transparent 1px);
    background-size: 48px 48px;
    pointer-events: none;
    z-index: 0;
  }

  .grain {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 1;
    opacity: 0.03;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }

  /* ── Floating ambient particles (tinted tokyo-night) ──────── */

  .particle-field {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 1;
    overflow: hidden;
  }

  .particle {
    position: absolute;
    border-radius: 50%;
    opacity: 0;
    animation: particle-float 12s ease-in-out infinite;
  }

  .particle:nth-child(1) { width: 3px; height: 3px; background: var(--primary); left: 12%; top: 20%; animation-delay: 0s; animation-duration: 14s; }
  .particle:nth-child(2) { width: 2px; height: 2px; background: var(--success); left: 28%; top: 65%; animation-delay: 2s; animation-duration: 11s; }
  .particle:nth-child(3) { width: 4px; height: 4px; background: var(--accent); left: 55%; top: 15%; animation-delay: 4s; animation-duration: 16s; }
  .particle:nth-child(4) { width: 2px; height: 2px; background: var(--primary); left: 72%; top: 50%; animation-delay: 1s; animation-duration: 13s; }
  .particle:nth-child(5) { width: 3px; height: 3px; background: var(--success); left: 88%; top: 30%; animation-delay: 3s; animation-duration: 15s; }
  .particle:nth-child(6) { width: 2px; height: 2px; background: var(--accent); left: 42%; top: 80%; animation-delay: 5s; animation-duration: 12s; }

  @keyframes particle-float {
    0% { opacity: 0; transform: translateY(0) scale(0); }
    10% { opacity: 0.8; transform: translateY(-10px) scale(1); }
    40% { opacity: 0.6; transform: translateY(-30px) scale(0.9); }
    70% { opacity: 0.3; transform: translateY(-50px) scale(0.6); }
    100% { opacity: 0; transform: translateY(-80px) scale(0); }
  }

  /* ── Header ─────────────────────────────────────────── */

  header {
    position: relative;
    z-index: 2;
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border-subtle);
    background: rgba(19, 20, 29, 0.7);
    backdrop-filter: blur(12px);
  }

  .header-left { display: flex; align-items: center; gap: 16px; }

  .logo-icon {
    width: 38px; height: 38px;
    border-radius: 12px;
    background: linear-gradient(145deg, var(--accent), var(--primary));
    display: flex; align-items: center; justify-content: center;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800; font-size: 20px; color: #13141d;
    box-shadow: 0 0 20px var(--primary-glow);
    position: relative;
  }

  .logo-icon::after {
    content: '';
    position: absolute;
    inset: -2px;
    border-radius: 14px;
    background: linear-gradient(145deg, var(--primary-glow), transparent);
    z-index: -1;
  }

  header h1 {
    font-family: 'JetBrains Mono', monospace;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.3px;
    color: var(--text-primary);
    position: relative;
  }

  header h1::after {
    content: '▮';
    font-size: 10px;
    margin-left: 8px;
    color: var(--primary);
    vertical-align: super;
    opacity: 0.6;
  }

  header .subtitle {
    font-size: 12px;
    color: var(--text-muted);
    font-weight: 400;
    letter-spacing: 0.2px;
  }

  .header-right { display: flex; align-items: center; gap: 12px; }

  .server-badge {
    display: flex; align-items: center; gap: 7px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; font-weight: 500;
    padding: 5px 12px;
    border-radius: 20px;
    background: var(--success-dim);
    color: var(--success);
    border: 1px solid rgba(115, 218, 202, 0.15);
    letter-spacing: 0.5px;
    text-transform: uppercase;
    position: relative;
    overflow: hidden;
  }

  .server-badge::before {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, transparent, rgba(115, 218, 202, 0.05), transparent);
    animation: badge-shimmer 3s ease-in-out infinite;
  }

  @keyframes badge-shimmer {
    0%, 100% { transform: translateX(-100%); }
    50% { transform: translateX(100%); }
  }

  .live-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--success);
    box-shadow: 0 0 8px var(--success-glow);
    animation: pulse-dot 1.5s ease-in-out infinite;
  }

  @keyframes pulse-dot {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.7); }
  }

  /* ── Layout ─────────────────────────────────────────── */

  .app-container {
    position: relative;
    z-index: 2;
    display: flex;
    flex: 1;
    min-height: 0;
  }

  /* ── Sidebar ─────────────────────────────────────────── */

  .sidebar {
    width: 300px;
    min-width: 300px;
    display: flex;
    flex-direction: column;
    background: rgba(26, 27, 38, 0.6);
    backdrop-filter: blur(4px);
    border-right: 1px solid var(--border-subtle);
  }

  .tab-bar {
    display: flex;
    gap: 2px;
    padding: 10px 10px 0;
    flex-shrink: 0;
  }

  .tab-btn {
    flex: 1;
    padding: 10px 12px;
    border: none;
    border-radius: var(--radius-xs) var(--radius-xs) 0 0;
    background: transparent;
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.25s ease;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    position: relative;
  }

  .tab-btn:hover { color: var(--text-secondary); background: rgba(36, 40, 59, 0.5); }
  .tab-btn.active {
    color: var(--primary);
    background: rgba(36, 40, 59, 0.8);
  }
  .tab-btn.active::after {
    content: '';
    position: absolute;
    bottom: 0; left: 15%; right: 15%;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--primary), transparent);
    border-radius: 2px;
  }

  .tab-btn .count {
    display: inline-block;
    margin-left: 5px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--bg-elevated);
    color: var(--text-muted);
  }

  .tab-btn.active .count {
    background: var(--primary-dim);
    color: var(--primary);
  }

  .sidebar-search {
    padding: 8px 10px;
    flex-shrink: 0;
  }

  .sidebar-search input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-xs);
    background: var(--bg-deep);
    color: var(--text-secondary);
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    outline: none;
    transition: all 0.2s ease;
  }

  .sidebar-search input:focus {
    border-color: var(--primary);
    box-shadow: 0 0 0 2px var(--primary-glow);
    color: var(--text-primary);
  }

  .sidebar-search input::placeholder { color: var(--text-muted); }

  .item-list {
    flex: 1;
    overflow-y: auto;
    padding: 6px 8px 8px;
  }

  .item-list::-webkit-scrollbar { width: 3px; }
  .item-list::-webkit-scrollbar-track { background: transparent; }
  .item-list::-webkit-scrollbar-thumb { background: var(--border-mid); border-radius: 3px; }

  /* Accent-bar cards — the TUI's signature message-card style */
  .item-card {
    padding: 10px 12px;
    margin-bottom: 3px;
    border-radius: var(--radius-xs);
    cursor: pointer;
    transition: all 0.2s ease;
    border: 1px solid transparent;
    border-left: 3px solid var(--border-subtle);
    background: var(--bg-surface);
    opacity: 0;
    animation: card-slide-in 0.3s ease forwards;
  }

  @keyframes card-slide-in {
    from { opacity: 0; transform: translateX(-6px); }
    to { opacity: 1; transform: translateX(0); }
  }

  .item-card:nth-child(1) { animation-delay: 0.02s; }
  .item-card:nth-child(2) { animation-delay: 0.04s; }
  .item-card:nth-child(3) { animation-delay: 0.06s; }
  .item-card:nth-child(4) { animation-delay: 0.08s; }
  .item-card:nth-child(5) { animation-delay: 0.10s; }
  .item-card:nth-child(n+6) { animation-delay: 0.12s; }

  .item-card:hover {
    background: var(--bg-hover);
    border-color: var(--border-subtle);
    border-left-color: var(--primary);
    transform: translateX(2px);
  }

  .item-card.active {
    background: linear-gradient(135deg, var(--primary-dim), var(--accent-dim));
    border-color: rgba(122, 162, 247, 0.3);
    border-left-color: var(--primary);
    box-shadow: 0 0 16px var(--primary-glow);
  }

  .item-card .item-name {
    font-size: 13px;
    font-weight: 500;
    margin-bottom: 2px;
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--text-primary);
  }

  .item-card.active .item-name { color: var(--primary); }

  .item-card .item-desc {
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .source-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 8px;
    font-weight: 500;
    padding: 2px 5px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }

  .source-badge.global {
    background: var(--primary-dim);
    color: var(--primary);
    border: 1px solid rgba(122, 162, 247, 0.12);
  }
  .source-badge.project {
    background: var(--success-dim);
    color: var(--success);
    border: 1px solid rgba(115, 218, 202, 0.12);
  }
  .source-badge.claude {
    background: var(--accent-dim);
    color: var(--accent);
    border: 1px solid rgba(187, 154, 247, 0.15);
  }

  .empty-state {
    padding: 40px 20px;
    text-align: center;
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.6;
  }

  .empty-state .empty-icon {
    font-size: 28px;
    margin-bottom: 10px;
    opacity: 0.3;
    display: block;
  }

  /* ── Main Pane ───────────────────────────────────────── */

  .main-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: rgba(19, 20, 29, 0.3);
  }

  .pane-header {
    padding: 18px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border-subtle);
    background: rgba(26, 27, 38, 0.4);
    backdrop-filter: blur(4px);
  }

  .pane-header h2 {
    font-family: 'JetBrains Mono', monospace;
    font-size: 16px;
    font-weight: 600;
    letter-spacing: -0.2px;
  }

  .pane-header h2 .pane-source {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 400;
    color: var(--text-muted);
    margin-left: 8px;
    letter-spacing: 0;
  }

  .pane-actions { display: flex; gap: 6px; }

  .btn {
    padding: 7px 14px;
    border: none;
    border-radius: var(--radius-xs);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    letter-spacing: 0.3px;
    text-transform: uppercase;
    position: relative;
    overflow: hidden;
  }

  .btn::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(135deg, transparent 40%, rgba(255,255,255,0.06) 100%);
    pointer-events: none;
  }

  .btn-primary {
    background: linear-gradient(135deg, var(--accent), var(--primary));
    color: #13141d;
    font-weight: 700;
  }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 16px var(--primary-glow); }

  .btn-secondary {
    background: var(--bg-elevated);
    color: var(--text-secondary);
    border: 1px solid var(--border-mid);
  }
  .btn-secondary:hover { background: var(--bg-hover); border-color: var(--text-muted); color: var(--text-primary); }

  .btn-danger {
    background: var(--error-dim);
    color: var(--error);
    border: 1px solid rgba(247, 118, 142, 0.2);
  }
  .btn-danger:hover { background: rgba(247, 118, 142, 0.25); }

  .btn-success {
    background: linear-gradient(135deg, #0d9488, var(--success));
    color: #13141d;
  }
  .btn-success:hover { transform: translateY(-1px); box-shadow: 0 4px 16px var(--success-glow); }

  .btn-sm {
    padding: 5px 10px;
    font-size: 10px;
  }

  .pane-content {
    flex: 1;
    overflow-y: auto;
    padding: 28px;
  }

  .pane-content::-webkit-scrollbar { width: 4px; }
  .pane-content::-webkit-scrollbar-track { background: transparent; }
  .pane-content::-webkit-scrollbar-thumb { background: var(--border-mid); border-radius: 4px; }

  /* ── Preview ─────────────────────────────────────────── */

  .preview {
    font-size: 14px;
    line-height: 1.75;
    color: var(--text-primary);
    max-width: 800px;
    animation: fade-in 0.25s ease;
  }

  @keyframes fade-in {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .preview h1 {
    font-family: 'JetBrains Mono', monospace;
    font-size: 26px;
    font-weight: 700;
    margin: 0 0 16px;
    color: var(--primary);
    letter-spacing: -0.5px;
    line-height: 1.2;
  }

  .preview h2 {
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px;
    font-weight: 600;
    margin: 28px 0 12px;
    color: var(--text-primary);
    letter-spacing: -0.3px;
  }

  .preview h3 {
    font-size: 15px;
    font-weight: 600;
    margin: 22px 0 8px;
    color: var(--text-secondary);
  }

  .preview p { margin: 0 0 14px; }
  .preview a { color: var(--success); text-decoration: none; border-bottom: 1px solid rgba(115, 218, 202, 0.3); transition: border-color 0.2s; }
  .preview a:hover { border-bottom-color: var(--success); }

  .preview code {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    background: rgba(36, 40, 59, 0.8);
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid var(--border-subtle);
    color: var(--primary);
  }

  .preview pre {
    background: rgba(19, 20, 29, 0.8);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    padding: 18px;
    overflow-x: auto;
    margin: 16px 0;
    position: relative;
  }

  .preview pre::before {
    content: '```';
    position: absolute;
    top: 6px; right: 12px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: var(--text-muted);
    opacity: 0.4;
  }

  .preview pre code { background: none; border: none; padding: 0; color: var(--text-primary); }

  .preview ul, .preview ol { margin: 0 0 14px; padding-left: 24px; }
  .preview li { margin-bottom: 4px; }
  .preview li::marker { color: var(--primary); }

  .preview strong { color: var(--text-primary); font-weight: 600; }
  .preview hr { border: none; border-top: 1px solid var(--border-subtle); margin: 28px 0; }
  .preview blockquote {
    border-left: 3px solid var(--accent);
    padding: 10px 18px;
    margin: 16px 0;
    background: var(--accent-dim);
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
    color: var(--text-secondary);
    font-style: italic;
  }

  .preview table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 13px;
    border-radius: var(--radius-xs);
    overflow: hidden;
  }

  .preview th, .preview td {
    padding: 10px 14px;
    border: 1px solid var(--border-subtle);
    text-align: left;
  }

  .preview th {
    background: rgba(36, 40, 59, 0.8);
    font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    color: var(--text-secondary);
  }

  .preview tr:hover td { background: rgba(36, 40, 59, 0.4); }

  /* ── Editor ──────────────────────────────────────────── */

  .editor textarea {
    width: 100%;
    min-height: 400px;
    background: rgba(19, 20, 29, 0.8);
    border: 1px solid var(--border-mid);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    line-height: 1.7;
    padding: 18px;
    resize: vertical;
    outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }

  .editor textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-glow); }

  .editor .editor-actions {
    display: flex;
    gap: 8px;
    margin-top: 14px;
  }

  /* ── Create Form ──────────────────────────────────────── */

  .create-form { max-width: 640px; animation: fade-in 0.25s ease; }

  .form-group { margin-bottom: 18px; }

  .form-group label {
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }

  .form-group input, .form-group textarea, .form-group select {
    width: 100%;
    padding: 11px 14px;
    background: rgba(19, 20, 29, 0.8);
    border: 1px solid var(--border-mid);
    border-radius: var(--radius-xs);
    color: var(--text-primary);
    font-family: 'JetBrains Mono', monospace;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }

  .form-group input:focus, .form-group textarea:focus, .form-group select:focus {
    border-color: var(--primary);
    box-shadow: 0 0 0 3px var(--primary-glow);
  }

  .form-group textarea {
    min-height: 240px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    resize: vertical;
  }

  .form-group select {
    cursor: pointer;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23565f89' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 14px center;
    padding-right: 36px;
  }

  .form-actions { display: flex; gap: 8px; margin-top: 24px; }

  /* ── Toast / Notifications ───────────────────────────── */

  .toast {
    position: fixed;
    bottom: 28px;
    right: 28px;
    z-index: 100;
    padding: 14px 22px;
    border-radius: var(--radius-xs);
    font-size: 13px;
    font-weight: 500;
    animation: toast-in 0.35s ease;
    max-width: 420px;
    backdrop-filter: blur(12px);
    font-family: 'JetBrains Mono', monospace;
  }

  .toast.success {
    background: var(--success-dim);
    color: var(--success);
    border: 1px solid rgba(115, 218, 202, 0.2);
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
  }
  .toast.error {
    background: var(--error-dim);
    color: var(--error);
    border: 1px solid rgba(247, 118, 142, 0.2);
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
  }

  @keyframes toast-in {
    from { opacity: 0; transform: translateY(12px) scale(0.95); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* ── Loading ──────────────────────────────────────────── */

  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 60px;
    color: var(--text-muted);
    font-size: 13px;
    font-weight: 400;
  }

  .loading::after {
    content: '';
    width: 14px; height: 14px;
    margin-left: 10px;
    border: 2px solid var(--border-mid);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Responsive ──────────────────────────────────────── */

  @media (max-width: 768px) {
    .sidebar { width: 100%; min-width: 0; border-right: none; border-bottom: 1px solid var(--border-subtle); }
    .app-container { flex-direction: column; }
    header { flex-direction: column; align-items: flex-start; gap: 10px; }
    .pane-header { flex-direction: column; align-items: flex-start; gap: 10px; }
    .pane-actions { flex-wrap: wrap; }
  }
</style>
</head>
<body>
<div class="grain"></div>
<div class="particle-field">
  <div class="particle"></div>
  <div class="particle"></div>
  <div class="particle"></div>
  <div class="particle"></div>
  <div class="particle"></div>
  <div class="particle"></div>
</div>

<header>
  <div class="header-left">
    <div class="logo-icon">N</div>
    <div>
      <h1>Nova</h1>
      <span class="subtitle">Skills &amp; Agents &mdash; tokyo-night</span>
    </div>
  </div>
  <div class="header-right">
    <div class="server-badge">
      <span class="live-dot"></span>
      LIVE
    </div>
  </div>
</header>

<div class="app-container">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="tab-bar">
      <button class="tab-btn active" data-tab="skills" onclick="switchTab('skills')">
        Skills <span class="count" id="skills-count">0</span>
      </button>
      <button class="tab-btn" data-tab="agents" onclick="switchTab('agents')">
        Agents <span class="count" id="agents-count">0</span>
      </button>
    </div>
    <div class="sidebar-search">
      <input type="text" id="search-input" placeholder="Filter items..." oninput="filterList(this.value)">
    </div>
    <div class="item-list" id="item-list">
      <div class="empty-state">
        <span class="empty-icon">◈</span>
        <div>No items loaded</div>
      </div>
    </div>
  </div>

  <!-- Main Pane -->
  <div class="main-pane">
    <div class="pane-header">
      <h2 id="pane-title">Select an item</h2>
      <div class="pane-actions" id="pane-actions">
        <button class="btn btn-primary" onclick="showCreateForm()">+ New</button>
      </div>
    </div>
    <div class="pane-content" id="pane-content">
      <div class="empty-state">
        <span class="empty-icon">◈</span>
        <div>Choose a skill or agent from the sidebar to inspect its contents, or create something new.</div>
      </div>
    </div>
  </div>
</div>

<div id="toast-container"></div>

<script>
// ── State ──────────────────────────────────────────────────

let currentTab = 'skills';
let skills = [];
let agents = [];
let selectedItem = null;
let isEditing = false;
let searchQuery = '';

// ── API Helpers ─────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: {'X-Nova-Request': '1'} };
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

// ── Toast ──────────────────────────────────────────────────

function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.3s'; setTimeout(() => toast.remove(), 300); }, 3000);
}

// ── Tab Switching ──────────────────────────────────────────

function switchTab(tab) {
  currentTab = tab;
  selectedItem = null;
  isEditing = false;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.getElementById('search-input').value = '';
  searchQuery = '';
  renderList();
  renderPane();
}

// ── Search Filter ──────────────────────────────────────────

function filterList(query) {
  searchQuery = query.toLowerCase().trim();
  renderList();
}

// ── Data Loading ───────────────────────────────────────────

async function loadData() {
  try {
    skills = await api('GET', '/api/skills');
    agents = await api('GET', '/api/agents');
    document.getElementById('skills-count').textContent = skills.length;
    document.getElementById('agents-count').textContent = agents.length;
    renderList();
    if (selectedItem) {
      const items = currentTab === 'skills' ? skills : agents;
      const stillExists = items.find(i => i.name === selectedItem.name);
      if (!stillExists) { selectedItem = null; renderPane(); }
    }
  } catch (e) {
    showToast('Failed to load data: ' + e.message, 'error');
  }
}

// ── List Rendering ─────────────────────────────────────────

function renderList() {
  const el = document.getElementById('item-list');
  let items = currentTab === 'skills' ? skills : agents;

  // Apply search filter
  if (searchQuery) {
    items = items.filter(item =>
      item.name.toLowerCase().includes(searchQuery) ||
      (item.description || '').toLowerCase().includes(searchQuery)
    );
  }

  if (items.length === 0) {
    const msg = searchQuery
      ? `<span class="empty-icon">◈</span><div>No ${currentTab} match "<strong>${escapeHtml(searchQuery)}</strong>"</div>`
      : `<span class="empty-icon">◈</span><div>No ${currentTab} found. Click <strong>New</strong> to create one.</div>`;
    el.innerHTML = `<div class="empty-state">${msg}</div>`;
    return;
  }

  el.innerHTML = items.map((item, i) => {
    const isActive = selectedItem && selectedItem.name === item.name;
    const source = item.source || 'global';
    return `<div class="item-card ${isActive ? 'active' : ''}" style="animation-delay:${(i % 6) * 0.03}s" onclick="selectItem('${escapeHtml(item.name)}')">
      <div class="item-name">
        ${escapeHtml(item.name)}
        <span class="source-badge ${source}">${source}</span>
      </div>
      <div class="item-desc">${escapeHtml(item.description || 'No description')}</div>
    </div>`;
  }).join('');
}

// ── Item Selection ─────────────────────────────────────────

async function selectItem(name) {
  isEditing = false;
  try {
    const data = await api('GET', `/api/${currentTab}/${encodeURIComponent(name)}`);
    selectedItem = data;
    renderList();
    renderPane();
  } catch (e) {
    showToast('Failed to load item: ' + e.message, 'error');
  }
}

// ── Pane Rendering ─────────────────────────────────────────

function renderPane() {
  const titleEl = document.getElementById('pane-title');
  const actionsEl = document.getElementById('pane-actions');
  const contentEl = document.getElementById('pane-content');

  if (!selectedItem) {
    titleEl.innerHTML = 'Select an item';
    actionsEl.innerHTML = '<button class="btn btn-primary" onclick="showCreateForm()">+ New</button>';
    contentEl.innerHTML = '<div class="empty-state"><span class="empty-icon">◈</span><div>Choose a ' + currentTab.slice(0, -1) + ' from the sidebar to inspect its contents, or create something new.</div></div>';
    return;
  }

  const source = selectedItem.source || '';
  titleEl.innerHTML = `${escapeHtml(selectedItem.name)} <span class="pane-source">${source}</span>`;

  if (isEditing) {
    actionsEl.innerHTML = `
      <button class="btn btn-secondary" onclick="cancelEdit()">Cancel</button>
      <button class="btn btn-success" onclick="saveEdit()">Save</button>
    `;
    contentEl.innerHTML = `<div class="editor">
      <textarea id="editor-textarea">${escapeHtml(selectedItem.content || '')}</textarea>
      <div class="editor-actions">
        <button class="btn btn-success btn-sm" onclick="saveEdit()">Save Changes</button>
        <button class="btn btn-secondary btn-sm" onclick="cancelEdit()">Cancel</button>
      </div>
    </div>`;
    setTimeout(() => {
      const ta = document.getElementById('editor-textarea');
      if (ta) ta.focus();
    }, 50);
    return;
  }

  actionsEl.innerHTML = `
    <button class="btn btn-secondary btn-sm" onclick="startEdit()">Edit</button>
    <button class="btn btn-danger btn-sm" onclick="deleteItem()">Delete</button>
    <button class="btn btn-primary btn-sm" onclick="showCreateForm()">+ New</button>
  `;

  contentEl.innerHTML = `<div class="preview">${renderMarkdown(selectedItem.content || '')}</div>`;
}

// ── Markdown Renderer (simple) ─────────────────────────────

function renderMarkdown(md) {
  let html = md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^---$/gm, '<hr>')
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>')
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>');

  return '<p>' + html + '</p>';
}

// ── Edit Operations ────────────────────────────────────────

function startEdit() {
  isEditing = true;
  renderPane();
}

function cancelEdit() {
  isEditing = false;
  renderPane();
}

async function saveEdit() {
  const ta = document.getElementById('editor-textarea');
  if (!ta) return;
  const content = ta.value;
  try {
    await api('PUT', `/api/${currentTab}/${encodeURIComponent(selectedItem.name)}`, { content });
    showToast(`${currentTab.slice(0, -1)} "${selectedItem.name}" saved`);
    isEditing = false;
    const data = await api('GET', `/api/${currentTab}/${encodeURIComponent(selectedItem.name)}`);
    selectedItem = data;
    renderPane();
    loadData();
  } catch (e) {
    showToast('Failed to save: ' + e.message, 'error');
  }
}

async function deleteItem() {
  if (!confirm(`Delete the ${currentTab.slice(0, -1)} "${selectedItem.name}"?`)) return;
  try {
    await api('DELETE', `/api/${currentTab}/${encodeURIComponent(selectedItem.name)}`);
    showToast(`${currentTab.slice(0, -1)} "${selectedItem.name}" deleted`);
    selectedItem = null;
    loadData();
  } catch (e) {
    showToast('Failed to delete: ' + e.message, 'error');
  }
}

// ── Create Form ────────────────────────────────────────────

async function showCreateForm() {
  isEditing = false;
  selectedItem = null;
  renderList();

  const titleEl = document.getElementById('pane-title');
  const actionsEl = document.getElementById('pane-actions');
  const contentEl = document.getElementById('pane-content');

  titleEl.textContent = 'Create New ' + (currentTab === 'skills' ? 'Skill' : 'Agent');

  actionsEl.innerHTML = `
    <button class="btn btn-secondary btn-sm" onclick="cancelCreate()">Cancel</button>
    <button class="btn btn-success btn-sm" onclick="submitCreate()">Create</button>
  `;

  contentEl.innerHTML = `<div class="create-form">
    <div class="form-group">
      <label for="create-name">Name</label>
      <input type="text" id="create-name" placeholder="e.g., my-custom-skill" autofocus>
    </div>
    <div class="form-group">
      <label for="create-desc">Description</label>
      <input type="text" id="create-desc" placeholder="Brief description of what this does">
    </div>
    <div class="form-group">
      <label for="create-scope">Scope</label>
      <select id="create-scope">
        <option value="global">Global (~/.nova/${currentTab}/)</option>
        <option value="project">Project (.nova/${currentTab}/)</option>
      </select>
    </div>
    <div class="form-group">
      <label for="create-content">Content (Markdown)</label>
      <textarea id="create-content" placeholder="Write the ${currentTab === 'skills' ? 'SKILL.md' : 'agent.md'} content here...">---
name: 
description: 
---

# ${currentTab === 'skills' ? 'My Skill' : 'My Agent'}

Write your content here...</textarea>
    </div>
    <div class="form-actions">
      <button class="btn btn-success" onclick="submitCreate()">Create</button>
      <button class="btn btn-secondary" onclick="cancelCreate()">Cancel</button>
    </div>
  </div>`;

  setTimeout(() => {
    const el = document.getElementById('create-name');
    if (el) el.focus();
  }, 50);
}

function cancelCreate() {
  selectedItem = null;
  renderList();
  renderPane();
}

async function submitCreate() {
  const name = document.getElementById('create-name').value.trim();
  const description = document.getElementById('create-desc').value.trim();
  const scope = document.getElementById('create-scope').value;
  const content = document.getElementById('create-content').value;

  if (!name) { showToast('Name is required', 'error'); return; }

  try {
    await api('POST', `/api/${currentTab}`, { name, description, content, scope });
    showToast(`${currentTab.slice(0, -1)} "${name}" created`);
    await loadData();
    await selectItem(name);
  } catch (e) {
    showToast('Failed to create: ' + e.message, 'error');
  }
}

// ── Utility ────────────────────────────────────────────────

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ── Init ───────────────────────────────────────────────────

loadData();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Request Handler
# ---------------------------------------------------------------------------


from novacode_cli.security.local_http import LocalRequestHandler


class CreateRequestHandler(LocalRequestHandler):
    """HTTP request handler for the Create web UI API and static page."""

    # Shared state — set by CreateServer
    settings: Settings | None = None
    lock: threading.Lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP server logging."""
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_html(self, html: str, status: int = 200) -> None:
        from novacode_cli.security.local_http import secure_html
        html = secure_html(html)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_error(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status)

    def _read_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _get_path_parts(self) -> list[str]:
        """Parse path into parts, handling /api/<type>/<name>."""
        path = self.path.rstrip("/")
        parts = path.split("/")
        parts = [p for p in parts if p]
        if len(parts) == 3 and parts[0] == "api" and not _is_valid_name(parts[2]):
            return []
        return parts

    # ── CORS ────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parts = self._get_path_parts()

        # Serve the HTML page
        if self.path == "/" or self.path == "":
            self._send_html(HTML_PAGE)
            return

        # GET /api/skills — list all skills
        if parts == ["api", "skills"]:
            self._send_json(self._list_skills())
            return

        # GET /api/skills/<name> — get skill content
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "skills":
            result = self._get_skill(parts[2])
            if result is not None:
                self._send_json(result)
            else:
                self._send_error(f"Skill '{parts[2]}' not found", 404)
            return

        # GET /api/agents — list all agents
        if parts == ["api", "agents"]:
            self._send_json(self._list_agents())
            return

        # GET /api/agents/<name> — get agent content
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "agents":
            result = self._get_agent(parts[2])
            if result is not None:
                self._send_json(result)
            else:
                self._send_error(f"Agent '{parts[2]}' not found", 404)
            return

        self._send_error("Not found", 404)

    # ── POST ───────────────────────────────────────────────────────

    def do_POST(self) -> None:
        parts = self._get_path_parts()
        body = self._read_body()

        # POST /api/skills — create a new skill
        if parts == ["api", "skills"]:
            self._create_skill(body)
            return

        # POST /api/agents — create a new agent
        if parts == ["api", "agents"]:
            self._create_agent(body)
            return

        self._send_error("Not found", 404)

    # ── PUT ────────────────────────────────────────────────────────

    def do_PUT(self) -> None:
        parts = self._get_path_parts()
        body = self._read_body()

        # PUT /api/skills/<name> — update skill content
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "skills":
            self._update_skill(parts[2], body)
            return

        # PUT /api/agents/<name> — update agent content
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "agents":
            self._update_agent(parts[2], body)
            return

        self._send_error("Not found", 404)

    # ── DELETE ─────────────────────────────────────────────────────

    def do_DELETE(self) -> None:
        parts = self._get_path_parts()

        # DELETE /api/skills/<name> — delete a skill
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "skills":
            self._delete_skill(parts[2])
            return

        # DELETE /api/agents/<name> — delete an agent
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "agents":
            self._delete_agent(parts[2])
            return

        self._send_error("Not found", 404)

    # ── Skills CRUD ────────────────────────────────────────────────

    def _list_skills(self) -> list[dict]:
        """List all skills from global and project scopes."""
        settings = self.settings
        if not settings:
            return []

        result: list[dict] = []
        seen: set[str] = set()

        # Global skills (~/.nova/skills/)
        global_dir = settings.get_global_skills_dir()
        if global_dir.exists():
            for skill_dir in sorted(global_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_md = skill_dir / "SKILL.md"
                    if skill_md.exists():
                        content = skill_md.read_text(encoding="utf-8", errors="replace")
                        name = _extract_frontmatter_field(content, "name") or skill_dir.name
                        description = _extract_frontmatter_field(content, "description") or ""
                        result.append({
                            "name": name,
                            "description": description[:120],
                            "source": "global",
                            "path": str(skill_dir),
                        })
                        seen.add(name)

        # Project skills (.nova/skills/)
        project_dir = settings.get_project_skills_dir()
        if project_dir and project_dir.exists():
            for skill_dir in sorted(project_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_md = skill_dir / "SKILL.md"
                    if skill_md.exists():
                        content = skill_md.read_text(encoding="utf-8", errors="replace")
                        name = _extract_frontmatter_field(content, "name") or skill_dir.name
                        description = _extract_frontmatter_field(content, "description") or ""
                        # Project skills override global ones with same name
                        if name in seen:
                            result = [r for r in result if r["name"] != name]
                        result.append({
                            "name": name,
                            "description": description[:120],
                            "source": "project",
                            "path": str(skill_dir),
                        })
                        seen.add(name)

        return result

    def _get_skill(self, name: str) -> dict | None:
        """Get a skill's full SKILL.md content by name.

        Returns the skill dict, or None if not found (caller sends 404).
        """
        settings = self.settings
        if not settings:
            return None

        # Check project first, then global
        project_dir = settings.get_project_skills_dir()
        if project_dir:
            skill_dir = project_dir / name
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                content = skill_md.read_text(encoding="utf-8", errors="replace")
                return {"name": name, "content": content, "source": "project", "path": str(skill_dir)}

        global_dir = settings.get_global_skills_dir()
        skill_dir = global_dir / name
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            return {"name": name, "content": content, "source": "global", "path": str(skill_dir)}

        return None

    def _create_skill(self, body: dict) -> None:
        """Create a new skill."""
        name = body.get("name", "").strip()
        description = body.get("description", "").strip()
        content = body.get("content", "")
        scope = body.get("scope", "global")

        if not name:
            self._send_error("Name is required")
            return
        if not _is_valid_name(name):
            self._send_error("Invalid name. Use only letters, numbers, hyphens, and underscores.")
            return

        settings = self.settings
        if not settings:
            self._send_error("Settings not available", 500)
            return

        # Determine target directory
        if scope == "project":
            base_dir = settings.get_project_skills_dir()
            if not base_dir:
                self._send_error("Not in a project directory", 400)
                return
        else:
            base_dir = settings.get_global_skills_dir()

        skill_dir = base_dir / name
        if skill_dir.exists():
            self._send_error(f"Skill '{name}' already exists", 409)
            return

        # Ensure content has frontmatter
        final_content = _ensure_frontmatter(content, name, description)

        # Write atomically
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        tmp_path = skill_dir / f".tmp.{os.getpid()}"
        try:
            tmp_path.write_text(final_content, encoding="utf-8")
            tmp_path.replace(skill_md)
        except Exception as e:
            # Clean up on failure
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            skill_dir.rmdir()
            self._send_error(f"Failed to create skill: {e}", 500)
            return

        self._send_json({"name": name, "source": scope, "path": str(skill_dir)}, 201)

    def _update_skill(self, name: str, body: dict) -> None:
        """Update a skill's SKILL.md content."""
        content = body.get("content", "")
        if not content:
            self._send_error("Content is required")
            return

        settings = self.settings
        if not settings:
            self._send_error("Settings not available", 500)
            return

        # Find the skill directory
        project_dir = settings.get_project_skills_dir()
        skill_dir = None
        if project_dir:
            candidate = project_dir / name
            if (candidate / "SKILL.md").exists():
                skill_dir = candidate

        if not skill_dir:
            global_dir = settings.get_global_skills_dir()
            candidate = global_dir / name
            if (candidate / "SKILL.md").exists():
                skill_dir = candidate

        if not skill_dir:
            self._send_error(f"Skill '{name}' not found", 404)
            return

        # Write atomically
        skill_md = skill_dir / "SKILL.md"
        tmp_path = skill_dir / f".tmp.{os.getpid()}"
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(skill_md)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            self._send_error(f"Failed to save: {e}", 500)
            return

        self._send_json({"name": name, "status": "saved"})

    def _delete_skill(self, name: str) -> None:
        """Delete a skill directory."""
        settings = self.settings
        if not settings:
            self._send_error("Settings not available", 500)
            return

        import shutil

        # Find the skill directory
        project_dir = settings.get_project_skills_dir()
        skill_dir = None
        if project_dir:
            candidate = project_dir / name
            if candidate.exists():
                skill_dir = candidate

        if not skill_dir:
            global_dir = settings.get_global_skills_dir()
            candidate = global_dir / name
            if candidate.exists():
                skill_dir = candidate

        if not skill_dir:
            self._send_error(f"Skill '{name}' not found", 404)
            return

        try:
            shutil.rmtree(skill_dir)
        except Exception as e:
            self._send_error(f"Failed to delete: {e}", 500)
            return

        self._send_json({"name": name, "status": "deleted"})

    # ── Agents CRUD ─────────────────────────────────────────────────

    def _list_agents(self) -> list[dict]:
        """List all agents from global and project scopes."""
        settings = self.settings
        if not settings:
            return []

        result: list[dict] = []
        seen: set[str] = set()

        # Global agents (~/.nova/agents/)
        global_dir = settings.get_agents_root_dir()
        if global_dir.exists():
            for agent_dir in sorted(global_dir.iterdir()):
                if agent_dir.is_dir():
                    agent_md = agent_dir / "agent.md"
                    if agent_md.exists():
                        content = agent_md.read_text(encoding="utf-8", errors="replace")
                        name = _extract_frontmatter_field(content, "name") or agent_dir.name
                        description = _extract_frontmatter_field(content, "description") or ""
                        result.append({
                            "name": name,
                            "description": description[:120],
                            "source": "global",
                            "path": str(agent_dir),
                        })
                        seen.add(name)

        # Project agents (.nova/agents/)
        project_dir = settings.get_project_agents_dir()
        if project_dir and project_dir.exists():
            for agent_dir in sorted(project_dir.iterdir()):
                if agent_dir.is_dir():
                    agent_md = agent_dir / "agent.md"
                    if agent_md.exists():
                        content = agent_md.read_text(encoding="utf-8", errors="replace")
                        name = _extract_frontmatter_field(content, "name") or agent_dir.name
                        description = _extract_frontmatter_field(content, "description") or ""
                        if name in seen:
                            result = [r for r in result if r["name"] != name]
                        result.append({
                            "name": name,
                            "description": description[:120],
                            "source": "project",
                            "path": str(agent_dir),
                        })
                        seen.add(name)

        return result

    def _get_agent(self, name: str) -> dict | None:
        """Get an agent's full agent.md content by name.

        Returns the agent dict, or None if not found (caller sends 404).
        """
        settings = self.settings
        if not settings:
            return None

        # Check project first, then global
        project_dir = settings.get_project_agents_dir()
        if project_dir:
            agent_dir = project_dir / name
            agent_md = agent_dir / "agent.md"
            if agent_md.exists():
                content = agent_md.read_text(encoding="utf-8", errors="replace")
                return {"name": name, "content": content, "source": "project", "path": str(agent_dir)}

        global_dir = settings.get_agents_root_dir()
        agent_dir = global_dir / name
        agent_md = agent_dir / "agent.md"
        if agent_md.exists():
            content = agent_md.read_text(encoding="utf-8", errors="replace")
            return {"name": name, "content": content, "source": "global", "path": str(agent_dir)}

        return None

    def _create_agent(self, body: dict) -> None:
        """Create a new agent."""
        name = body.get("name", "").strip()
        description = body.get("description", "").strip()
        content = body.get("content", "")
        scope = body.get("scope", "global")

        if not name:
            self._send_error("Name is required")
            return
        if not _is_valid_name(name):
            self._send_error("Invalid name. Use only letters, numbers, hyphens, and underscores.")
            return

        settings = self.settings
        if not settings:
            self._send_error("Settings not available", 500)
            return

        # Determine target directory
        if scope == "project":
            base_dir = settings.get_project_agents_dir()
            if not base_dir:
                self._send_error("Not in a project directory", 400)
                return
        else:
            base_dir = settings.get_agents_root_dir()

        agent_dir = base_dir / name
        if agent_dir.exists():
            self._send_error(f"Agent '{name}' already exists", 409)
            return

        # Ensure content has frontmatter
        final_content = _ensure_frontmatter(content, name, description)

        # Write atomically
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_md = agent_dir / "agent.md"
        tmp_path = agent_dir / f".tmp.{os.getpid()}"
        try:
            tmp_path.write_text(final_content, encoding="utf-8")
            tmp_path.replace(agent_md)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            agent_dir.rmdir()
            self._send_error(f"Failed to create agent: {e}", 500)
            return

        self._send_json({"name": name, "source": scope, "path": str(agent_dir)}, 201)

    def _update_agent(self, name: str, body: dict) -> None:
        """Update an agent's agent.md content."""
        content = body.get("content", "")
        if not content:
            self._send_error("Content is required")
            return

        settings = self.settings
        if not settings:
            self._send_error("Settings not available", 500)
            return

        # Find the agent directory
        project_dir = settings.get_project_agents_dir()
        agent_dir = None
        if project_dir:
            candidate = project_dir / name
            if (candidate / "agent.md").exists():
                agent_dir = candidate

        if not agent_dir:
            global_dir = settings.get_agents_root_dir()
            candidate = global_dir / name
            if (candidate / "agent.md").exists():
                agent_dir = candidate

        if not agent_dir:
            self._send_error(f"Agent '{name}' not found", 404)
            return

        # Write atomically
        agent_md = agent_dir / "agent.md"
        tmp_path = agent_dir / f".tmp.{os.getpid()}"
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(agent_md)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            self._send_error(f"Failed to save: {e}", 500)
            return

        self._send_json({"name": name, "status": "saved"})

    def _delete_agent(self, name: str) -> None:
        """Delete an agent directory."""
        settings = self.settings
        if not settings:
            self._send_error("Settings not available", 500)
            return

        import shutil

        # Find the agent directory
        project_dir = settings.get_project_agents_dir()
        agent_dir = None
        if project_dir:
            candidate = project_dir / name
            if candidate.exists():
                agent_dir = candidate

        if not agent_dir:
            global_dir = settings.get_agents_root_dir()
            candidate = global_dir / name
            if candidate.exists():
                agent_dir = candidate

        if not agent_dir:
            self._send_error(f"Agent '{name}' not found", 404)
            return

        try:
            shutil.rmtree(agent_dir)
        except Exception as e:
            self._send_error(f"Failed to delete: {e}", 500)
            return

        self._send_json({"name": name, "status": "deleted"})


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class CreateServer:
    """Lightweight HTTP server for the Create web UI.

    Runs in a background daemon thread. Serves a self-contained HTML single-page
    app for browsing, previewing, editing, creating, and deleting skills and agents.
    """

    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0
        self.is_running: bool = False

    def _find_available_port(self) -> int:
        """Find a random available port on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def start(self) -> int:
        """Start the HTTP server in a background daemon thread.

        Returns:
            The port number the server is listening on.
        """
        port = self._find_available_port()

        # Wire up shared state to the handler class
        CreateRequestHandler.settings = Settings.from_environment()

        self._server = HTTPServer(("127.0.0.1", port), CreateRequestHandler)
        import secrets
        self._server.auth_token = secrets.token_urlsafe(32)
        self.port = port
        self.is_running = True

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="create-server",
        )
        self._thread.start()

        return port

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
