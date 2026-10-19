# ruff: noqa: E501 — the inline page template has long CSS/JS lines
"""Self-contained HTML export of a session tree (``/export``, ``/share``).

The page embeds every branch as JSON and renders it with a few lines of inline
script: the tree on the left, the conversation along the chosen path on the
right. No network access, so it also renders from a gist preview. Secrets that
match common token formats are masked before anything is written.
"""

from __future__ import annotations

import html
import json
import re
from typing import TYPE_CHECKING, Any

from novacode_cli.session.history_tree import message_text

if TYPE_CHECKING:
    from novacode_cli.session.history_tree import HistoryTree, Node

#: Best-effort masks for credentials that commonly end up in tool output.
_SECRETS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{35}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\b(?:nvapi|hf|glpat)[-_][A-Za-z0-9_-]{20,}"
)
#: Tool results are capped so one huge file read can't bloat the page.
TOOL_TEXT_LIMIT = 20_000


def redact(text: str) -> str:
    """Mask token-shaped secrets."""
    return _SECRETS.sub("[redacted]", text)


def _node_payload(node: Node) -> dict[str, Any]:
    msg = node.msg
    text = message_text(msg)
    if msg.get("type") == "ToolMessage" and len(text) > TOOL_TEXT_LIMIT:
        text = text[:TOOL_TEXT_LIMIT] + f"\n… ({len(text) - TOOL_TEXT_LIMIT} more characters)"
    calls = [
        {"name": c.get("name", ""), "args": redact(json.dumps(c.get("args", {}), indent=2))}
        for c in msg.get("tool_calls") or []
    ]
    return {
        "id": node.id,
        "parent": node.parent,
        "kind": node.kind,
        "type": msg.get("type", ""),
        "name": msg.get("name") or "",
        "label": node.label or "",
        "ts": node.ts,
        "text": redact(text),
        "calls": calls,
        "children": node.children,
    }


