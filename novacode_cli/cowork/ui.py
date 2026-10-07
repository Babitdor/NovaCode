"""The Nova Cowork single-page desktop UI (served by the FastAPI server).

Self-contained HTML/CSS/JS styled after the Nova TUI: tokyo-night palette,
matrix-rain banner with the NOVA ASCII logo, accent-bar message cards, and a
prompt-dock composer with the ``>`` chevron. Talks to the existing server:
- POST /sessions + WS /ws/{id} for the agent chat (reused as-is).
- /api/workspace[/grant|/revoke] + /api/authorize for the WorkspacePolicy broker.

Default-deny: the chat is disabled until at least one folder is granted.
"""

from __future__ import annotations


def render_cowork_html(token: str = "") -> str:
    """Render the SPA with the IPC token embedded so its fetch/WebSocket calls
    can authenticate against the token-gated server routes."""
    return _HTML.replace("__COWORK_TOKEN__", token)


_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nova Cowork</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700;800&display=swap" rel="stylesheet">
<style>
 :root{ --bg:#13141d; --surface:#1a1b26; --panel:#24283b; --boost:#2f3346;
        --border:#3b4261; --fg:#c0caf5; --muted:#565f89;
        --primary:#7aa2f7; --secondary:#9ece6a; --accent:#bb9af7;
        --success:#73daca; --warning:#e0af68; --error:#f7768e; }
 *{box-sizing:border-box;margin:0;padding:0}
 html,body{height:100%}
 body{background:var(--bg);color:var(--fg);
   font:14px/1.55 "JetBrains Mono",ui-monospace,"Cascadia Code","SF Mono",monospace;
   display:flex;flex-direction:column;overflow:hidden}
 ::selection{background:var(--primary);color:#0f0f16}
 ::-webkit-scrollbar{width:8px;height:8px}
 ::-webkit-scrollbar-track{background:var(--bg)}
 ::-webkit-scrollbar-thumb{background:var(--border)}
 ::-webkit-scrollbar-thumb:hover{background:var(--muted)}

 /* --- matrix-rain banner --- */
 .banner{position:relative;flex:0 0 auto;height:clamp(140px,16vw,200px);overflow:hidden;
   border-bottom:1px solid var(--border);background:var(--bg);animation:fadein .5s ease both}
 #rain{position:absolute;inset:0;width:100%;height:100%}
 .logo{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
   padding-bottom:20px;font-size:clamp(5px,1.05vw,12px);line-height:1.3;white-space:pre;
   color:var(--primary);text-shadow:0 0 16px rgba(122,162,247,.45);
   user-select:none;pointer-events:none;overflow:hidden}
 .bootline{position:absolute;left:0;right:0;bottom:8px;text-align:center;
   font-size:11px;color:var(--muted);letter-spacing:.06em;min-height:16px}
 .bootline .ok{color:var(--success)}

 /* --- header (TUI info bar) --- */
 header{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--surface);
   border-bottom:1px solid var(--border);flex:0 0 auto;animation:rise .35s ease both}
 .mark{color:var(--primary);font-weight:800;letter-spacing:.02em;white-space:nowrap}
 .mode{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
   color:#b4c6ef;background:#161f33;border:1px solid var(--primary);padding:2px 8px;white-space:nowrap}
 .state{font-size:11px;padding:2px 8px;border:1px solid;white-space:nowrap}
 .state.ok{color:var(--success);border-color:var(--success)}
 .state.deny{color:var(--warning);border-color:var(--warning)}
 .run-badge{display:none;font-size:11px;padding:2px 8px;border:1px solid var(--accent);
   color:var(--accent);white-space:nowrap;animation:pulse 1.6s ease-in-out infinite}
 .run-badge.show{display:inline-block}
 .sid{margin-left:auto;color:var(--muted);font-size:11px;white-space:nowrap}

 /* --- 3-column main (collapsible sidebars) --- */
 .main{flex:1;display:grid;grid-template-columns:300px 1fr 340px;min-height:0;
   transition:grid-template-columns .2s ease}
 .main.collapsed-workspace{grid-template-columns:44px 1fr 340px}
 .main.collapsed-activity{grid-template-columns:300px 1fr 44px}
 .main.collapsed-workspace.collapsed-activity{grid-template-columns:44px 1fr 44px}
 .col{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--border);
   animation:rise .4s ease both}
 .col:last-child{border-right:0;border-left:1px solid var(--border)}
 .col:nth-child(1){animation-delay:.05s}
 .col:nth-child(2){animation-delay:.12s}
 .col:nth-child(3){animation-delay:.19s}
 .col-head{display:flex;align-items:center;flex:0 0 auto;border-bottom:1px solid var(--border);
   background:var(--surface)}
 .col h2{flex:1;font-size:10px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
   color:var(--muted);padding:10px 14px 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .col h2::before{content:"▮";margin-right:7px}
 #col-workspace h2::before{color:var(--secondary)}
 #col-chat h2::before{color:var(--primary)}
 #col-activity h2::before{color:var(--accent)}
 .col-head .clear-btn{background:transparent;border:0;color:var(--muted);font:inherit;font-size:10px;
   text-transform:uppercase;letter-spacing:.1em;padding:0 8px;cursor:pointer;height:100%;white-space:nowrap}
 .col-head .clear-btn:hover{color:var(--fg)}
 .collapse-btn{background:transparent;border:0;color:var(--muted);font:inherit;font-size:11px;
   padding:0 10px;cursor:pointer;height:100%;align-self:stretch}
 .collapse-btn:hover{color:var(--fg)}
 .col-body{display:flex;flex-direction:column;flex:1;min-height:0;position:relative}
 .rail-label{display:none;flex-direction:column;align-items:center;justify-content:center;gap:12px;
   flex:1;cursor:pointer;color:var(--muted);user-select:none;transition:color .15s}
 .rail-label:hover{color:var(--primary)}
 .rail-ic{font-size:12px}
 #col-workspace .rail-ic{color:var(--secondary)}
 #col-activity .rail-ic{color:var(--accent)}
 .rail-txt{writing-mode:vertical-rl;font-size:10px;letter-spacing:.2em;text-transform:uppercase}
 .col.rail .col-body{display:none}
 .col.rail .col-head{display:none}
 .col.rail .rail-label{display:flex}
 .scroll{overflow-y:auto;flex:1;padding:12px 14px 16px}

 /* --- workspace grants --- */
 .picker{padding:12px;border-bottom:1px solid var(--border);background:var(--surface)}
 .picker label{display:block;font-size:10px;color:var(--muted);text-transform:uppercase;
   letter-spacing:.12em;margin-bottom:6px}
 .picker input[type=text]{width:100%;background:var(--panel);border:1px solid var(--border);color:var(--fg);
   font:inherit;font-size:12px;padding:8px 10px;outline:none;transition:border-color .15s,background .15s}
 .picker input[type=text]:focus{border-color:var(--primary);background:var(--boost)}
 .perms{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
 .perms label{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted);
   border:1px solid var(--border);padding:3px 8px;cursor:pointer;user-select:none;
   transition:color .15s,border-color .15s}
 .perms label:has(input:checked){color:var(--success);border-color:var(--success)}
 .perms input{accent-color:var(--success);margin:0}
 .picker button{margin-top:8px;width:100%;background:var(--primary);color:#0f0f16;border:0;
   font:inherit;font-weight:700;font-size:12px;padding:8px;cursor:pointer;letter-spacing:.06em;
   transition:background .15s}
 .picker button:hover{background:#8fb4ff}
 .picker button:active{transform:translateY(1px)}
 .deny-note{display:none;align-items:center;gap:8px;color:var(--warning);font-size:11px;
   padding:9px 12px;border-bottom:1px solid var(--border);background:#241b1b}
 .grant{position:relative;background:var(--surface);border:1px solid var(--border);
   border-left:3px solid var(--secondary);padding:10px 12px;margin:8px 0;animation:rise .3s ease both}
 .grant .p{word-break:break-all;font-size:12px;padding-right:56px}
 .grant .meta{margin-top:6px;display:flex;gap:4px;flex-wrap:wrap}
 .grant .created{color:var(--muted);font-size:10px;margin-top:5px}
 .chip{font-size:10px;padding:1px 6px;border:1px solid var(--border);color:var(--muted)}
 .chip.on{color:var(--success);border-color:var(--success)}
 .chip.rec{color:var(--accent);border-color:var(--accent)}
 .grant button{position:absolute;top:8px;right:8px;background:transparent;border:1px solid var(--error);
   color:var(--error);font:inherit;font-size:10px;padding:1px 8px;cursor:pointer;
   text-transform:uppercase;letter-spacing:.08em;transition:background .15s,color .15s}
 .grant button:hover{background:var(--error);color:#0f0f16}
 .agent-root{background:var(--boost);border:1px solid var(--border);border-left:3px solid var(--accent);
   padding:8px 12px;margin:8px 0;font-size:11px}
 .agent-root .lbl{display:block;font-size:9px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
   color:var(--accent);margin-bottom:3px}
 .agent-root .p{word-break:break-all;color:var(--fg)}

 /* --- chat --- */
 #col-chat{position:relative}
 .chat-wrap{position:relative;flex:1;min-height:0;display:flex}
 #chat{flex:1;overflow-y:auto;padding:16px}
 .empty{color:var(--muted);font-size:12px;text-align:center;padding:24px 12px;line-height:1.7}
 #chat-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
   pointer-events:none;z-index:1}
 #activity-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
   pointer-events:none;z-index:1}
 .msg{position:relative;display:flex;flex-direction:column;background:var(--surface);border-left:3px solid;
   padding:10px 14px;margin:0 0 12px;animation:rise .25s ease both}
 .msg.user{border-color:var(--primary)}
 .msg.nova{border-color:var(--success)}
 .msg.sys{border-color:var(--accent)}
 .mhead{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px}
 .mright{display:flex;align-items:center;gap:8px}
 .msg .who{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
   color:var(--muted)}
 .msg.user .who{color:var(--primary)}
 .msg.nova .who{color:var(--success)}
 .msg.sys .who{color:var(--accent)}
 .msg.sys .bubble{font-size:12.5px}
 .msg.sys code{background:var(--panel);padding:1px 4px;border-radius:3px}
 .mhead .ts{color:var(--muted);font-size:10px}
 .copy-btn{background:transparent;border:0;color:var(--muted);font:inherit;font-size:11px;padding:0;
   cursor:pointer;opacity:.6;transition:opacity .15s,color .15s}
 .copy-btn:hover{opacity:1;color:var(--fg)}
 .bubble{white-space:pre-wrap;word-break:break-word;font-size:13px}
 .bubble.rendered{white-space:normal}
 .bubble.rendered p{margin:0 0 8px}
 .bubble.rendered p:last-child{margin-bottom:0}
 .bubble.rendered h1,.bubble.rendered h2,.bubble.rendered h3,.bubble.rendered h4{margin:10px 0 6px;color:var(--primary);letter-spacing:.04em}
 .bubble.rendered h1{font-size:1.15em}
 .bubble.rendered h2{font-size:1.08em}
 .bubble.rendered h3,.bubble.rendered h4{font-size:1em}
 .bubble.rendered ul,.bubble.rendered ol{margin:0 0 8px;padding-left:22px}
 .bubble.rendered li{margin:2px 0}
 .bubble.rendered code{background:var(--boost);border:1px solid var(--border);padding:1px 5px;font-size:.92em;color:var(--secondary)}
 .bubble.rendered pre.md-code{position:relative;background:var(--bg);border:1px solid var(--border);border-left:3px solid var(--accent);padding:10px 12px;margin:8px 0;overflow-x:auto}
 .bubble.rendered pre.md-code code{background:none;border:0;padding:0;color:var(--fg);display:block;white-space:pre}
 .code-copy{position:absolute;top:4px;right:4px;background:var(--panel);border:1px solid var(--border);
   color:var(--muted);font:inherit;font-size:10px;padding:1px 6px;cursor:pointer;opacity:0;
   transition:opacity .15s}
 .bubble.rendered pre.md-code:hover .code-copy{opacity:1}
 .bubble.rendered a{color:var(--primary);text-decoration:underline}
 .bubble.rendered blockquote{border-left:3px solid var(--border);padding:2px 10px;margin:8px 0;color:var(--muted)}
 .bubble.rendered hr{border:0;border-top:1px solid var(--border);margin:10px 0}
 .bubble.rendered del{color:var(--muted)}
 .cursor{display:inline-block;width:.6em;height:1.05em;background:var(--success);
   vertical-align:text-bottom;margin-left:2px;animation:blink 1s steps(1) infinite}
 .thinking{display:inline-flex;gap:4px;padding:2px 0}
 .thinking .dot{width:6px;height:6px;background:var(--success);animation:think 1.2s ease-in-out infinite}
 .thinking .dot:nth-child(2){animation-delay:.15s}
 .thinking .dot:nth-child(3){animation-delay:.3s}
 #deny-overlay{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;
   justify-content:center;gap:14px;background:rgba(19,20,29,.93);z-index:5;text-align:center;padding:24px}
 #deny-overlay .lock{font-size:42px;color:var(--error);text-shadow:0 0 28px rgba(247,118,142,.55);
   animation:pulse 2.4s ease-in-out infinite}
 #deny-overlay .big{font-size:19px;font-weight:800;letter-spacing:.22em;color:var(--error)}
 #deny-overlay .sub{color:var(--muted);font-size:12px;max-width:340px;line-height:1.8}
 .jump{position:absolute;bottom:70px;left:50%;transform:translateX(-50%);display:none;z-index:4;
   background:var(--panel);border:1px solid var(--primary);color:var(--primary);font:inherit;
   font-size:11px;padding:4px 12px;cursor:pointer;transition:background .15s,color .15s}
 .jump.show{display:block}
 .jump:hover{background:var(--primary);color:#0f0f16}

 /* --- question dialog (ask_user_question interrupt) --- */
 .qmodal{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;
   justify-content:center;background:rgba(19,20,29,.93);z-index:6;padding:24px}
 .qmodal.show{display:flex}
 .qbox{width:min(560px,94%);max-height:82%;overflow-y:auto;background:var(--surface);
   border:1px solid var(--primary);border-left:4px solid var(--accent);padding:18px 20px;
   animation:rise .25s ease both}
 .qbox .q{font-size:14px;font-weight:700;color:var(--fg);white-space:pre-wrap;margin-bottom:4px}
 .qbox .qc{color:var(--muted);font-size:12px;white-space:pre-wrap;margin-bottom:10px}
 .qopts{display:flex;flex-direction:column;gap:8px;margin:12px 0}
 .qopt{text-align:left;background:var(--panel);border:1px solid var(--border);color:var(--fg);
   font:inherit;font-size:13px;padding:9px 12px;cursor:pointer;transition:border-color .15s,background .15s}
 .qopt:hover{border-color:var(--primary);background:var(--boost)}
 .qopt.sel{border-color:var(--success);background:var(--boost)}
 .qfree{display:flex;gap:8px;margin-top:4px}
 .qfree input{flex:1;background:var(--panel);border:1px solid var(--border);color:var(--fg);
   font:inherit;font-size:13px;padding:9px 12px;outline:none;transition:border-color .15s,background .15s}
 .qfree input:focus{border-color:var(--primary);background:var(--boost)}
 .qfree button{background:var(--primary);color:#0f0f16;border:0;font:inherit;font-weight:700;
   font-size:12px;padding:0 16px;cursor:pointer;letter-spacing:.06em;transition:background .15s}
 .qfree button:hover{background:#8fb4ff}
 .qskip{margin-top:10px;background:transparent;border:1px solid var(--border);color:var(--muted);
   font:inherit;font-size:11px;padding:4px 10px;cursor:pointer;text-transform:uppercase;
   letter-spacing:.08em;transition:color .15s,border-color .15s}
 .qskip:hover{color:var(--fg);border-color:var(--muted)}

 /* --- composer (TUI prompt dock) --- */
 .composer{display:flex;align-items:stretch;border-top:1px solid var(--border);
   background:var(--panel);flex:0 0 auto}
 .mode-badge{display:flex;align-items:center;padding:0 10px;font-size:10px;font-weight:700;
   letter-spacing:.14em;text-transform:uppercase;color:#b4c6ef;background:#161f33;
   border-right:1px solid var(--primary)}
 .prefix{display:flex;align-items:center;padding:0 10px;color:var(--accent);font-weight:800;
   font-size:16px;background:var(--panel);user-select:none}
 .composer textarea{flex:1;resize:none;height:56px;background:var(--panel);border:0;color:var(--fg);
   font:inherit;font-size:13px;padding:16px 12px;outline:none;transition:background .2s}
 .composer textarea:focus{background:var(--boost)}
 .composer textarea:disabled{opacity:.45}
 .composer button{background:var(--primary);color:#0f0f16;border:0;font:inherit;font-weight:700;
   font-size:12px;padding:0 18px;cursor:pointer;letter-spacing:.06em;transition:background .15s}
 .composer button:hover:not(:disabled){background:#8fb4ff}
 .composer button:disabled{opacity:.4;cursor:not-allowed}
 .composer button.stop{background:var(--error);display:none}
 .composer button.stop:hover:not(:disabled){background:#ff9ab0}
 .composer.running button.stop{display:block}
 .composer.running button#send{display:none}

 /* --- activity --- */
 .act{background:var(--surface);border-left:3px solid var(--border);padding:6px 10px;
   margin:6px 0;font-size:12px;animation:rise .3s ease both}
 .act.run{border-color:var(--accent)}
 .act.done{border-color:var(--success)}
 .act.warn{border-color:var(--warning)}
 .act.err{border-color:var(--error)}
 .act-head{display:flex;align-items:center;gap:6px}
 .act-head .chev{color:var(--muted);font-size:9px;transition:transform .15s}
 .act:not(.open) .chev{transform:rotate(-90deg)}
 .act .t{color:var(--muted);font-size:10px;margin-left:auto}
 .act .ic{margin-right:2px}
 .act.run .ic{color:var(--accent)}
 .act.done .ic{color:var(--success)}
 .act.warn .ic{color:var(--warning)}
 .act.err .ic{color:var(--error)}
 .act pre{display:none;margin:6px 0 0;background:var(--bg);border:1px solid var(--border);padding:6px 8px;
   overflow:auto;max-height:140px;font:inherit;font-size:11px;color:var(--muted);
   white-space:pre-wrap;word-break:break-word}
 .act.open pre{display:block}

 /* --- todo list (live agent checklist) --- */
 .todos-panel{display:none;flex-direction:column;flex:0 0 auto;max-height:36%;
   border-bottom:1px solid var(--border);background:var(--surface)}
 .todos-head{display:flex;align-items:center;gap:8px;font-size:10px;font-weight:700;
   letter-spacing:.16em;text-transform:uppercase;color:var(--accent);padding:9px 14px 7px;
   border-bottom:1px solid var(--border)}
 .todos-head .clear-btn{margin-left:auto;background:transparent;border:0;color:var(--muted);
   font:inherit;font-size:10px;text-transform:uppercase;letter-spacing:.1em;padding:0 4px;
   cursor:pointer;height:100%}
 .todos-head .clear-btn:hover{color:var(--fg)}
 .todos-list{overflow-y:auto;padding:8px 12px}
 .todo{display:flex;gap:8px;align-items:baseline;font-size:12px;padding:2px 0;animation:rise .2s ease both}
 .todo .g{flex:0 0 auto}
 .todo.pend .g{color:var(--muted)}
 .todo.run .g{color:var(--warning)}
 .todo.done .g{color:var(--success)}
 .todo.done .c{color:var(--muted);text-decoration:line-through}

 /* --- status bar --- */
 .statusbar{display:flex;align-items:center;gap:16px;padding:5px 14px;background:var(--surface);
   border-top:1px solid var(--border);font-size:11px;color:var(--muted);flex:0 0 auto;
   animation:rise .4s ease both;animation-delay:.24s}
 .statusbar .dot.on{color:var(--success)}
 .statusbar .dot.off{color:var(--muted)}
 .statusbar .spacer{flex:1}
 .reconnect{background:transparent;border:1px solid var(--warning);color:var(--warning);font:inherit;
   font-size:11px;padding:1px 8px;cursor:pointer;transition:background .15s,color .15s}
 .reconnect:hover{background:var(--warning);color:#0f0f16}

 /* --- atmosphere --- */
 .scanlines{position:fixed;inset:0;pointer-events:none;z-index:9998;opacity:.5;
   background:repeating-linear-gradient(0deg,rgba(0,0,0,.14) 0 1px,transparent 1px 3px)}
 .vignette{position:fixed;inset:0;pointer-events:none;z-index:9997;
   background:radial-gradient(ellipse at center,transparent 58%,rgba(0,0,0,.38))}

 /* --- motion --- */
 @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
 @keyframes fadein{from{opacity:0}to{opacity:1}}
 @keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}
 @keyframes think{0%,100%{opacity:.2;transform:translateY(0)}50%{opacity:1;transform:translateY(-3px)}}
 @keyframes pulse{0%,100%{opacity:.75}50%{opacity:1}}
 @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

 /* --- responsive --- */
 @media (max-width:1100px){
   .main{grid-template-columns:260px 1fr 280px}
   .main.collapsed-workspace{grid-template-columns:44px 1fr 280px}
   .main.collapsed-activity{grid-template-columns:260px 1fr 44px}
   .main.collapsed-workspace.collapsed-activity{grid-template-columns:44px 1fr 44px}
 }
 @media (max-width:900px){
   body{overflow:auto}
   .main{grid-template-columns:1fr;min-height:auto}
   .col{min-height:52vh;border-right:0;border-bottom:1px solid var(--border)}
   .col:last-child{border-left:0}
   #col-chat{order:-1}
   .col.rail{min-height:44px}
   .col.rail .rail-label{flex-direction:row;align-items:center;justify-content:center;gap:8px;padding:0}
   .rail-txt{writing-mode:horizontal-tb}
 }
</style></head>
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
⣿⣿⠀⠈⠀⠀⣿⣿⣿⡿⣿⠿⢿⣿⣿⣿⣿⣿⣿⣧⡀⠀⢄⠲⠀⠀⠀⣱      ~ Secrets, Locks, Firewalls
⣿⣿⡆⠀⠀⠀⠈⣿⣿⣷⣶⣼⣾⣿⣿⣿⣿⣿⣿⣿⣷⠂⠀⠀⠂⢀⢲         Everything has a weakness.
⣿⣿⣿⡆⠀⠀⠀⠙⣿⠋⠠⠄⢀⠉⣹⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⣿          The right code just knows where to look.
⣿⣿⣿⣿⣦⠀⠀⠀⠘⣿⣤⣤⣶⣿⣿⣿⣿⣿⠟⣛⡽⠀⠀⠀⠠⣸            ♥︎ NOVA ~
⣿⣿⣿⣿⣿⣷⡀⠀⠀⠈⠻⣿⣿⣿⠿⠛⠋⠐⠚⠛⠃   ⣰⣿</pre>
  <div class="bootline"><span class="ok">✓</span> <span id="boot"></span></div>
</div>
<header>
  <span class="mark">◆ NOVA COWORK</span>
  <span class="mode">cowork</span>
  <span id="run-badge" class="run-badge">● running</span>
  <span id="ws-state" class="state deny">workspace: deny-all</span>
  <span class="sid" id="sid"></span>
</header>
<div class="main">
  <div class="col" id="col-workspace">
    <div class="col-head">
      <h2>Workspace &amp; Permissions</h2>
      <button class="collapse-btn" data-col="workspace" title="Collapse">◀</button>
    </div>
    <div class="col-body">
      <div class="picker">
        <label for="path">grant a folder</label>
        <input type="text" id="path" placeholder="/absolute/path/to/folder" autocomplete="off" spellcheck="false" />
        <div class="perms">
          <label><input type="checkbox" id="perm-r" checked> r</label>
          <label><input type="checkbox" id="perm-w" checked> w</label>
          <label><input type="checkbox" id="perm-x" checked> x</label>
          <label><input type="checkbox" id="perm-rec" checked> recursive</label>
        </div>
        <button id="grant">Grant access</button>
      </div>
      <div id="deny" class="deny-note">⛔ No folder granted — access is denied everywhere.</div>
      <div class="scroll" id="grants"></div>
    </div>
    <div class="rail-label" data-col="workspace" title="Expand">
      <span class="rail-ic">▮</span>
      <span class="rail-txt">Workspace</span>
    </div>
  </div>
  <div class="col" id="col-chat">
    <div class="col-head">
      <h2>Chat</h2>
      <button class="clear-btn" id="new-chat" title="Start a new session">New</button>
      <button class="clear-btn" id="clear-chat" title="Clear messages">Clear</button>
    </div>
    <div class="chat-wrap">
      <div id="chat"></div>
      <div id="chat-empty" class="empty">▸ send a message to start — the agent works inside your granted folders</div>
    </div>
    <div id="deny-overlay">
      <div class="lock">🔒</div>
      <div class="big">ACCESS DENIED</div>
      <div class="sub">Grant a workspace folder to start chatting. The agent is confined to granted folders only, and access is denied everywhere else until you grant one.</div>
    </div>
    <div id="qmodal" class="qmodal">
      <div class="qbox">
        <div class="q" id="q-text"></div>
        <div class="qc" id="q-context" style="display:none"></div>
        <div class="qopts" id="q-opts"></div>
        <div class="qfree">
          <input type="text" id="q-input" placeholder="Type your answer…" autocomplete="off" spellcheck="false" />
          <button id="q-submit">Answer</button>
        </div>
        <button class="qskip" id="q-skip">skip</button>
      </div>
    </div>
    <button id="jump" class="jump">↓ latest</button>
    <div class="composer" id="composer">
      <span class="mode-badge">cowork</span>
      <span class="prefix">&gt;</span>
      <textarea id="in" placeholder="Message Nova…" disabled></textarea>
      <button id="send" disabled>Send</button>
      <button id="stop" class="stop" title="Stop the running agent">■ Stop</button>
    </div>
  </div>
  <div class="col" id="col-activity">
    <div class="col-head">
      <h2>Activity</h2>
      <button class="clear-btn" id="clear-activity" title="Clear activity">Clear</button>
      <button class="collapse-btn" data-col="activity" title="Collapse">▶</button>
    </div>
    <div class="col-body">
      <div id="todos-panel" class="todos-panel">
        <div class="todos-head">▦ Todos<button class="clear-btn" id="todos-clear" title="Hide todos">✕</button></div>
        <div id="todos" class="todos-list"></div>
      </div>
      <div class="scroll" id="activity"></div>
      <div id="activity-empty" class="empty">no activity yet — tool calls and file changes will appear here</div>
    </div>
    <div class="rail-label" data-col="activity" title="Expand">
      <span class="rail-ic">▮</span>
      <span class="rail-txt">Activity</span>
    </div>
  </div>
</div>
<footer class="statusbar">
  <span id="conn" class="dot off">○ disconnected</span>
  <span id="stat-run" class="dot off">○ idle</span>
  <span id="stat-session"></span>
  <span id="stat-grants"></span>
  <button id="reconnect" class="reconnect" style="display:none">↻ reconnect</button>
  <span class="spacer"></span>
  <span class="hint">/ focus · ⏎ send · ⇧⏎ newline</span>
  <span id="clock"></span>
</footer>
<div class="scanlines"></div>
<div class="vignette"></div>
<script>
const qs = new URLSearchParams(location.search);
const initialTask = qs.get('task') || '';
const TOKEN = "__COWORK_TOKEN__";
const $ = id => document.getElementById(id);
let ws = null, sessionId = null, curBubble = null, sentInitial = false, thinkingEl = null;
let stick = true, intentionalClose = false, running = false;
const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// All server calls carry the IPC token (header for fetch, query param for WS).
function api(url, opts){ opts = opts||{}; opts.headers = Object.assign({'X-Cowork-Token':TOKEN}, opts.headers||{}); return fetch(url, opts); }
function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmtTime(ts){ return ts ? new Date(ts*1000).toLocaleString() : ''; }
function setRunning(on){
  running = on;
  $('composer').classList.toggle('running', on);
  $('run-badge').classList.toggle('show', on);
  $('stat-run').textContent = on ? '● running' : '○ idle';
  $('stat-run').className = 'dot ' + (on ? 'on' : 'off');
}

/* ---- matrix rain (theme-tinted, like the TUI home screen) ---- */
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

/* ---- boot line typewriter ---- */
(function(){
  const el = document.getElementById('boot');
  if (!el) return;
  const text = 'workspace broker online · default-deny enforced';
  if (REDUCED){ el.textContent = text; return; }
  let i = 0;
  (function type(){
    el.textContent = text.slice(0, i);
    if (i < text.length){ el.textContent += '▊'; i++; setTimeout(type, 16); }
  })();
})();

/* ---- collapsible sidebars ---- */
function setColState(col, collapsed){
  document.querySelector('.main').classList.toggle('collapsed-' + col, collapsed);
  document.getElementById('col-' + col).classList.toggle('rail', collapsed);
  localStorage.setItem('cowork-col-' + col, collapsed ? '1' : '0');
}
document.querySelectorAll('.collapse-btn').forEach(b => b.onclick = () => {
  const col = b.dataset.col;
  setColState(col, !document.querySelector('.main').classList.contains('collapsed-' + col));
});
document.querySelectorAll('.rail-label').forEach(r => r.onclick = () => setColState(r.dataset.col, false));
['workspace','activity'].forEach(col => {
  if (localStorage.getItem('cowork-col-' + col) === '1') setColState(col, true);
});

/* ---- chat ---- */
function scrollChat(){
  const el = $('chat');
  if (stick) el.scrollTop = el.scrollHeight;
}
$('chat').addEventListener('scroll', () => {
  const el = $('chat');
  stick = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  $('jump').classList.toggle('show', !stick);
});
$('jump').onclick = () => { stick = true; $('chat').scrollTop = 1e9; $('jump').classList.remove('show'); };
function copyText(t, btn){
  const done = () => { if (btn){ const old = btn.textContent; btn.textContent = '✓'; setTimeout(() => btn.textContent = old, 1200); } };
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
  } else fallbackCopy(t, done);
}
function fallbackCopy(t, done){
  const ta = document.createElement('textarea');
  ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch(e){}
  document.body.removeChild(ta);
}
function addMsg(who, cls){
  const m = document.createElement('div');
  m.className = 'msg ' + cls;
  m.innerHTML = '<div class="mhead"><span class="who">' + who + '</span>' +
    '<span class="mright"><span class="ts">' + new Date().toLocaleTimeString() + '</span>' +
    '<button class="copy-btn" title="Copy">⧉</button></span></div><div class="bubble"></div>';
  m.querySelector('.copy-btn').onclick = () => copyText(m.dataset.raw || m.querySelector('.bubble').textContent, m.querySelector('.copy-btn'));
  $('chat').appendChild(m);
  $('chat-empty').style.display = 'none';
  scrollChat();
  scheduleSave();
  return m.querySelector('.bubble');
}
function clearThinking(){
  if (thinkingEl){
    const msg = thinkingEl.closest('.msg');
    if (msg) msg.remove();
    thinkingEl = null;
  }
}
/* Finalize the currently-streaming monologue bubble: render its accumulated
   text as markdown and detach it so the next step starts a fresh card. */
function finalizeBubble(){
  if (!curBubble) return;
  const raw = curBubble.textContent;
  const msg = curBubble.closest('.msg');
  if (raw.trim()){
    msg.dataset.raw = raw;
    curBubble.innerHTML = renderMarkdown(raw);
    curBubble.classList.add('rendered');
    curBubble.querySelectorAll('pre.md-code').forEach(pre => {
      const btn = document.createElement('button');
      btn.className = 'code-copy';
      btn.textContent = '⧉';
      btn.title = 'Copy code';
      btn.onclick = () => copyText(pre.querySelector('code').textContent, btn);
      pre.appendChild(btn);
    });
  } else {
    if (msg) msg.remove();
  }
  curBubble = null;
  clearThinking();
  scheduleSave();
}
/* ---- chat persistence (best-effort, client-side only) ---- */
const CHAT_KEY = 'cowork-chat';
let saveTimer = null;
function saveChat(){
  const msgs = [];
  $('chat').querySelectorAll('.msg').forEach(m => {
    const who = m.querySelector('.who');
    const bubble = m.querySelector('.bubble');
    const ts = m.querySelector('.ts');
    msgs.push({
      who: who ? who.textContent : '',
      cls: m.className.replace('msg','').trim(),
      text: m.dataset.raw || (bubble ? bubble.textContent : ''),
      ts: ts ? ts.textContent : ''
    });
  });
  // Cap to the last 200 messages to keep localStorage small.
  const capped = msgs.slice(-200);
  try { localStorage.setItem(CHAT_KEY, JSON.stringify(capped)); } catch(_){}
}
function scheduleSave(){
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveChat, 300);
}
function restoreChat(){
  let msgs = [];
  try { msgs = JSON.parse(localStorage.getItem(CHAT_KEY) || '[]'); } catch(_){}
  if (!msgs.length) return;
  msgs.forEach(m => {
    const bubble = addMsg(m.who || 'Nova', m.cls || 'nova');
    if (m.text){
      bubble.textContent = m.text;
      bubble.innerHTML = renderMarkdown(m.text);
      bubble.classList.add('rendered');
      bubble.querySelectorAll('pre.md-code').forEach(pre => {
        const btn = document.createElement('button');
        btn.className = 'code-copy';
        btn.textContent = '⧉';
        btn.title = 'Copy code';
        btn.onclick = () => copyText(pre.querySelector('code').textContent, btn);
        pre.appendChild(btn);
      });
    }
    const ts = bubble.closest('.msg').querySelector('.ts');
    if (ts && m.ts) ts.textContent = m.ts;
  });
  $('chat-empty').style.display = 'none';
  scrollChat();
}
function showThinking(){
  if (thinkingEl || curBubble) return;
  const b = addMsg('Nova','nova');
  thinkingEl = document.createElement('span');
  thinkingEl.className = 'thinking';
  thinkingEl.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
  b.appendChild(thinkingEl);
}
function streamToken(bubble, text){
  clearThinking();
  let cur = bubble.querySelector('.cursor');
  if (!cur){ cur = document.createElement('span'); cur.className = 'cursor'; bubble.appendChild(cur); }
  cur.insertAdjacentText('beforebegin', text);
  scrollChat();
  scheduleSave();
}
function addActivity(kind, title, body){
  const a = document.createElement('div');
  a.className = 'act ' + kind;
  const ic = kind==='run' ? '▶' : kind==='done' ? '✓' : kind==='warn' ? '⚠' : '✗';
  a.innerHTML = '<div class="act-head">' + (body ? '<span class="chev">▾</span>' : '') +
    '<span class="ic">'+ic+'</span><b>'+esc(title)+'</b> <span class="t">'+
    new Date().toLocaleTimeString()+'</span></div>' + (body?('<pre>'+esc(body)+'</pre>'):'');
  if (body){
    a.classList.add('open');
    a.querySelector('.act-head').onclick = () => a.classList.toggle('open');
  }
  $('activity').appendChild(a);
  $('activity-empty').style.display = 'none';
  a.scrollIntoView({block:'nearest'});
}

/* ---- markdown rendering (offline, XSS-safe: escape first, then format) ---- */
function mdEscape(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function mdInline(s){
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function renderMarkdown(src){
  const blocks = [];
  src = src.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push('<pre class="md-code"><code>' + mdEscape(code.replace(/\n+$/, '')) + '</code></pre>');
    return '\u0000' + (blocks.length - 1) + '\u0000';
  });
  const lines = mdEscape(src).split('\n');
  let out = '', list = null, para = [];
  const flushPara = () => { if (para.length){ out += '<p>' + mdInline(para.join(' ')) + '</p>'; para = []; } };
  const flushList = () => { if (list){ out += (list === 'ol' ? '</ol>' : '</ul>'); list = null; } };
  for (const raw of lines){
    const line = raw.trim();
    if (!line){ flushPara(); flushList(); continue; }
    const fence = line.match(/^\u0000(\d+)\u0000$/);
    if (fence){ flushPara(); flushList(); out += blocks[+fence[1]]; continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h){ flushPara(); flushList(); out += '<h' + h[1].length + '>' + mdInline(h[2]) + '</h' + h[1].length + '>'; continue; }
    if (/^-{3,}$/.test(line)){ flushPara(); flushList(); out += '<hr>'; continue; }
    const bq = line.match(/^&gt;\s?(.*)$/);
    if (bq){ flushPara(); flushList(); out += '<blockquote>' + mdInline(bq[1]) + '</blockquote>'; continue; }
    const ul = line.match(/^[-*]\s+(.*)$/);
    if (ul){ flushPara(); if (list !== 'ul'){ flushList(); out += '<ul>'; list = 'ul'; } out += '<li>' + mdInline(ul[1]) + '</li>'; continue; }
    const ol = line.match(/^\d+[.)]\s+(.*)$/);
    if (ol){ flushPara(); if (list !== 'ol'){ flushList(); out += '<ol>'; list = 'ol'; } out += '<li>' + mdInline(ol[1]) + '</li>'; continue; }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return out;
}
function onEvent(e){
  const t = e.type, d = e.data||{};
  if (t==='assistant_token'){
    setRunning(true);
    if (!curBubble) curBubble = addMsg('Nova','nova');
    streamToken(curBubble, d.token||d.content||'');
  }
  else if (t==='assistant_done'){
    setRunning(false);
    finalizeBubble();
  }
  else if (t==='tool_started'){
    setRunning(true);
    // A tool call is a natural boundary: finalize the monologue step that led
    // up to it so each step renders as its own card instead of one blob.
    finalizeBubble();
    showThinking();
    addActivity('run','tool: '+(d.name||d.tool||'?'), JSON.stringify(d.args||d.input||{}).slice(0,300));
  }
  else if (t==='tool_finished'){ addActivity('done','tool done: '+(d.name||d.tool||'?'), (d.output||d.result||'').slice(0,600)); }
  else if (t==='file_changed'){ addActivity('done','file: '+(d.path||''), d.summary||''); }
  else if (t==='status'){ setRunning(true); showThinking(); addActivity('run','status', d.message||JSON.stringify(d)); }
  else if (t==='error'){ setRunning(false); clearThinking(); addActivity('err','error', d.message||JSON.stringify(d)); }
  else if (t==='cancelled'){ setRunning(false); clearThinking(); addActivity('err','cancelled',''); curBubble = null; }
  else if (t==='interrupt'){
    if (d.kind === 'question'){ showQuestion(d.payload); }
    else { addActivity('warn','interrupt', d.message||JSON.stringify(d)); }
  }
  else if (t==='todo_update'){ renderTodos(d.todos||[]); }
  else if (t==='subagent_activity'){ addActivity('run','subagent', (d.name||'')+' '+(d.message||'')); }
  else if (t==='usage'){ usage.lastIn=d.input_tokens||0; usage.lastOut=d.output_tokens||0;
    usage.session += (d.input_tokens||0)+(d.output_tokens||0); usage.turns++; }
}
function send(text){
  // Cowork slash commands run locally (UI operations) and are NEVER sent to the
  // agent. Only the Cowork-relevant subset is supported — TUI-only commands
  // (/theme, /voice, /mcp, /ralph, …) are intentionally not here.
  if (text && text.trim().charAt(0) === '/' && handleSlash(text.trim())) return;
  if (!ws || ws.readyState!==1){ addActivity('err','not connected',''); return; }
  stick = true; $('jump').classList.remove('show');
  addMsg('You','user').textContent = text;
  ws.send(JSON.stringify({type:'message',data:{content:text}}));
}

/* ---- Cowork slash commands (client-side, Cowork-relevant only) ---- */
const usage = { lastIn:0, lastOut:0, session:0, turns:0 };
const COWORK_COMMANDS = [
  ['help',                    'list these commands'],
  ['clear',                   'clear the chat view (keep the session)'],
  ['new',                     'start a new conversation'],
  ['context (/tokens, /cost)','show token usage this session'],
  ['workspace (/grants)',     'list granted workspace folders'],
];
function sysMsg(html){ addMsg('⌘ Cowork','sys').innerHTML = html; }
function usageSummary(){
  const f = n => (n||0).toLocaleString();
  return '<b>Token usage</b><br>Current context: <code>'+f(usage.lastIn)+'</code> tokens'
    + '<br>Last reply: <code>'+f(usage.lastOut)+'</code> tokens'
    + '<br>Session total: <code>'+f(usage.session)+'</code> tokens over '+usage.turns+' turn(s)';
}
async function workspaceSummary(){
  try{
    const r = await api('/api/workspace'); const gs = (await r.json()).grants || [];
    if (!gs.length){ sysMsg('No folders granted. Grant one in the Workspace panel to begin.'); return; }
    const rows = gs.map(g => '<code>'+esc(g.display_path)+'</code> ('
      + (g.read?'r':'-')+(g.write?'w':'-')+(g.execute?'x':'-')+(g.recursive?' ·rec':'')+')').join('<br>');
    sysMsg('<b>Granted workspaces ('+gs.length+')</b><br>'+rows);
  }catch(_){ sysMsg('Could not read workspaces.'); }
}
function handleSlash(text){
  const cmd = text.slice(1).split(/\s+/)[0].toLowerCase();
  switch(cmd){
    case 'help':
      sysMsg('<b>Cowork commands</b><br>' + COWORK_COMMANDS.map(
        c => '<code>/'+c[0]+'</code> — '+c[1]).join('<br>'));
      return true;
    case 'clear':  $('clear-chat').click(); return true;
    case 'new':    $('new-chat').click();   return true;
    case 'context': case 'tokens': case 'cost': sysMsg(usageSummary()); return true;
    case 'workspace': case 'grants': workspaceSummary(); return true;
    default:
      sysMsg('Unknown or TUI-only command <code>/'+esc(cmd)+'</code>. Type <code>/help</code> for Cowork commands.');
      return true;
  }
}

/* ---- question dialog (ask_user_question interrupt) ---- */
function showQuestion(payload){
  const p = payload || {};
  $('q-text').textContent = p.question || p.prompt || 'The agent has a question:';
  const ctx = p.context || '';
  $('q-context').textContent = ctx;
  $('q-context').style.display = ctx ? 'block' : 'none';
  const box = $('q-opts'); box.innerHTML = '';
  (p.options || []).forEach((opt, i) => {
    const b = document.createElement('button');
    b.className = 'qopt';
    b.textContent = opt;
    b.onclick = () => answerQuestion(opt, i);
    box.appendChild(b);
  });
  $('q-input').value = '';
  $('qmodal').classList.add('show');
  $('q-input').focus();
}
function answerQuestion(answer, idx){
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({type:'interrupt_response', data:{response:{answer: answer, selected_index: idx}}}));
  $('qmodal').classList.remove('show');
  addMsg('You','user').textContent = answer || '(skipped)';
}
$('q-submit').onclick = () => {
  const v = $('q-input').value.trim();
  if (v) answerQuestion(v, null);
};
$('q-input').addEventListener('keydown', e => {
  if (e.isComposing) return;
  if (e.key === 'Enter'){ e.preventDefault(); $('q-submit').click(); }
});
$('q-skip').onclick = () => answerQuestion('', null);

/* ---- todo list (live agent checklist) ---- */
function renderTodos(todos){
  const panel = $('todos-panel'), box = $('todos');
  if (!todos || !todos.length){ panel.style.display = 'none'; return; }
  box.innerHTML = todos.map(td => {
    const content = (td && typeof td === 'object' && td.content) ? td.content : String(td || '');
    const status = (td && td.status) || 'pending';
    const glyph = status === 'completed' ? '☑' : status === 'in_progress' ? '▶' : '☐';
    const cls = status === 'completed' ? 'done' : status === 'in_progress' ? 'run' : 'pend';
    return '<div class="todo ' + cls + '"><span class="g">' + glyph + '</span><span class="c">' + esc(content) + '</span></div>';
  }).join('');
  panel.style.display = 'flex';
}
$('todos-clear').onclick = () => { $('todos-panel').style.display = 'none'; };

/* ---- workspace broker ---- */
async function loadGrants(){
  let gs = [];
  try {
    const r = await api('/api/workspace');
    if (r.ok) gs = (await r.json()).grants || [];
  } catch(e){ /* server unreachable — fail closed to deny-all */ }
  const box = $('grants'); box.innerHTML = '';
  // Show the canonical root the agent is confined to (first active grant).
  const root = gs[0] && gs[0].canonical;
  if (root){
    const r = document.createElement('div');
    r.className = 'agent-root';
    r.innerHTML = '<span class="lbl">agent root</span><span class="p">'+esc(root)+'</span>';
    box.appendChild(r);
  }
  gs.forEach(g => {
    const d = document.createElement('div'); d.className = 'grant';
    d.innerHTML = '<button data-id="'+g.id+'">revoke</button>' +
      '<div class="p">'+esc(g.display_path)+'</div>' +
      '<div class="meta">' +
        '<span class="chip '+(g.read?'on':'off')+'">r</span>' +
        '<span class="chip '+(g.write?'on':'off')+'">w</span>' +
        '<span class="chip '+(g.execute?'on':'off')+'">x</span>' +
        (g.recursive ? '<span class="chip rec">recursive</span>' : '') +
      '</div>' +
      (g.created_at ? '<div class="created">created ' + fmtTime(g.created_at) + '</div>' : '');
    box.appendChild(d);
  });
  box.querySelectorAll('button').forEach(b => b.onclick = async () => {
    await api('/api/workspace/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:b.dataset.id})});
    loadGrants();
  });
  const has = gs.length > 0;
  $('deny').style.display = has ? 'none' : 'flex';
  $('deny-overlay').style.display = has ? 'none' : 'flex';
  const st = $('ws-state');
  st.textContent = has ? ('workspace: '+gs.length+' granted') : 'workspace: deny-all';
  st.className = 'state ' + (has ? 'ok' : 'deny');
  $('in').disabled = !has; $('send').disabled = !has;
  $('stat-grants').textContent = 'grants: ' + gs.length;
  if (!has && ws){ intentionalClose = true; ws.close(); ws = null; setConn(false); }
  if (has && !ws) connect();
}
$('grant').onclick = async () => {
  const p = $('path').value.trim(); if (!p) return;
  const r = await api('/api/workspace/grant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path: p,
    read: $('perm-r').checked,
    write: $('perm-w').checked,
    execute: $('perm-x').checked,
    recursive: $('perm-rec').checked
  })});
  if (!r.ok){ const e = await r.json(); addActivity('err','grant failed', e.error||('HTTP '+r.status)); return; }
  $('path').value = ''; addActivity('done','workspace granted', p); loadGrants();
};

