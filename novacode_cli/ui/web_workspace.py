"""Shared layout and drawer behavior for Nova's local conversation pages."""

WORKSPACE_CSS = """
:root{color-scheme:dark;--bg:#11141d;--surface:#171c28;--panel:#202737;--boost:#283249;
--border:#30394e;--fg:#edf0fa;--muted:#a6afc4;--primary:#a5b6ff;--secondary:#b9d6ad;
--accent:#c5b1ed;--success:#91cfbd;--warning:#e1c18d;--error:#f19ba8;
--utility:'Cascadia Code',Consolas,monospace}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body.workspace-app{height:100dvh;display:grid;grid-template-columns:280px minmax(0,1fr);
overflow:hidden;background:var(--bg);color:var(--fg);font:14px/1.6 'Segoe UI',system-ui,sans-serif}
button,input,textarea,select{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed}
[hidden]{display:none!important}button:focus-visible,a:focus-visible,input:focus-visible,
textarea:focus-visible{outline:2px solid var(--primary);outline-offset:3px}
.workspace-sidebar{display:flex;flex-direction:column;min-width:0;min-height:0;background:#151925;
border-right:1px solid var(--border);z-index:20;overflow:hidden}
.workspace-brand{display:flex;align-items:center;gap:11px;min-height:77px;padding:0 23px;
border-bottom:1px solid var(--border);font-size:17px;font-weight:650}
.workspace-brand .brand-icon{display:grid;place-items:center;width:31px;height:31px;
background:#252e49;color:var(--primary);border:1px solid #4a5882;border-radius:9px}
.workspace-brand .brand-product{font-size:12px;font-weight:400;color:var(--muted);margin-left:auto}
.sidebar-content{flex:1;min-height:0;overflow-y:auto;padding:23px 17px}
.sidebar-label{margin:23px 8px 11px;color:var(--muted);font:10px var(--utility);
letter-spacing:.12em;text-transform:uppercase}.sidebar-label:first-child{margin-top:0}
.sidebar-primary{width:100%;min-height:43px;border:1px solid #46567f;border-radius:8px;
background:#232d46;color:var(--primary);font-size:13px;font-weight:600;text-align:left;padding:10px 14px}
.sidebar-primary:hover{background:#2d3a59}.sidebar-note{font-size:12px;line-height:1.7;
color:var(--muted);padding:0 8px}.sidebar-footer{padding:17px 23px;border-top:1px solid var(--border);
font-size:11px;color:var(--muted);display:flex;flex-direction:column;gap:9px}
.sidebar-footer .sid{margin:0;overflow-wrap:anywhere;white-space:normal;font:10px var(--utility)}
.workflow{margin:0;padding:0 8px 0 25px;color:var(--muted);font-size:12px;line-height:2.1}
.workspace-chat{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
.chat-topbar{min-height:77px;display:flex;align-items:center;gap:13px;padding:15px 30px;
border-bottom:1px solid var(--border);background:var(--bg);flex-shrink:0}
.chat-heading{flex:1;min-width:0}.chat-heading h1,.chat-heading h2{margin:0;font-size:16px;
font-weight:600;letter-spacing:-.02em;color:var(--fg);padding:0;text-transform:none;white-space:nowrap}
.chat-heading p{margin:1px 0 0;color:var(--muted);font-size:11px}
.header-action{background:transparent;border:1px solid var(--border);border-radius:7px;
color:var(--muted);font-size:12px;padding:7px 11px;min-height:36px}
.header-action:hover{color:var(--fg);border-color:#6474aa}
.mobile-menu,.sidebar-close{display:none}.workspace-backdrop{display:none}
.run-badge{display:none;border:1px solid #526485;border-radius:20px;color:var(--primary);
font:10px var(--utility);padding:4px 9px;white-space:nowrap}.run-badge.show{display:inline-block}
.state{width:fit-content;border:1px solid var(--border);border-radius:6px;font:10px var(--utility);
padding:5px 8px;white-space:normal}.state.ok{color:var(--success)}.state.deny{color:var(--warning)}
.welcome{margin:auto;text-align:left;max-width:600px;padding:45px 16px;animation:none}
.welcome .welcome-icon{display:grid;place-items:center;width:48px;height:48px;margin-bottom:25px;
color:var(--primary);background:#242e49;border:1px solid #45557f;border-radius:14px;font-size:23px}
.welcome h2{font-family:Bahnschrift,'Segoe UI',sans-serif;font-size:clamp(28px,4vw,38px);
font-weight:550;letter-spacing:-.04em;line-height:1.2;margin:0 0 15px;color:var(--fg)}
.welcome p{font-size:14px;line-height:1.8;margin:0;color:var(--muted)}
.prompt-options{display:flex;flex-wrap:wrap;gap:9px;margin-top:25px}.prompt-option{padding:9px 13px;
border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--muted);font-size:12px}
.prompt-option:hover{color:var(--primary);border-color:#6474aa}
body #input-area,body .composer{display:flex;align-items:center;gap:10px;flex-shrink:0;
width:calc(100% - 60px);max-width:960px;margin:12px auto 14px;padding:10px 12px;
border:1px solid #46516e;border-radius:12px;background:var(--surface)}
body #input-area:focus-within,body .composer:focus-within{border-color:var(--primary)}
body #input-area textarea,body .composer textarea{flex:1;min-width:0;min-height:46px;height:46px;
max-height:180px;padding:11px 7px;resize:none;background:transparent;color:var(--fg);font:14px/1.6 'Segoe UI',sans-serif;
border:0;outline:0}body #input-area textarea:focus,body .composer textarea:focus{background:transparent}
body #input-area button,body .composer button{min-height:40px;padding:9px 16px;border:0;
border-radius:8px;background:var(--primary);color:#151925;font-size:12px;font-weight:600;letter-spacing:0}
body .composer .stop{background:#55303b;color:#ffd5dd;display:none}
body .composer.running #send{display:none}body .composer.running .stop{display:block}
body .mode-badge,body .prefix{display:none}
body #status-bar,body .statusbar{display:flex;gap:13px;align-items:center;flex-wrap:nowrap;
padding:9px 30px;border-top:0;background:var(--bg);color:var(--muted);font:10px/1.5 var(--utility);
min-height:35px;flex-shrink:0;animation:none}#status-bar .spacer,.statusbar .spacer{flex:1}
.statusbar .dot.on,#status-bar .dot.on{color:var(--success)}
.statusbar .dot.off,#status-bar .dot.off{color:var(--muted)}
.status-msg{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
code,pre{font-family:var(--utility)}

/* Council's advisor list stays next to the conversation as it streams. */
body #new-thread{margin:0;font-size:13px;letter-spacing:0;text-transform:none;color:var(--primary);
background:#232d46;border:1px solid #46567f}
body #bench{display:none;padding:0;gap:9px;overflow:visible;border:0;background:transparent}
body #bench.active{display:flex;flex-direction:column}
body #bench .seat{flex:none;min-width:0;display:grid;grid-template-columns:36px minmax(0,1fr);
column-gap:12px;row-gap:1px;padding:11px;border-radius:9px;background:var(--surface);
border:1px solid var(--border);border-left:2px solid var(--seat);opacity:.75;box-shadow:none}
body #bench .seat-av{grid-row:1/3;font-size:19px;width:35px;height:35px}
body #bench .seat-name{font-size:12px;text-align:left;font-weight:500;overflow-wrap:anywhere}
body #bench .seat-state{font:10px var(--utility);letter-spacing:0;text-transform:none}
#bench.active + .advisor-placeholder{display:none}
body #bench .seat[data-state=speaking],body #bench .seat[data-state=winner]{opacity:1;background:#242d41;
transform:none;border-color:var(--seat)}
body #messages{flex:1;min-height:0;overflow-y:auto;padding:30px max(28px,calc((100% - 920px)/2));gap:19px}
body .agent{width:100%;max-width:100%;align-self:stretch;padding:20px 22px;border-radius:0 10px 10px 0;
background:var(--surface)}body .agent .body{font-size:14px;line-height:1.8}
body .agent .who{font-size:12px}.topic-banner{overflow-wrap:anywhere}
body .verdict{max-width:100%;padding:21px;border-radius:10px}
body .vote-row{max-width:100%;border-radius:0 8px 8px 0;padding:10px 16px}

/* Cowork uses one sidebar with two panels, instead of squeezing chat between columns. */
.sidebar-tabs{display:flex;gap:5px;margin:19px 16px 12px;padding:4px;border:1px solid var(--border);
border-radius:9px;background:var(--bg)}.sidebar-tab{flex:1;padding:8px 4px;border:0;border-radius:6px;
background:transparent;color:var(--muted);font-size:12px}.sidebar-tab[aria-selected=true]{background:#29334b;color:var(--primary)}
.workspace-panel{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.workspace-panel .col-head{display:flex;align-items:center;padding:4px 18px 11px;gap:8px;border:0;background:transparent}
.workspace-panel .col-head h2{font-size:11px;flex:1;color:var(--muted);font-weight:500}
.workspace-panel .col-head button{padding:3px 7px;background:transparent;color:var(--muted);border:0;font-size:11px}
.col-body{display:flex;flex-direction:column;flex:1;min-height:0;position:relative}
.scroll{flex:1;min-height:0;overflow:auto;padding:12px 17px}
body .picker{padding:6px 17px 18px;border-bottom:1px solid var(--border);background:transparent}
body .picker label{font-size:11px;text-transform:none;letter-spacing:0}
body .picker input[type=text]{font-size:12px;padding:10px;border-radius:7px;background:var(--bg)}
body .perms{gap:5px}body .perms label{padding:4px 7px;border-radius:5px;font-size:10px}
body .picker button{padding:10px;border-radius:7px;letter-spacing:0;font-weight:600}
body .grant,body .agent-root{border-radius:7px}body .grant .p{font-size:11px}
body .deny-note{font-size:11px;background:transparent;padding:12px 17px;line-height:1.7}
body #col-chat{border:0;animation:none}
body #chat{min-width:0;padding:28px max(28px,calc((100% - 920px)/2))}
body .msg{padding:17px 20px;border-radius:0 10px 10px 0;margin-bottom:18px}
body .bubble{font-size:14px;line-height:1.8}body .mhead .who{font-size:11px;letter-spacing:.03em}
body #chat-empty{padding:40px;max-width:650px;left:50%;transform:translateX(-50%);width:100%;font-size:15px}
body #deny-overlay{inset:78px 0 120px;background:var(--bg);padding:24px;gap:15px}
body #deny-overlay .lock{color:var(--primary);font-size:32px;animation:none;text-shadow:none}
body #deny-overlay .big{font:550 clamp(24px,3vw,34px)/1.2 Bahnschrift,'Segoe UI',sans-serif;
color:var(--fg);letter-spacing:-.03em}body #deny-overlay .sub{max-width:390px;font-size:14px;line-height:1.8}
.choose-workspace{padding:10px 16px;border:1px solid #526485;border-radius:8px;
color:var(--primary);background:#242e49;font-size:13px}
body .todos-panel{max-height:40%;background:transparent}body .todos-head{font-size:10px;letter-spacing:.05em}
body .act{border-radius:7px}.chat-topbar .chat-heading h2::before{display:none}
@media(max-width:1100px){.statusbar .hint{display:none}}

@media(max-width:760px){
 body.workspace-app{grid-template-columns:minmax(0,1fr)}
 .workspace-sidebar{position:fixed;inset:0 auto 0 0;width:min(300px,calc(100vw - 38px));
 transform:translateX(-100%);transition:transform .2s ease;visibility:hidden}
 body.sidebar-open .workspace-sidebar{transform:none;visibility:visible}
 .workspace-backdrop{position:fixed;inset:0;background:#080b14b8;border:0;border-radius:0;z-index:19}
 body.sidebar-open .workspace-backdrop{display:block}
 .mobile-menu,.sidebar-close{display:grid;place-items:center;width:36px;height:36px;flex-shrink:0;
 border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--fg);font-size:17px}
 .sidebar-close{margin-left:auto}.workspace-brand .brand-product{display:none}
 .chat-topbar{min-height:70px;padding:12px 15px;gap:10px}
 .chat-heading p{font-size:10px}.chat-topbar .header-action{padding:6px 8px;font-size:11px}
 .chat-topbar .run-badge{font-size:9px;padding:3px 5px}.chat-topbar .sid{display:none}
 .cowork-app .chat-topbar .run-badge{display:none}
 body #messages,body #chat{padding:20px 15px}.welcome{padding:25px 5px}
 .welcome h2{font-size:29px}.welcome p{font-size:13px}.prompt-options{gap:8px}
 body .agent{padding:15px}.agent .who{flex-wrap:wrap}body .agent .who-tag{font-size:9px}
 body #input-area,body .composer{width:calc(100% - 28px);padding:8px;margin:8px auto 6px;gap:5px}
 body #input-area textarea,body .composer textarea{font-size:16px}
 body #input-area button,body .composer button{padding:9px 12px}
 body #status-bar,body .statusbar{padding:8px 15px;gap:9px;font-size:9px}
 #status-bar .hint,.statusbar .hint,.statusbar #stat-session{display:none}
 body #deny-overlay{inset:71px 0 110px;padding:22px}.status-msg{max-width:40vw}
 .approval-row{flex-wrap:wrap}.approval-state{flex-wrap:wrap}
 .qmodal{padding:12px}.qfree{flex-wrap:wrap}.qfree input{min-width:0}
}
@media(max-width:420px){.chat-heading p{display:none}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
"""

