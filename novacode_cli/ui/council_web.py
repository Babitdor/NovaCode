"""Council-only visual treatment and draft-based settings editor."""

COUNCIL_STYLE = r"""
body.council-app{--bg:#121a21;--surface:#19242d;--panel:#202f3b;--boost:#283d49;--border:#344650;
--primary:#93d6cd;--accent:#c6dbd5;--muted:#9dafba;--fg:#ecf2f3;grid-template-columns:310px minmax(0,1fr)}
.council-app .workspace-sidebar{background:#16212a}.council-app .workspace-brand{border-color:var(--border)}
.council-app .workspace-brand .brand-icon{background:#253d43;color:var(--primary);border-color:#46665f;border-radius:50%}
.council-app .chat-topbar{background:var(--bg);gap:14px}.council-app .chat-heading{flex:1}
.council-app .sidebar-primary{background:#263d42;border-color:#47655f;color:var(--primary)}
.council-app .sidebar-label{letter-spacing:.12em;font-size:10px;margin-top:25px;color:var(--primary)}
.council-app .sidebar-content>label{font-size:12px;font-weight:600}.council-app .sidebar-note{font-size:12px}
.council-app #messages{padding-top:40px}.council-app .welcome{max-width:680px;text-align:left;align-items:flex-start}
.council-app .welcome h2{font-family:Georgia,'Times New Roman',serif;font-weight:400;letter-spacing:-.045em;font-size:clamp(36px,5vw,62px);line-height:1.1}
.council-app .welcome-icon{width:auto;height:auto;padding:0;border:0;background:none;border-radius:0;display:block;line-height:1.5;font-size:11px;letter-spacing:.12em;color:var(--primary)}
.council-app .welcome p{font-size:16px;line-height:1.8;color:var(--muted)}
.council-app .prompt-options{justify-content:flex-start}.council-app .prompt-option{border-radius:8px;background:transparent}
.council-app #input-area{width:calc(100% - 48px);margin:0 auto 12px;border:1px solid var(--border);border-radius:14px;background:var(--surface);padding:9px}
.council-app #input-area textarea{background:transparent}.council-app #input-area button{border-radius:9px;background:var(--primary)}
.council-app #stop-council,.council-app .settings-open{padding:9px 13px;color:var(--fg);background:transparent;border:1px solid var(--border);border-radius:8px}
.council-app #stop-council{width:100%;color:#efb5aa}.council-app .agent{border-radius:12px;border:1px solid var(--border);border-left:3px solid var(--seat)}
.council-app .topic-banner{font-size:19px;line-height:1.6;border-bottom:1px solid var(--border);padding-bottom:20px}
.council-app .verdict{border-color:#5a8a7e;background:#1c302f}.council-app .clarification{background:#203537;border-color:#658e86}
.council-app .clarification button{background:#2d4c4a;color:var(--fg);border:1px solid #597a72;border-radius:7px}
.council-app .clarification input{background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:7px}
.council-app #decision-options,.council-app #decision-criteria{background:#111d25;border-color:var(--border);line-height:1.7}
#council-settings{width:min(940px,calc(100vw - 32px));max-height:calc(100dvh - 32px);padding:0;border:1px solid #50666d;
 border-radius:18px;background:var(--bg);color:var(--fg);box-shadow:0 32px 100px #0008;overflow:hidden}
#council-settings::backdrop{background:#080f16ba;backdrop-filter:blur(6px)}
.settings-head{padding:24px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:20px}
.settings-head h2{font:400 29px Georgia,serif;margin:0}.settings-head p{margin:5px 0 0;color:var(--muted);font-size:13px}
.settings-body{display:grid;grid-template-columns:215px minmax(0,1fr);max-height:calc(100dvh - 260px);min-height:240px;overflow:auto}
.settings-seats{background:var(--surface);padding:16px 12px;display:flex;flex-direction:column;gap:5px}
.settings-seats button{background:transparent;color:var(--muted);border:1px solid transparent;text-align:left;padding:11px 12px;border-radius:8px;overflow-wrap:anywhere}
.settings-seats button[aria-pressed=true]{background:#2a4245;border-color:#4c726a;color:var(--fg)}
.settings-form{padding:22px 28px;min-width:0;display:grid;gap:16px;align-content:start}
.settings-form label{display:grid;gap:6px;font-size:12px;font-weight:600}.settings-form input,.settings-form select,.settings-form textarea{
 width:100%;min-width:0;padding:10px 12px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--fg)}
.settings-form input,.settings-form select,.settings-form textarea{font-weight:400}
.settings-form input:disabled{opacity:.5}.settings-pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.settings-template{display:flex;gap:8px}.settings-template select{flex:1}.settings-template button{flex-shrink:0}
.settings-foot{padding:17px 28px;border-top:1px solid var(--border);display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.settings-foot p{flex:1 1 100%;margin:0;color:var(--muted);font-size:12px;min-height:20px}
#settings-message[data-error=true]{color:var(--error)}.settings-foot button,.settings-template button{border:1px solid var(--border);border-radius:7px;padding:9px 15px;background:var(--panel);color:var(--fg)}
.settings-foot #settings-save{margin-left:auto;background:var(--primary);color:#132923;border-color:var(--primary);font-weight:650}
#settings-role{font-size:12px;color:var(--primary);margin:0}.council-app select:focus-visible{outline:2px solid var(--primary);outline-offset:3px}
@media(max-width:760px){body.council-app{grid-template-columns:minmax(0,1fr)}.council-app #input-area{width:calc(100% - 20px);margin:0 auto 8px}
.council-app .run-badge{display:none}.council-app .settings-open{font-size:12px;padding:8px}.settings-head{padding:18px}.settings-head h2{font-size:24px}
.settings-body{grid-template-columns:minmax(0,1fr);max-height:calc(100dvh - 260px)}.settings-seats{flex-direction:row;overflow-x:auto;padding:10px;min-width:0}
.settings-seats button{flex-shrink:0;max-width:180px}.settings-form{padding:18px}.settings-pair{grid-template-columns:minmax(0,1fr)}.settings-foot{padding:12px 18px}}
"""

