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
            f'<iframe sandbox="allow-scripts allow-popups" '
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


def _index_html() -> str:
    arts = get_registry().list()
    if not arts:
        rows = '<p class="empty">No artifacts yet.</p>'
    else:
        rows = "".join(
            f'<p><a href="/artifacts/{a.id}">&#9670; {_html.escape(a.title)}</a> '
            f'<span class="badge">{_html.escape(a.type)}</span></p>'
            for a in arts
        )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>Artifacts</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        f"<header><span class='glyph'>&#9670;</span><h1>Artifacts</h1></header>"
        f"<main><div class='markdown'>{rows}</div></main></body></html>"
    )


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