# Keep fixed sidebar controls outside a single, keyboard-scrollable content area.
WORKSPACE_CSS += """
.workspace-sidebar .workspace-brand,.workspace-sidebar .sidebar-tabs,.workspace-sidebar .sidebar-footer,
.workspace-panel .col-head{flex-shrink:0}
.workspace-sidebar .sidebar-content,.workspace-sidebar .col-body{overscroll-behavior-y:contain;scrollbar-gutter:stable}
.workspace-sidebar :is(.sidebar-content,.col-body,.scroll),.settings-body{
scrollbar-width:thin;scrollbar-color:#536b72 transparent;scroll-padding-block:16px}
.workspace-sidebar :is(.sidebar-content,.col-body,.scroll)::-webkit-scrollbar,.settings-body::-webkit-scrollbar{width:8px;height:8px}
.workspace-sidebar :is(.sidebar-content,.col-body,.scroll)::-webkit-scrollbar-thumb,.settings-body::-webkit-scrollbar-thumb{
background:#536b72;border-radius:8px;border:2px solid transparent;background-clip:padding-box}
.workspace-sidebar :is(.sidebar-content,.col-body,.scroll)::-webkit-scrollbar-thumb:hover,.settings-body::-webkit-scrollbar-thumb:hover{background-color:#80979c}
.workspace-sidebar .sidebar-footer .sid{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(forced-colors:active){.workspace-sidebar :is(.sidebar-content,.col-body,.scroll),.settings-body{scrollbar-color:auto}}
"""