COUNCIL_SETTINGS_HTML = r"""
<dialog id="council-settings" aria-labelledby="settings-title" aria-describedby="settings-description">
 <div class="settings-head"><div><h2 id="settings-title">Shape your council</h2>
 <p id="settings-description">Edit each voice’s perspective and model. Saved changes apply to future runs.</p></div></div>
 <div class="settings-body"><nav id="settings-seats" class="settings-seats" aria-label="Council seats"></nav>
 <div class="settings-form" id="settings-form" hidden>
  <p id="settings-role"></p>
  <label>Display name<input id="settings-name" maxlength="64"></label>
  <div class="settings-pair"><label>Model provider<select id="settings-provider"></select></label>
  <label>Model ID<input id="settings-model" maxlength="240" list="settings-models" placeholder="Inherit session model"><datalist id="settings-models"></datalist></label></div>
  <div><label for="settings-template">Personality template</label><div class="settings-template"><select id="settings-template"></select><button type="button" id="settings-apply">Apply</button></div></div>
  <label>System prompt / chairman perspective<textarea id="settings-system" rows="7" maxlength="12000"></textarea></label>
 </div></div>
 <div class="settings-foot"><p id="settings-message" role="status" aria-live="polite">Loading council settings…</p>
 <button type="button" id="settings-reset">Reset all</button><button type="button" id="settings-cancel">Cancel</button>
 <button type="button" id="settings-save">Save changes</button></div>
</dialog>
"""

