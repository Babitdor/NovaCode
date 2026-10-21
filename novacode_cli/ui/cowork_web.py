"""Cowork visual treatment, scoped to preserve the shared conversation controls."""

COWORK_STYLE = r"""
body.cowork-app{--bg:#121a21;--surface:#19242d;--panel:#202f3b;--boost:#283d49;--border:#344650;
--primary:#93d6cd;--accent:#c6dbd5;--muted:#9dafba;--fg:#ecf2f3;grid-template-columns:310px minmax(0,1fr)}
.cowork-app .workspace-sidebar{background:#16212a}.cowork-app .workspace-brand .brand-icon{
background:#253d43;color:var(--primary);border-color:#46665f;border-radius:50%}
.cowork-app .sidebar-tabs{margin:18px 16px 16px;background:#111d25;border-color:var(--border)}
.cowork-app .sidebar-tab{padding:10px 6px}.cowork-app .sidebar-tab[aria-selected=true]{background:#2a4245;color:var(--fg)}
.cowork-app .workspace-panel .col-head{padding:0 20px 12px}.cowork-app .col-head h2{font-size:11px;letter-spacing:.08em;color:var(--primary)}
.cowork-app .workspace-panel>.col-body{display:block;overflow-y:auto;overflow-x:hidden;overscroll-behavior-y:contain;scrollbar-gutter:stable}
.cowork-app .col-body>.scroll{overflow:visible;min-height:0;flex:none;padding:10px 17px 20px}
.cowork-app .picker{padding:8px 17px 20px}.cowork-app .picker>label{font-size:12px;font-weight:600;color:var(--fg);margin-bottom:8px}
.cowork-app .picker input[type=text]{background:#111d25;font-size:13px;line-height:1.6;padding:11px}
.cowork-app .perms{gap:7px;margin:12px 0}.cowork-app .perms label{font-size:11px;padding:6px 8px;border-radius:6px}
.cowork-app .perms label:has(input:checked){background:#203b36;border-color:#54796b;color:#b6dfca}
.cowork-app .picker button{padding:11px;background:var(--primary);color:#132923;border-radius:8px}
.cowork-app .picker button:hover{background:#b4e6de}.cowork-app .deny-note{color:var(--muted);background:transparent;padding:16px 18px}
.cowork-app .grant,.cowork-app .agent-root{padding:14px;border-radius:10px;background:var(--surface);border-color:var(--border);border-left-color:#93d6cd}
.cowork-app .grant .p{font-size:12px;line-height:1.7;word-break:normal;overflow-wrap:anywhere}
.cowork-app .grant .meta{gap:6px;margin-top:10px}.cowork-app .chip{border-radius:4px;padding:2px 6px}
.cowork-app .grant button{border-radius:5px;padding:4px 8px;font-size:10px;letter-spacing:0;text-transform:none}
.cowork-app .todos-panel{max-height:none;border-bottom:1px solid var(--border);padding:0 17px 14px}
.cowork-app .todos-list{max-height:none;overflow:visible;padding:0}.cowork-app .todos-head{padding:12px 0;font-size:11px;color:var(--primary)}
.cowork-app .act{padding:12px;border:1px solid var(--border);background:var(--surface);margin-bottom:9px;border-radius:8px;overflow-wrap:anywhere}
.cowork-app .empty{padding:22px 18px;color:var(--muted);font-size:13px;line-height:1.8}
.cowork-app #deny-overlay .lock{width:auto;height:auto;background:none;border:0;font-size:11px;letter-spacing:.13em;color:var(--primary)}
.cowork-app #deny-overlay .big{font-family:Georgia,'Times New Roman',serif;font-weight:400;letter-spacing:-.04em;font-size:clamp(32px,4vw,52px);line-height:1.15;max-width:570px}
.cowork-app #deny-overlay .sub{font-size:15px;line-height:1.8;max-width:470px}
.cowork-app .choose-workspace{padding:12px 18px;border-radius:8px;background:#2a4245;border-color:#54796b;color:var(--fg)}
.cowork-app #chat-empty{font:400 clamp(22px,3vw,32px)/1.4 Georgia,serif;color:var(--muted);max-width:650px}
.cowork-app .msg{border:1px solid var(--border);border-left:3px solid var(--primary);border-radius:12px;background:var(--surface)}
.cowork-app .msg.user{border-left-color:#a4b8ca;background:#1d2a35}.cowork-app .bubble{line-height:1.85}
.cowork-app .composer{width:calc(100% - 48px);margin:0 auto 12px;padding:9px;border-color:var(--border);border-radius:14px}
.cowork-app .composer button{border-radius:8px}.cowork-app .header-action{color:var(--fg);border-color:var(--border);padding:9px 13px}
.cowork-app .qbox{border-radius:14px;border-color:#658e86;background:var(--surface)}
.cowork-app .qopt{border-radius:8px;background:var(--panel)}.cowork-app .qfree input{border-radius:7px}
@media(max-width:760px){body.cowork-app{grid-template-columns:minmax(0,1fr)}
.cowork-app .composer{width:calc(100% - 20px);margin:0 auto 8px}.cowork-app .header-action{padding:7px 9px;font-size:11px}
.cowork-app #deny-overlay .big{font-size:34px}.cowork-app #deny-overlay .sub{font-size:14px}}
"""
