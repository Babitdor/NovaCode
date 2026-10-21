"""Lightweight local HTTP server that renders artifacts for the browser.

Mirrors ``commands/create_server.CreateServer``: a stdlib ``http.server`` on a
random localhost port in a daemon thread, started lazily the first time an
artifact is created. It only ever reads the in-memory artifact registry — it has
no access to (and never exposes) the filesystem, env vars, secrets, or the agent.

Safety model: HTML/`dashboard` artifacts render inside an ``<iframe sandbox>``
with only ``allow-scripts`` (an opaque origin — no same-origin, no access to the
viewer page, cookies, or storage). Markdown renders server-side with raw HTML
disabled, so embedded ``<script>`` never executes. Artifact code is NEVER run on
the host — only in the visitor's browser, isolated.
"""

from __future__ import annotations

import html as _html
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

from novacode_cli.artifacts.registry import Artifact, get_registry


def _render_markdown(text: str) -> str:
    """Render Markdown to HTML with raw HTML disabled (no script execution)."""
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
        return md.render(text or "")
    except Exception:  # noqa: BLE001 — fall back to escaped preformatted text
        return f"<pre>{_html.escape(text or '')}</pre>"


def _time_str(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #1a1b26; color: #c0caf5; }
header { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 12px;
         padding: 12px 18px; background: #16161e; border-bottom: 1px solid #2a2b3c; }
header .glyph { color: #7aa2f7; font-size: 20px; }
header h1 { font-size: 15px; margin: 0; font-weight: 600; flex: 0 1 auto;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #24283b; color: #7aa2f7; }
.badge.updated { color: #e0af68; }
.meta { font-size: 11px; color: #565f89; margin-left: auto; white-space: nowrap; }
button { font: inherit; font-size: 12px; padding: 5px 12px; border-radius: 6px; cursor: pointer;
         background: #7aa2f7; color: #16161e; border: none; font-weight: 600; }
button:hover { background: #9ab4ff; }
main { padding: 0; }
iframe { width: 100%; height: calc(100vh - 49px); border: 0; background: #fff; display: block; }
.markdown { max-width: 820px; margin: 0 auto; padding: 28px 22px; line-height: 1.65; }
.markdown pre { background: #16161e; padding: 12px 14px; border-radius: 8px; overflow-x: auto; }
.markdown code { background: #24283b; padding: 1px 5px; border-radius: 4px; font-size: 90%; }
.markdown pre code { background: none; padding: 0; }
.markdown h1,.markdown h2,.markdown h3 { line-height: 1.25; }
.markdown a { color: #7aa2f7; }
.markdown table { border-collapse: collapse; }
.markdown th,.markdown td { border: 1px solid #2a2b3c; padding: 6px 10px; }
.empty { padding: 40px; text-align: center; color: #565f89; }
@media (max-width: 600px) {
  header { flex-wrap: wrap; gap: 8px; padding: 10px 12px; }
  header h1 { flex: 1 1 45%; font-size: 14px; }
  .meta { display: none; }
  header button { min-height: 38px; }
  iframe { height: calc(100dvh - 60px); }
  .markdown { padding: 20px 14px; overflow-wrap: anywhere; }
  .markdown table { display: block; max-width: 100%; overflow-x: auto; }
  .markdown pre { max-width: 100%; }
}
"""

_ARTIFACT_MOBILE_BASELINE = """
<style id="nova-mobile-baseline">
@media (max-width: 640px) {
  html, body { max-width: 100%; overflow-x: hidden; }
  body { min-width: 0 !important; }
  img, video, canvas, svg, iframe { max-width: 100% !important; }
  pre, code { max-width: 100%; overflow-x: auto; }
  table { display: block; max-width: 100%; overflow-x: auto; }
  a, p, li, h1, h2, h3 { overflow-wrap: anywhere; }
}
</style>
"""


def _responsive_artifact_html(content: str) -> str:
    """Add viewport metadata and overflow guards to user-authored previews."""
    additions = ""
    if not re.search(r"<meta\b[^>]*name=[\"']viewport[\"']", content, re.IGNORECASE):
        additions += '<meta name="viewport" content="width=device-width, initial-scale=1">'
    additions += _ARTIFACT_MOBILE_BASELINE
    head = re.search(r"<head\b[^>]*>", content, re.IGNORECASE)
    if head:
        closing_head = re.search(r"</head\s*>", content[head.end() :], re.IGNORECASE)
        if closing_head:
            index = head.end() + closing_head.start()
            return content[:index] + additions + content[index:]
        return content[: head.end()] + additions + content[head.end() :]
    html = re.search(r"<html\b[^>]*>", content, re.IGNORECASE)
    if html:
        return content[: html.end()] + "<head>" + additions + "</head>" + content[html.end() :]
    doctype = re.search(r"<!doctype\b[^>]*>", content, re.IGNORECASE)
    if doctype:
        return content[: doctype.end()] + additions + content[doctype.end() :]
    return additions + content


def _viewer_html(art: Artifact) -> str:
    status_cls = "updated" if art.status == "updated" else ""
    when = (
        f"created {_time_str(art.created_at)}"
        if art.version == 1
        else f"updated {_time_str(art.updated_at)} · v{art.version}"
    )
    if art.type in ("html", "dashboard"):
        # srcdoc value is HTML-entity-decoded by the browser, then parsed inside
        # the sandbox — so escaping &<>\" here is correct and safe.
        srcdoc = _html.escape(_responsive_artifact_html(art.content or ""), quote=True)
        body = (
            f'<iframe sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
            f'title="{_html.escape(art.title)}" srcdoc="{srcdoc}"></iframe>'
        )
    else:
        body = f'<main><div class="markdown">{_render_markdown(art.content)}</div></main>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(art.title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body data-version="{art.version}">
<header>
  <span class="glyph">&#9670;</span>
  <h1>{_html.escape(art.title)}</h1>
  <span class="badge {status_cls}">{_html.escape(art.type)}</span>
  <span class="meta">{when}</span>
  <button id="copy">Copy Link</button>
</header>
{body}
<script>
  document.getElementById('copy').addEventListener('click', function () {{
    navigator.clipboard.writeText(location.href).then(function () {{
      var b = document.getElementById('copy'); var t = b.textContent;
      b.textContent = 'Copied!'; setTimeout(function () {{ b.textContent = t; }}, 1200);
    }});
  }});
  // Live update-state: poll metadata; reload when the artifact changes.
  var v = document.body.getAttribute('data-version');
  setInterval(function () {{
    fetch('/api/artifacts/{art.id}').then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (j) {{ if (j && String(j.version) !== v) location.reload(); }})
      .catch(function () {{}});
  }}, 3000);
</script>
</body>
</html>"""


_INDEX_CSS = """
:root{color-scheme:dark;--bg:#11141d;--panel:#191e2b;--line:#30384c;
--text:#edf0fa;--muted:#a6afc4;--accent:#a5b6ff;--mono:'Cascadia Code',Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 'Segoe UI',system-ui,sans-serif}button,input,select{font:inherit}
[hidden]{display:none!important}a{color:inherit}svg{display:block;flex-shrink:0}
.shell{width:min(1120px,calc(100% - 64px));margin:auto}
.topbar{border-bottom:1px solid var(--line);background:#141824}
.topbar .shell{min-height:76px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{display:flex;align-items:center;gap:11px;text-decoration:none;font-size:16px;font-weight:650}
.brand-mark{width:30px;height:30px;display:grid;place-items:center;border:1px solid #485583;
border-radius:9px;color:var(--accent);background:#232c45}
.brand-divider{height:17px;width:1px;background:#41495d;margin:0 3px}
.brand-section{color:var(--muted);font-weight:400}
.session-label{font:11px var(--mono);color:var(--muted);display:flex;align-items:center;gap:8px}
.session-label::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--accent)}
main{padding-top:56px;padding-bottom:48px}.intro{display:flex;align-items:flex-end;
justify-content:space-between;gap:24px;margin-bottom:35px}.eyebrow{font:11px var(--mono);
letter-spacing:.13em;text-transform:uppercase;color:var(--accent);margin:0 0 12px}
h1{font-family:Bahnschrift,'Segoe UI',sans-serif;font-weight:600;font-size:clamp(34px,5vw,46px);
letter-spacing:-.045em;line-height:1.12;margin:0 0 12px}.subtitle{color:var(--muted);margin:0;max-width:560px}
.collection-count{font:12px var(--mono);color:var(--muted);white-space:nowrap;padding-bottom:3px}
.toolbar{display:flex;align-items:center;gap:12px;margin:0 0 25px}
.search{position:relative;flex:1;max-width:420px}.search svg{position:absolute;left:14px;
top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
input,select{min-height:46px;background:#171c28;border:1px solid var(--line);border-radius:8px;
color:var(--text);font-size:13px}input{width:100%;padding:10px 14px 10px 43px}
input::placeholder{color:#9aa5bf}select{padding:10px 35px 10px 13px;margin-left:auto;max-width:100%}
input:focus-visible,select:focus-visible,.artifact:focus-visible,.brand:focus-visible{
outline:2px solid var(--accent);outline-offset:4px}
.collection{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.artifact{--tone:#a5b6ff;--tint:#252e49;display:flex;flex-direction:column;min-width:0;
min-height:254px;padding:23px;background:var(--panel);border:1px solid var(--line);
border-radius:12px;text-decoration:none;transition:border-color .18s,transform .18s,background .18s}
.artifact:hover{border-color:#6474aa;background:#1d2333;transform:translateY(-3px)}
.artifact[data-type=markdown]{--tone:#e1c18d;--tint:#373025}
.artifact[data-type=dashboard]{--tone:#c5b1ed;--tint:#302a42}
.artifact-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:25px}
.file-icon{width:45px;height:49px;position:relative;display:grid;place-items:center;
background:var(--tint);color:var(--tone);border:1px solid color-mix(in srgb,var(--tone) 35%,transparent);
border-radius:7px 0 7px 7px;font:600 13px var(--mono)}
.file-icon::after{content:'';position:absolute;right:-1px;top:-1px;width:12px;height:12px;
background:var(--panel);border-left:1px solid var(--tone);border-bottom:1px solid var(--tone);
border-bottom-left-radius:3px}.format{font:10px var(--mono);letter-spacing:.08em;text-transform:uppercase;
padding:5px 8px;background:var(--tint);color:var(--tone);border-radius:5px}
.artifact h2{margin:0 0 6px;font-size:18px;font-weight:600;line-height:1.4;letter-spacing:-.02em;
overflow-wrap:anywhere}.artifact-description{margin:0 0 22px;font-size:12px;color:var(--muted)}
.artifact-bottom{margin-top:auto;padding-top:15px;border-top:1px solid var(--line)}
.artifact-details{display:flex;flex-wrap:wrap;gap:6px 12px;font:10px/1.6 var(--mono);color:var(--muted)}
.open-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:15px;
color:var(--tone);font-size:12px;font-weight:600}.artifact-id{font:10px var(--mono);color:var(--muted)}
.empty-state{grid-column:1/-1;text-align:center;padding:64px 24px;border:1px dashed var(--line);
border-radius:12px;color:var(--muted)}.empty-state svg{margin:0 auto 18px;color:var(--accent)}
.empty-state h2{font-size:20px;font-weight:550;margin:0 0 8px;color:var(--text)}
.empty-state p{margin:0}.footer{margin-top:35px;padding-top:19px;border-top:1px solid var(--line);
display:flex;justify-content:space-between;gap:16px;color:var(--muted);font:10px/1.6 var(--mono)}
@media(min-width:761px){.artifact:only-child{max-width:calc((1120px - 36px)/3)}}
@media(max-width:1000px){.collection{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.shell{width:calc(100% - 36px)}.topbar .shell{min-height:66px}
.brand-section,.brand-divider{display:none}.session-label{font-size:10px}main{padding-top:34px}
.intro{display:block;margin-bottom:25px}.collection-count{display:block;margin-top:18px}
.toolbar{flex-wrap:wrap;gap:10px}.search{flex-basis:100%;max-width:none}select{width:100%;margin-left:0}
.collection{grid-template-columns:1fr;gap:13px}.artifact{min-height:244px;padding:21px}
.footer{flex-direction:column;gap:5px}.subtitle{font-size:14px}}
@media(prefers-reduced-motion:reduce){.artifact{transition:none}.artifact:hover{transform:none}}
"""

_INDEX_SCRIPT = """
const toolbar = document.getElementById('toolbar');
const search = document.getElementById('search');
const sort = document.getElementById('sort');
const collection = document.getElementById('collection');
const cards = Array.from(collection.querySelectorAll('.artifact'));
const count = document.getElementById('collection-count');
const noResults = document.getElementById('no-results');
if (cards.length) toolbar.hidden = false;
function updateCollection() {
  const query = search.value.trim().toLocaleLowerCase();
  cards.sort((a, b) => {
    if (sort.value === 'title') return a.dataset.title.localeCompare(b.dataset.title);
    if (sort.value === 'type') return a.dataset.type.localeCompare(b.dataset.type)
      || a.dataset.title.localeCompare(b.dataset.title);
    return Number(b.dataset.updated) - Number(a.dataset.updated);
  });
  let visible = 0;
  for (const card of cards) {
    card.hidden = !card.dataset.search.includes(query);
    if (!card.hidden) visible++;
    collection.appendChild(card);
  }
  count.textContent = query ? `${visible} of ${cards.length} artifacts`
    : `${cards.length} artifact${cards.length === 1 ? '' : 's'}`;
  noResults.hidden = visible !== 0 || cards.length === 0;
}
search.addEventListener('input', updateCollection);
sort.addEventListener('change', updateCollection);
"""


def _index_html() -> str:
    """Render the session collection without executing any artifact content."""
    arts = sorted(get_registry().list(), key=lambda art: art.updated_at, reverse=True)
    formats = {
        "html": ("HTML", "</>", "Interactive web page"),
        "markdown": ("Markdown", "M↓", "Markdown document"),
        "dashboard": ("Dashboard", "▦", "Interactive dashboard"),
    }
    rows = []
    for art in arts:
        kind = art.type if art.type in formats else "markdown"
        label, glyph, description = formats[kind]
        title = _html.escape(art.title, quote=True)
        artifact_id = _html.escape(art.id, quote=True)
        search = _html.escape(f"{art.title} {art.type} {art.id}".lower(), quote=True)
        when = _time_str(art.updated_at)
        rows.append(
            f'<a class="artifact" href="/artifacts/{quote(art.id, safe="")}" '
            f'data-title="{title}" data-type="{kind}" '
            f'data-search="{search}" data-updated="{art.updated_at}">'
            f'<div class="artifact-top"><span class="file-icon" aria-hidden="true">'
            f'{_html.escape(glyph)}</span><span class="format">{label}</span></div>'
            f'<h2>{title}</h2><p class="artifact-description">{description}</p>'
            f'<div class="artifact-bottom"><div class="artifact-details">'
            f'<span>Version {art.version}</span><span>Updated {when}</span></div>'
            f'<div class="open-row"><span>Open artifact <span aria-hidden="true">↗</span></span>'
            f'<span class="artifact-id">#{artifact_id}</span></div></div></a>'
        )
    empty = (
        '<div class="empty-state"><svg width="36" height="40" viewBox="0 0 24 28" '
        'fill="none" stroke="currentColor" aria-hidden="true">'
        '<path d="M5 1h9l6 6v19H5zM14 1v7h6M9 14h7M9 19h5"/></svg>'
        '<h2>No artifacts yet</h2><p>Artifacts created in this Nova session will appear here.</p></div>'
        if not arts else ""
    )
    count = f"{len(arts)} artifact{'s' if len(arts) != 1 else ''}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>Artifacts · Nova</title>
<style>{_INDEX_CSS}</style></head><body>
<header class="topbar"><div class="shell">
  <a class="brand" href="/" aria-label="Nova artifacts home"><span class="brand-mark" aria-hidden="true">◆</span>
  <span>Nova</span><span class="brand-divider"></span><span class="brand-section">Artifacts</span></a>
  <span class="session-label">Session collection</span>
</div></header>
<main class="shell"><div class="intro"><div><p class="eyebrow">Made with Nova</p>
  <h1>Your artifacts.</h1><p class="subtitle">Pages, documents, and ideas brought to life in this session.</p>
  </div><span class="collection-count" id="collection-count" role="status" aria-live="polite">{count}</span></div>
<div class="toolbar" id="toolbar" hidden>
  <div class="search"><svg width="17" height="17" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/>
    <path d="m16 16 5 5"/></svg><input id="search" type="search" aria-label="Search artifacts"
    placeholder="Search by title, type, or ID…" autocomplete="off"></div>
  <select id="sort" aria-label="Sort artifacts"><option value="updated">Recently updated</option>
    <option value="title">Title A–Z</option><option value="type">File type</option></select>
</div>
<section class="collection" id="collection" aria-label="Session artifacts">{''.join(rows)}{empty}</section>
<div class="empty-state" id="no-results" hidden><h2>No matching artifacts</h2>
  <p>Try another title, file type, or artifact ID.</p></div>
<footer class="footer"><span>Created in Nova. Ready to explore.</span>
  <span>Choose an artifact to open its full preview ↗</span></footer></main>
<script>{_INDEX_SCRIPT}</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # silence default stderr logging
        pass

    def _send(self, body: str, *, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # Defense-in-depth: don't let this page be framed by others; keep scripts
        # inline-only. The per-artifact iframe sandbox is the real isolation.
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        reg = get_registry()
        try:
            if path == "/":
                self._send(_index_html())
            elif len(parts) == 2 and parts[0] == "artifacts":
                art = reg.get(parts[1])
                if art is None:
                    self._send("<h1>404 — artifact not found</h1>", status=404)
                else:
                    self._send(_viewer_html(art))
            elif path == "/api/artifacts":
                self._send(
                    json.dumps([a.public_dict() for a in reg.list()]),
                    content_type="application/json",
                )
            elif len(parts) == 3 and parts[0] == "api" and parts[1] == "artifacts":
                art = reg.get(parts[2])
                if art is None:
                    self._send(json.dumps({"error": "not found"}), content_type="application/json", status=404)
                else:
                    self._send(json.dumps(art.public_dict()), content_type="application/json")
            else:
                self._send("<h1>404</h1>", status=404)
        except Exception as e:  # noqa: BLE001 — never crash the daemon thread
            self._send(f"<h1>500</h1><pre>{_html.escape(str(e))}</pre>", status=500)


class ArtifactServer:
    """Singleton-ish server; use :func:`ensure_server`."""

    def __init__(self) -> None:
        self._httpd: HTTPServer | None = None
        self.port: int = 0

    def start(self) -> int:
        if self._httpd is not None:
            return self.port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self._httpd = HTTPServer(("127.0.0.1", self.port), _Handler)
        threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="nova-artifact-server"
        ).start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


_server: ArtifactServer | None = None
_lock = threading.Lock()


def ensure_server() -> str:
    """Start the artifact server if needed; return its base URL (``http://127.0.0.1:PORT``)."""
    global _server
    with _lock:
        if _server is None:
            _server = ArtifactServer()
        _server.start()
        return f"http://127.0.0.1:{_server.port}"


def artifact_url(artifact_id: str) -> str:
    """Base URL + ``/artifacts/<id>`` (starts the server if needed)."""
    return f"{ensure_server()}/artifacts/{artifact_id}"
