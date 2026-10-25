"""A private, demand-driven desktop preview on loopback."""
# ruff: noqa: E501
# Standalone HTML/JS lives inline, matching Nova's other loopback viewers.

from __future__ import annotations

import json
import secrets
import threading
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from novacode_cli.security.local_http import LocalRequestHandler, secure_html

if TYPE_CHECKING:
    from novacode_cli.computer.runtime import Computer

HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nova · Computer</title><style>
:root{color-scheme:dark;font:15px/1.5 system-ui,sans-serif;background:#101313;color:#e6e9e5}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0}header{padding:20px 28px;border-bottom:1px solid #35403b;
display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:600}small{color:#a4b3aa}.badge{font:12px ui-monospace,monospace;
letter-spacing:.08em;color:#bce8bf}nav{display:flex;gap:8px}button{font:inherit;cursor:pointer;
border:1px solid #57695e;background:transparent;color:inherit;border-radius:5px;padding:7px 14px}
button:hover,button:focus-visible{background:#293c30;outline:2px solid #bce8bf;outline-offset:2px}
button:disabled{opacity:.4;cursor:default}#stop{color:#ffc3ae;border-color:#865b4c}
main{max-width:1440px;padding:24px;margin:auto}.status{display:flex;align-items:center;gap:12px;
margin-bottom:16px}#state{font:13px ui-monospace,monospace;color:#bce8bf;text-transform:uppercase}
#notice{color:#b6c1ba}.desktop{background:#070909;border:1px solid #35403b;min-height:50vh;
display:flex;align-items:center;justify-content:center;overflow:auto;border-radius:6px}
#screen{display:block;max-width:100%;height:auto}#empty{padding:40px;text-align:center;color:#a4b3aa}
footer{font-size:13px;color:#a4b3aa;padding-top:16px}code{color:#d6e8db}
@media(max-width:600px){header{padding:16px}main{padding:12px}button{padding:8px 12px}}
</style></head><body><header><div><div class="badge">NOVA / LOCAL COMPUTER</div>
<h1>A separate Linux desktop</h1><small>QEMU virtual machine · 2 CPUs · 4 GiB RAM</small></div>
<nav aria-label="Computer controls"><button id="pause">Pause input</button>
<button id="resume">Resume input</button><button id="stop">Stop VM</button></nav></header>
<main><div class="status" role="status" aria-live="polite"><span id="state">Connecting</span>
<span id="notice"></span></div><div class="desktop"><img id="screen" alt="Live view of Nova's Linux virtual machine" hidden>
<div id="empty">Waiting for the computer…</div></div>
<footer>Preview refreshes once per second while this tab is visible. Pause blocks the next agent input;
an action already sent may finish. Stop deletes this temporary VM and its files.
Start another computer from Nova with <code>/computer start</code>.</footer></main>
<script>
const $=id=>document.getElementById(id);let picture=null,busy=false;
async function request(path,options={}){const r=await fetch(path,options);
if(!r.ok){const error=await r.json();throw new Error(error.error||'Computer unavailable')}return r}
function render(s){$('state').textContent=s.phase+(s.paused&&s.phase==='ready'?' · input paused':'');
$('notice').textContent=s.error||s.stage+(s.phase==='starting'?' · '+s.elapsed_seconds+'s elapsed':'');
$('pause').disabled=busy||s.phase!=='ready'||s.paused;
$('resume').disabled=busy||s.phase!=='ready'||!s.paused;
$('stop').disabled=busy||s.phase==='stopped'||s.phase==='stopping';
if(s.phase!=='ready'){$('screen').hidden=true;$('empty').hidden=false;
$('empty').textContent=s.phase==='starting'?'Preparing the Linux desktop. First startup downloads the guest image.':'Computer '+s.phase}}
async function tick(){try{if(!document.hidden){const s=await(await request('/status')).json();render(s);
if(s.phase==='ready'){const blob=await(await request('/frame')).blob();const next=URL.createObjectURL(blob);
$('screen').src=next;$('screen').hidden=false;$('empty').hidden=true;if(picture)URL.revokeObjectURL(picture);picture=next}
}}catch(e){$('state').textContent='Preview disconnected';$('notice').textContent=e.message}
finally{setTimeout(tick,1000)}}
for(const action of ['pause','resume','stop'])$(action).onclick=async()=>{
if(action==='stop'&&!confirm('Stop and delete this temporary computer and its files?'))return;
busy=true;for(const id of ['pause','resume','stop'])$(id).disabled=true;
try{const s=await(await request('/'+action,{method:'POST',headers:{'X-Nova-Request':'1'}})).json();
busy=false;render(s)}catch(e){busy=false;$('notice').textContent=e.message}};
tick();
</script></body></html>"""


class ComputerPreview:
    """One authenticated viewer for one session; never exposed through artifacts."""

    def __init__(self, computer: Computer) -> None:
        """Bind an authenticated listener directly to an available loopback port."""
        owner = computer

        class Handler(LocalRequestHandler):
            def log_message(self, *_args: object) -> None:
                # Browser authorization tokens must not enter access logs.
                pass

            def respond(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    (
                        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                        "img-src blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
                    ),
                )
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                if self.path == "/":
                    self.respond(200, secure_html(HTML).encode(), "text/html; charset=utf-8")
                elif self.path in {"/status", "/frame"}:
                    self.operate(self.path[1:])
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                if self.path in {"/pause", "/resume", "/stop"}:
                    self.operate(self.path[1:])
                else:
                    self.send_error(404)

            def operate(self, operation: str) -> None:
                try:
                    result = owner.call_sync(operation)
                    if operation == "frame":
                        self.respond(200, result, "image/png")
                    else:
                        self.respond(200, json.dumps(result).encode(), "application/json")
                except Exception as exc:  # noqa: BLE001 - Return a usable viewer error.
                    self.respond(409, json.dumps({"error": str(exc)}).encode(), "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        token = secrets.token_urlsafe(32)
        self._server.auth_token = token  # type: ignore[attr-defined]  # LocalRequestHandler contract.
        query = urlencode({"token": token})
        self.url = f"http://127.0.0.1:{self._server.server_port}/?{query}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="nova-computer-preview"
        )
        self._thread.start()

    def close(self) -> None:
        """Release the viewer's listener."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