/* ---- session / websocket ---- */
function setConn(on){
  const el = $('conn');
  el.className = 'dot ' + (on ? 'on' : 'off');
  el.textContent = on ? '● connected' : '○ disconnected';
}
function openWs(){
  ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/'+sessionId+'?token='+encodeURIComponent(TOKEN));
  ws.onmessage = ev => onEvent(JSON.parse(ev.data));
  ws.onopen = () => { setConn(true); $('reconnect').style.display = 'none'; if (initialTask && !sentInitial){ sentInitial = true; send(initialTask); } };
  ws.onclose = () => {
    ws = null; setConn(false);
    $('qmodal').classList.remove('show');
    if (!intentionalClose && sessionId) $('reconnect').style.display = 'inline';
  };
}
async function connect(){
  const r = await api('/sessions',{method:'POST'});
  if (!r.ok){ addActivity('err','session error', await r.text()); return; }
  sessionId = (await r.json()).session_id;
  $('stat-session').textContent = 'session ' + sessionId.slice(0,8);
  intentionalClose = false;
  openWs();
}
$('reconnect').onclick = () => { $('reconnect').style.display = 'none'; if (sessionId) openWs(); else connect(); };

/* ---- new chat / clear ---- */
$('new-chat').onclick = async () => {
  if (sessionId) api('/sessions/' + sessionId, {method:'DELETE'}).catch(()=>{});
  intentionalClose = true;
  if (ws){ ws.close(); ws = null; }
  sessionId = null; curBubble = null; clearThinking();
  $('chat').innerHTML = ''; $('activity').innerHTML = '';
  $('todos-panel').style.display = 'none';
  $('chat-empty').style.display = 'flex'; $('activity-empty').style.display = 'flex';
  $('stat-session').textContent = ''; setConn(false);
  stick = true; $('jump').classList.remove('show');
  try { localStorage.removeItem(CHAT_KEY); } catch(_){}
  await connect();
};
$('clear-chat').onclick = () => {
  $('chat').innerHTML = '';
  $('chat-empty').style.display = 'flex';
  curBubble = null; clearThinking();
  stick = true; $('jump').classList.remove('show');
  try { localStorage.removeItem(CHAT_KEY); } catch(_){}
};
$('clear-activity').onclick = () => {
  $('activity').innerHTML = '';
  $('activity-empty').style.display = 'flex';
};

/* ---- composer ---- */
$('send').onclick = () => { const v = $('in').value.trim(); if (v){ send(v); $('in').value = ''; } };
$('stop').onclick = () => {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({type:'cancel'}));
  setRunning(false);
};
$('in').addEventListener('keydown', e => {
  if (e.isComposing) return;
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); $('send').click(); }
});
// Press "/" (when not already typing) to focus the composer.
document.addEventListener('keydown', e => {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (e.key === '/' && tag !== 'TEXTAREA' && tag !== 'INPUT'){
    e.preventDefault();
    $('in').focus();
  }
});

/* ---- clock ---- */
function tick(){ $('clock').textContent = new Date().toLocaleTimeString('en-GB'); }
setInterval(tick, 1000); tick();

/* ---- boot ---- */
$('sid').textContent = qs.get('session') ? ('session ' + qs.get('session').slice(0,8)) : '';
restoreChat();
loadGrants();
</script>
</body></html>"""