WORKSPACE_SCRIPT = """
(() => {
  const sidebar = document.getElementById('workspace-sidebar');
  const toggle = document.getElementById('sidebar-toggle');
  const close = document.getElementById('sidebar-close');
  const backdrop = document.getElementById('workspace-backdrop');
  const mobile = matchMedia('(max-width: 760px)');
  let returnFocus = toggle;
  function setOpen(open, restoreFocus = true) {
    open = open && mobile.matches;
    if (open) returnFocus = document.activeElement;
    document.body.classList.toggle('sidebar-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    sidebar.inert = mobile.matches && !open;
    sidebar.setAttribute('aria-hidden', String(mobile.matches && !open));
    sidebar.setAttribute('role', open ? 'dialog' : 'complementary');
    if (open) { sidebar.setAttribute('aria-modal', 'true'); close.focus(); }
    else {
      sidebar.removeAttribute('aria-modal');
      if (restoreFocus && mobile.matches) {
        (returnFocus.offsetParent !== null && !returnFocus.disabled ? returnFocus : toggle).focus();
      }
    }
  }
  toggle.addEventListener('click', () => setOpen(true));
  close.addEventListener('click', () => setOpen(false));
  backdrop.addEventListener('click', () => setOpen(false));
  mobile.addEventListener('change', () => setOpen(false, false));
  document.addEventListener('keydown', event => {
    if (!document.body.classList.contains('sidebar-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
    if (event.key !== 'Tab') return;
    const focusable = [...sidebar.querySelectorAll('button,input,select,textarea,a[href],[tabindex="0"]')]
      .filter(el => !el.disabled && el.offsetParent !== null && el.tabIndex >= 0);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  setOpen(false, false);
  const tabs = [...document.querySelectorAll('[data-sidebar-tab]')];
  function selectTab(name) {
    tabs.forEach(tab => {
      const selected = tab.dataset.sidebarTab === name;
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !selected;
    });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.sidebarTab));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[next].dataset.sidebarTab); tabs[next].focus();
    });
  });
  document.querySelectorAll('[data-open-workspace]').forEach(button => button.addEventListener('click', () => {
    selectTab('workspace');
    if (mobile.matches) setOpen(true);
    document.getElementById('path').focus();
  }));
  document.querySelectorAll('[data-return-to-chat]').forEach(button => button.addEventListener('click', () => {
    if (mobile.matches) setOpen(false);
    const input = document.getElementById('input');
    if (input && !input.disabled) input.focus();
  }));
  document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
    const input = document.getElementById('input'); input.value = button.dataset.prompt;
    input.dispatchEvent(new Event('input')); input.focus();
  }));
})();
"""
