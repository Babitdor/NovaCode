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

MAX_MANUAL_INPUT_BYTES = 64_000

if TYPE_CHECKING:
    from novacode_cli.computer.runtime import Computer

HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nova · Computer</title><style>
:root{color-scheme:dark;--bg:#111923;--surface:#182331;--canvas:#090e15;--line:#2d3d50;--text:#edf3fa;--muted:#9eafc0;--accent:#91b8ff;--warning:#f1cf90;--danger:#efabac;
font:14px/1.6 'Segoe UI Variable','Segoe UI',sans-serif;background:var(--bg);color:var(--text)}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0}::selection{background:var(--accent);color:var(--canvas)}
header{max-width:1600px;margin:auto;padding:28px 32px 24px;display:flex;align-items:center;justify-content:space-between;gap:24px;flex-wrap:wrap}
.identity{display:flex;align-items:center;gap:18px}.brand{font-weight:650;color:var(--accent);border-right:1px solid var(--line);padding-right:18px;font-size:17px}
h1{font:600 24px/1.25 'Segoe UI Variable Display','Segoe UI',sans-serif;letter-spacing:-.025em;margin:0 0 4px}
h2{font-size:15px;line-height:1.4;font-weight:600;margin:0 0 12px}p{margin:0 0 16px}small,.hint{color:var(--muted)}
nav{display:flex;gap:8px;flex-wrap:wrap}button,input{font:inherit;border-radius:7px;min-height:40px}
button{cursor:pointer;padding:8px 14px;border:1px solid var(--line);background:var(--surface);color:var(--text);font-weight:600;transition:background .15s,border-color .15s}
button:hover:not(:disabled){border-color:var(--accent);background:#223449}button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
button:disabled{color:#708296;cursor:default}#stop{color:var(--danger)}#takeover{background:var(--accent);color:var(--canvas);border-color:var(--accent)}
body[data-retained="true"] #stop{color:var(--text)}
main{max-width:1600px;padding:0 32px 24px;margin:auto}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 288px;gap:24px;align-items:start}
.viewer{min-width:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--canvas)}
.viewer-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;background:var(--surface);border-bottom:1px solid var(--line)}
.viewer-label{color:var(--muted);font-size:12px}.status{display:flex;align-items:center;gap:8px}
#state{font:12px/1.5 'Cascadia Code',Consolas,monospace;text-transform:capitalize;color:var(--accent)}
.status:before{content:'';width:6px;height:6px;border-radius:50%;background:var(--accent)}
#notice{padding:10px 16px;color:var(--muted);border-top:1px solid var(--line);font-size:12px;overflow-wrap:anywhere;min-height:40px}
.desktop{min-height:400px;display:grid;place-items:center;overflow:hidden;padding:8px}
#screen{display:block;max-width:100%;width:auto;height:auto;max-height:calc(100dvh - 230px);object-fit:contain}
#empty{max-width:38ch;padding:64px 24px;text-align:center;color:var(--muted)}
body[data-control="manual"] .viewer{border-color:var(--accent)}body[data-control="manual"] #screen{cursor:crosshair}
aside{padding:6px 0}.rail-section{padding:0 0 24px;margin-bottom:24px;border-bottom:1px solid var(--line)}
.rail-section:last-child{border-bottom:0;margin-bottom:0}.control-heading{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
#control-owner{color:var(--accent);font-size:12px}#takeover,#return_control{width:100%;margin-bottom:20px}
#takeover-controls label{display:block;font-weight:600;margin:18px 0 6px}.input-row{display:flex;gap:8px;align-items:start}
input{color:var(--text);background:var(--canvas);border:1px solid var(--line);padding:8px 10px;width:100%;min-width:0}
input:disabled{color:#708296}input::placeholder{color:var(--muted)}.input-row button{flex:none}.field-hint{font-size:12px;color:var(--muted);margin:6px 0 0}
.specs{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px;color:var(--muted)}.specs strong{display:block;color:var(--text);font-weight:500;font-size:14px}
#approval-indicator{color:var(--muted);font-size:13px}#approval-indicator.waiting{color:var(--warning)}
#approval-observation{margin-top:16px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
summary{padding:14px 16px;cursor:pointer;color:var(--warning);font-weight:600}#approval-observation p{padding:0 16px;color:var(--muted);font-size:13px}
#approval-screen{display:block;max-width:100%;height:auto;margin:auto}footer{color:var(--muted);font-size:12px;padding-top:18px;max-width:100ch}
code{font-family:'Cascadia Code',Consolas,monospace;color:var(--text)}
@media(max-width:900px){.workspace{grid-template-columns:minmax(0,1fr)}aside{display:grid;grid-template-columns:1fr 1fr;gap:24px}.desktop{min-height:280px}#screen{max-height:65dvh}}
@media(max-width:600px){header{padding:20px 16px;gap:16px}.identity{gap:12px}.brand{padding-right:12px}h1{font-size:22px}main{padding:0 16px 20px}nav{width:100%}nav button{flex:1;min-height:44px}aside{display:block}.desktop{min-height:230px}.input-row input,.input-row button{min-height:44px}.viewer-label{display:none}}
@media(prefers-reduced-motion:reduce){button{transition:none}}
/* A desktop console: the guest is the focal surface, controls stay quiet. */
:root{--bg:#101720;--surface:#1b2633;--canvas:#080d13;--line:#344252;--text:#eef3f8;--muted:#a7b7c8;--accent:#a9c8fb}
header{padding:22px 32px;max-width:1800px}.brand{font-size:20px;letter-spacing:-.05em}
h1{font-size:21px}header small{font-size:12px}nav button{background:transparent;min-height:36px;font-size:12px}
main{max-width:1800px}.workspace{grid-template-columns:minmax(0,1fr) 276px;gap:28px}
.viewer{border-radius:10px;box-shadow:0 16px 48px #0003}.viewer-bar{padding:10px 16px;background:#1b2633}
.viewer-tools{display:flex;align-items:center;gap:16px}.viewer-tools button{min-height:28px;padding:2px 9px;font-size:12px;background:transparent}
.desktop{height:calc(100dvh - 220px);min-height:360px;padding:18px;background:#080d13}
#screen{max-height:100%;max-width:100%;box-shadow:0 0 0 1px #ffffff14}
.viewer:fullscreen{display:flex;flex-direction:column;border:0;border-radius:0;background:var(--canvas)}
.viewer:fullscreen .desktop{height:auto;min-height:0;flex:1}.viewer:fullscreen #screen{max-height:calc(100dvh - 110px)}
#empty{max-width:42ch}#empty:before{content:'▣';display:block;font-size:36px;line-height:1;margin-bottom:20px;color:var(--accent)}
#notice{background:#141e29;min-height:38px}.rail-section{padding-bottom:22px;margin-bottom:22px}
.control-heading{margin-bottom:8px}.control-heading h2{margin:0}#control-owner{font:11px 'Cascadia Code',Consolas,monospace}
aside p.hint{font-size:13px}#takeover,#return_control{margin:2px 0 8px;min-height:44px}
#return_control{border-color:var(--accent);color:var(--accent)}
.specs{gap:16px 12px}.specs strong{font-family:'Cascadia Code',Consolas,monospace;font-size:12px;margin-top:3px}
#approval-observation{border-left:3px solid var(--warning);border-radius:6px;background:#1b2633}
body[data-connected="false"] .status:before{background:var(--warning)}
body[data-connected="false"] #state{color:var(--warning)}
footer{font-size:11px;padding-top:16px}
@media(min-width:1600px){.desktop{height:calc(100dvh - 230px)}}
@media(max-width:900px){.workspace{grid-template-columns:minmax(0,1fr)}.desktop{height:60dvh;min-height:280px}aside{padding-top:4px}}
@media(max-width:600px){header{padding:18px 16px}main{padding:0 16px 20px}.desktop{height:48dvh;min-height:220px;padding:8px}.viewer-tools{gap:8px}nav button{min-height:44px}.viewer-bar{padding:10px 12px}}
</style></head><body><header><div class="identity"><span class="brand">Nova</span><div>
<h1>Computer</h1><small>Your private Linux workspace.</small></div></div>
<nav aria-label="Computer controls"><button id="pause">Pause input</button>
<button id="resume">Resume input</button><button id="stop">Stop VM</button></nav></header>
<main><div class="workspace"><div class="display-column"><section class="viewer" aria-label="Live computer preview">
<div class="viewer-bar"><div class="status" role="status" aria-live="polite"><span id="state">Connecting</span></div><div class="viewer-tools"><span class="viewer-label">Live desktop</span><button id="fullscreen" aria-label="Enter fullscreen preview">Expand</button></div></div>
<div class="desktop"><img id="screen" alt="Live view of Nova's Linux virtual machine" hidden>
<div id="empty">Connecting to your computer…</div></div><div id="notice" role="status"></div></section>
<details id="approval-observation" hidden><summary>Observation awaiting approval</summary>
<p>This is the exact frame attached to the proposed input. Review it before approving in Nova.</p>
<img id="approval-screen" alt="Exact desktop observation attached to the pending input proposal"></details></div>
<aside aria-label="Computer controls and details"><section id="takeover-controls" class="rail-section" hidden aria-label="Manual computer input">
<div class="control-heading"><h2>Desktop control</h2><span id="control-owner" role="status"></span></div>
<p class="hint">Take control to use the mouse and keyboard. Your teammate waits until you return control.</p>
<button id="takeover">Take Control</button><button id="return_control" hidden>Return Control</button>
<label for="manual-text">Private text</label><div class="input-row">
<input id="manual-text" type="password" autocomplete="off" placeholder="Text to type in the guest">
<button id="send-text">Type</button></div><p class="field-hint">Sent to the guest, kept out of input logs.</p>
<label for="manual-key">Keyboard shortcut</label><div class="input-row">
<input id="manual-key" autocomplete="off" placeholder="e.g. ctrl,l"><button id="send-key">Send</button></div>
<p class="field-hint">Separate key names with commas.</p>
<p class="hint" style="margin-top:20px">Click or scroll on the desktop while you hold control. Return Control, then Resume in Nova. Closing this page leaves the teammate paused.</p></section>
<section class="rail-section"><h2>Workspace</h2><div class="specs"><div>System<strong>Linux</strong></div><div>Memory<strong>4 GiB</strong></div><div>Processor<strong>2 CPUs</strong></div><div>Runtime<strong>Local QEMU</strong></div></div></section>
<section id="approval-section" class="rail-section" hidden><h2>Input approval</h2><p id="approval-indicator">No input waiting for review.</p><p class="hint">Review and approve proposed actions in Nova's Teammates panel.</p></section></aside></div>
<footer>Preview refreshes once per second while this tab is visible. Pause blocks the next agent input;
an action already sent may finish. Stop deletes this temporary VM and its files.
Start another computer from Nova with <code>/computer start</code>.</footer></main>
<script>
const $=id=>document.getElementById(id);let picture=null,approvalPicture=null,busy=false,retained=false,manual=false;
async function request(path,options={}){const r=await fetch(path,options);
if(!r.ok){const error=await r.json();throw new Error(error.error||'Computer unavailable')}return r}
function render(s){document.body.dataset.connected='true';retained=!!s.persistent;manual=!!s.manual;document.body.dataset.control=manual?'manual':'agent';document.body.dataset.retained=String(retained);
$('state').textContent=s.phase+(s.phase==='ready'?(manual?' · you have control':s.paused?' · input paused':''):'');
$('takeover-controls').hidden=!retained;$('resume').hidden=retained;$('stop').textContent=retained?'Suspend':'Stop VM';
$('approval-section').hidden=!retained;
$('approval-indicator').textContent=s.pending_approval?'An input needs your review. Open the saved observation.':'No input waiting for review.';
$('approval-indicator').classList.toggle('waiting',!!s.pending_approval);
if(!s.pending_approval){$('approval-observation').hidden=true;$('approval-observation').open=false;}
if(retained){$('control-owner').textContent=manual?'You hold control':'Teammate';
$('takeover').hidden=manual;$('return_control').hidden=!manual;
$('takeover').disabled=busy||s.manual;$('return_control').disabled=busy||!s.manual;
for(const id of ['manual-text','manual-key','send-text','send-key'])$(id).disabled=busy||!s.manual||s.phase!=='ready';
document.querySelector('footer').textContent='Suspend preserves your files. Desktop input requires approval in Nova.';}
$('notice').textContent=s.error||s.stage+(s.phase==='starting'?' · '+s.elapsed_seconds+'s elapsed':'');
$('pause').disabled=busy||s.phase!=='ready'||s.paused;
$('resume').disabled=busy||s.phase!=='ready'||!s.paused;
$('stop').disabled=busy||s.phase==='stopped'||s.phase==='stopping';
if(s.phase!=='ready'){$('screen').hidden=true;$('empty').hidden=false;
$('empty').textContent=s.phase==='starting'?'Preparing your Linux workspace. The first start may take a few minutes.':s.phase==='suspended'?'Your workspace is suspended. Take Control or resume a task in Nova to wake it.':s.phase==='stopped'?'Your computer is offline. Start it from Nova with /computer start.':'Computer '+s.phase}}
async function tick(){try{if(!document.hidden){const s=await(await request('/status')).json();render(s);
if(s.pending_approval){const blob=await(await request('/approval_frame')).blob();const next=URL.createObjectURL(blob);
$('approval-screen').src=next;$('approval-observation').hidden=false;if(approvalPicture)URL.revokeObjectURL(approvalPicture);approvalPicture=next}
else{$('approval-observation').hidden=true}
if(s.phase==='ready'){const blob=await(await request('/frame')).blob();const next=URL.createObjectURL(blob);
$('screen').src=next;$('screen').hidden=false;$('empty').hidden=true;if(picture)URL.revokeObjectURL(picture);picture=next}
}}catch(e){manual=false;document.body.dataset.connected='false';document.body.dataset.control='agent';$('state').textContent='Preview disconnected';$('notice').textContent=e.message+' · Reconnecting automatically…';
for(const id of ['pause','resume','stop','takeover','return_control','manual-text','manual-key','send-text','send-key'])$(id).disabled=true;}
finally{setTimeout(tick,1000)}}
for(const action of ['pause','resume','stop','takeover','return_control'])$(action).onclick=async()=>{
if(busy)return;
if(action==='stop'&&!retained&&!confirm('Stop and delete this temporary computer and its files?'))return;
busy=true;for(const id of ['pause','resume','stop','takeover','return_control'])$(id).disabled=true;
try{const s=await(await request('/'+action,{method:'POST',headers:{'X-Nova-Request':'1'}})).json();
busy=false;render(s)}catch(e){busy=false;$('notice').textContent=e.message}};
async function input(args){if(!manual||busy)return;busy=true;
try{render(await(await request('/manual_action',{method:'POST',headers:{'X-Nova-Request':'1','Content-Type':'application/json'},body:JSON.stringify(args)})).json())}
catch(e){$('notice').textContent=e.message}finally{busy=false}}
$('screen').onclick=e=>{if(!manual)return;const r=$('screen').getBoundingClientRect();
input({action:'click',frame_width:$('screen').naturalWidth,frame_height:$('screen').naturalHeight,x:Math.min($('screen').naturalWidth-1,Math.floor((e.clientX-r.left)*$('screen').naturalWidth/r.width)),
y:Math.min($('screen').naturalHeight-1,Math.floor((e.clientY-r.top)*$('screen').naturalHeight/r.height))})};
$('screen').addEventListener('wheel',e=>{if(!manual)return;e.preventDefault();input({action:'scroll',dy:Math.sign(e.deltaY)*3})},{passive:false});
$('send-text').onclick=()=>{const text=$('manual-text').value;$('manual-text').value='';if(text)input({action:'type',text})};
$('send-key').onclick=()=>{const keys=$('manual-key').value.split(',').map(k=>k.trim()).filter(Boolean);$('manual-key').value='';if(keys.length)input({action:'key',keys})};
$('fullscreen').hidden=!document.fullscreenEnabled;
$('fullscreen').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.querySelector('.viewer').requestFullscreen()}catch(e){$('notice').textContent='Fullscreen unavailable. Use the fullscreen command in your browser.'}};
document.addEventListener('fullscreenchange',()=>{const expanded=!!document.fullscreenElement;$('fullscreen').textContent=expanded?'Exit fullscreen':'Expand';$('fullscreen').setAttribute('aria-label',expanded?'Exit fullscreen preview':'Enter fullscreen preview')});
for(const [field,button] of [['manual-text','send-text'],['manual-key','send-key']])$(field).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$(button).click()}});
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
                elif self.path in {"/status", "/frame", "/approval_frame"}:
                    self.operate(self.path[1:])
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                if self.path in {"/pause", "/resume", "/stop", "/takeover", "/return_control"}:
                    self.operate(self.path[1:])
                elif self.path == "/manual_action":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= MAX_MANUAL_INPUT_BYTES:
                            message = "Invalid input size"
                            raise ValueError(message)  # noqa: TRY301
                        arguments = json.loads(self.rfile.read(length))
                        if not isinstance(arguments, dict):
                            message = "Expected input object"
                            raise TypeError(message)  # noqa: TRY301
                        self.operate("manual_action", arguments)
                    except (ValueError, TypeError, OSError):
                        self.respond(400, b'{"error":"Invalid manual input"}', "application/json")
                else:
                    self.send_error(404)

            def operate(self, operation: str, arguments: dict | None = None) -> None:
                try:
                    result = owner.call_sync(operation, **(arguments or {}))
                    if operation in {"frame", "approval_frame"}:
                        self.respond(200, result, "image/png")
                    else:
                        self.respond(200, json.dumps(result).encode(), "application/json")
                except Exception as exc:  # noqa: BLE001 - Return a usable viewer error.
                    message = (
                        "Manual input failed; inspect desktop"
                        if operation == "manual_action"
                        else str(exc)
                    )
                    self.respond(409, json.dumps({"error": message}).encode(), "application/json")

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