def export_html(tree: HistoryTree, *, title: str, subtitle: str = "") -> str:
    """The whole tree as one HTML page, opened at the current position."""
    data = {
        "nodes": {nid: _node_payload(n) for nid, n in tree.nodes.items()},
        "roots": tree.roots,
        "leaf": tree.leaf,
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__DATA__", blob)
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f7f7f8; --panel: #ffffff; --text: #1d1d22; --muted: #6b6b76;
  --line: #e2e2e8; --accent: #6d4fd6; --user: #eef0ff; --tool: #f3f3f5;
  --mark: #b7791f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #111116; --panel: #18181f; --text: #e6e6ea; --muted: #9a9aa6;
    --line: #2a2a33; --accent: #a58cff; --user: #22223a; --tool: #1f1f26;
    --mark: #e0a84a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 16px 20px; border-bottom: 1px solid var(--line); background: var(--panel); }
header h1 { margin: 0; font-size: 18px; }
header p { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.wrap { display: grid; grid-template-columns: minmax(240px, 340px) 1fr; min-height: calc(100vh - 70px); }
nav { border-right: 1px solid var(--line); padding: 12px; overflow: auto; max-height: calc(100vh - 70px);
  position: sticky; top: 0; background: var(--panel); }
nav label { color: var(--muted); font-size: 13px; display: block; margin-bottom: 8px; }
nav ul { list-style: none; margin: 0; padding-left: 14px; border-left: 1px dashed var(--line); }
nav > ul { padding-left: 0; border: 0; }
nav li > button { all: unset; cursor: pointer; display: block; width: 100%; padding: 2px 6px;
  border-radius: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 13px; }
nav li > button:hover { background: var(--tool); }
nav li > button.on { background: var(--user); }
nav li > button.sel { outline: 2px solid var(--accent); }
.k-user { color: var(--accent); }
.k-tool { color: var(--muted); }
.mark { color: var(--mark); }
main { padding: 20px; max-width: 900px; width: 100%; }
.msg { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 14px; margin: 0 0 12px; }
.msg.user { background: var(--user); }
.msg.tool { background: var(--tool); font-size: 13px; }
.who { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.body { white-space: pre-wrap; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 6px 0 0;
  font: 12.5px/1.45 ui-monospace, "Cascadia Code", Consolas, monospace; }
details summary { cursor: pointer; }
.hide-tools .tool { display: none; }
@media (max-width: 760px) {
  .wrap { grid-template-columns: 1fr; }
  nav { position: static; max-height: 40vh; border-right: 0; border-bottom: 1px solid var(--line); }
  main { padding: 16px; }
}
</style>
</head>
<body>
<header><h1>__TITLE__</h1><p>__SUBTITLE__</p></header>
<div class="wrap">
  <nav><label><input type="checkbox" id="tools"> Show tool steps</label><ul id="tree"></ul></nav>
  <main id="conv" class="hide-tools"></main>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const N = D.nodes;
let selected = D.leaf;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function preview(n) {
  if (n.calls.length) return "\\u2699 " + n.calls.map(c => c.name).join(", ");
  if (n.kind === "tool") return "\\u2699 " + (n.name || "result");
  const who = n.kind === "user" ? "you: " : "";
  return who + (n.text.split("\\n").find(l => l.trim()) || "(empty)");
}
function pathTo(id) {
  const out = [];
  for (let n = N[id]; n; n = n.parent ? N[n.parent] : null) out.push(n);
  return out.reverse();
}
function onPath(id) {
  return new Set(pathTo(selected).map(n => n.id)).has(id);
}
// Single-child chains stay at one level; each branch nests under its fork.
function addChain(ul, id) {
  while (id) {
    const n = N[id];
    const li = el("li");
    const b = el("button", "k-" + n.kind);
    if (n.label) b.append(el("span", "mark", "\\u2605 " + n.label + "  "));
    b.append(document.createTextNode(preview(n)));
    b.dataset.id = id;
    b.title = n.ts;
    li.append(b);
    ul.append(li);
    if (n.children.length > 1) {
      for (const child of n.children) {
        const sub = el("ul");
        li.append(sub);
        addChain(sub, child);
      }
      return;
    }
    id = n.children[0];
  }
}
function drawTree() {
  const root = document.getElementById("tree");
  root.textContent = "";
  if (D.roots.length > 1) {
    for (const r of D.roots) { const sub = el("ul"); root.append(el("li")); root.lastChild.append(sub); addChain(sub, r); }
  } else if (D.roots.length) addChain(root, D.roots[0]);
  for (const b of root.querySelectorAll("button")) {
    b.classList.toggle("on", onPath(b.dataset.id));
    b.classList.toggle("sel", b.dataset.id === selected);
    b.onclick = () => { selected = b.dataset.id; drawTree(); drawConv(); };
  }
}
function drawConv() {
  const main = document.getElementById("conv");
  main.textContent = "";
  for (const n of pathTo(selected)) {
    const box = el("div", "msg " + n.kind);
    const who = n.kind === "user" ? "You" : n.kind === "assistant" ? "Nova"
      : n.calls.length ? "Nova \\u00b7 tool call" : "Tool result" + (n.name ? " \\u00b7 " + n.name : "");
    box.append(el("div", "who", (n.label ? "\\u2605 " + n.label + " \\u00b7 " : "") + who));
    if (n.text.trim()) {
      if (n.kind === "tool" && !n.calls.length) {
        const d = el("details"); d.append(el("summary", "", "output")); d.append(el("pre", "", n.text)); box.append(d);
      } else box.append(el("div", "body", n.text));
    }
    for (const c of n.calls) {
      const d = el("details"); d.append(el("summary", "", "\\u2699 " + c.name)); d.append(el("pre", "", c.args)); box.append(d);
    }
    main.append(box);
  }
}
document.getElementById("tools").onchange = e =>
  document.getElementById("conv").classList.toggle("hide-tools", !e.target.checked);
drawTree();
drawConv();
</script>
</body>
</html>
"""