COUNCIL_SETTINGS_SCRIPT = r"""
(()=>{
 const byId=id=>document.getElementById(id), dialog=byId('council-settings');
 let catalog=null,draft=null,current=0,busy=false,opener=null;
 const entries=()=>[...draft.members,draft.judge];
 const message=(text,error=false)=>{byId('settings-message').textContent=text;byId('settings-message').dataset.error=String(error);};
 const clone=value=>JSON.parse(JSON.stringify(value));
 function capture(){if(!draft)return;const entry=entries()[current];entry.name=byId('settings-name').value;
 entry.system=byId('settings-system').value;entry.provider=byId('settings-provider').value;entry.model=entry.provider?byId('settings-model').value:'';}
 function models(){const provider=byId('settings-provider').value;byId('settings-model').disabled=!provider;
 byId('settings-model').placeholder=provider?'Enter a model ID':'Inherit session model';
 byId('settings-models').replaceChildren(...(catalog.providers.find(p=>p.id===provider)?.models||[]).map(model=>new Option(model,model)));}
 function show(){const entry=entries()[current];byId('settings-name').value=entry.name;byId('settings-system').value=entry.system;
 byId('settings-provider').value=entry.provider;byId('settings-model').value=entry.model;models();
 byId('settings-role').textContent=entry.id==='judge'?'Chairman · web synthesis; judge · planning councils':entry.id==='ux'?'Sixth advisor · planning councils only':'Advisor · web debates and planning councils';
 seats();}
 function seats(){byId('settings-seats').replaceChildren(...entries().map((entry,index)=>{const button=document.createElement('button');
 button.type='button';button.textContent=entry.avatar+' '+(entry.name||'Unnamed voice')+(entry.id==='judge'?' · Chairman':'');button.setAttribute('aria-pressed',String(current===index));
 button.onclick=()=>{if(busy)return;capture();current=index;show();};return button;}));}
 function lock(value){busy=value;dialog.querySelectorAll('button,input,select,textarea').forEach(control=>control.disabled=value);
 if(!value&&draft)models();}
 async function open(){if(dialog.open)return;opener=document.activeElement;dialog.showModal();draft=null;catalog=null;current=0;
 byId('settings-form').hidden=true;byId('settings-seats').replaceChildren();message('Loading council settings…');lock(true);
 try{const response=await fetch('/api/council/settings',{signal:AbortSignal.timeout(10000)});const result=await response.json();if(!response.ok)throw Error(result.error||'Could not load settings.');
 catalog=result;draft=clone(catalog.settings);byId('settings-provider').replaceChildren(new Option('Session model (inherit)',''),...catalog.providers.map(p=>new Option(p.name,p.id)));
 byId('settings-template').replaceChildren(...catalog.templates.map(p=>new Option(p.name,p.id)));byId('settings-form').hidden=false;show();
 message(catalog.warning || 'Five advisors debate, then the chairman synthesizes their votes. The sixth advisor participates only in planning councils.',Boolean(catalog.warning));
 }catch(error){message(error.message,true);}finally{lock(false);if(!draft){byId('settings-save').disabled=true;byId('settings-reset').disabled=true;byId('settings-cancel').focus();}else{byId('settings-seats').querySelector('[aria-pressed=true]')?.focus();}}}
 document.querySelectorAll('[data-council-settings]').forEach(button=>button.addEventListener('click',open));
 byId('settings-provider').onchange=()=>{byId('settings-model').value='';models();};
 byId('settings-apply').onclick=()=>{if(!draft)return;const template=catalog.templates.find(t=>t.id===byId('settings-template').value);
 byId('settings-name').value=template.name+(entries()[current].id==='judge'?' (Judge)':'');byId('settings-system').value=template.system;capture();seats();message('Template applied to the draft. Save to keep it.');};
 byId('settings-reset').onclick=()=>{if(!catalog)return;draft=clone(catalog.defaults);show();message('Defaults restored in the draft. Save to keep them.');};
 byId('settings-cancel').onclick=()=>dialog.close();
 dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
 dialog.addEventListener('close',()=>{draft=null;opener?.focus();});
 byId('settings-save').onclick=async()=>{if(!draft||busy)return;capture();lock(true);message('Saving council settings…');
 try{const response=await fetch('/api/council/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(draft),signal:AbortSignal.timeout(10000)});
 const result=await response.json();if(!response.ok)throw Error(result.error||'Could not save settings.');message('Saved. Future councils will use these settings.');dialog.close();
 document.getElementById('status-msg').textContent='Council settings saved · applies to future runs';
 }catch(error){message(error.message,true);}finally{lock(false);}};
})();
"""
