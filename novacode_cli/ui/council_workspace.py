"""Live workspace controls inspired by the supplied Council HTML reference."""

WORKSPACE_CONTROLS_HTML = r"""
<div class="council-toolbar">
 <label class="preset-control" for="council-preset">Council preset
 <select id="council-preset"><option value="saved">Saved council</option><option value="code">Code Audit</option><option value="arch">Architecture</option><option value="creative">Creative Debate</option></select></label>
 <span class="preset-note">Perspectives for this discussion · model settings retained</span>
</div>
<div class="pipeline" id="council-pipeline" aria-label="Council pipeline">
 <ol><li data-stage="analysis">1 <span>Parallel analysis</span></li><li data-stage="voting">2 <span>Voting & debate</span></li><li data-stage="synthesis">3 <span>Chairman synthesis</span></li></ol>
 <span id="pipeline-state" role="status">Ready to convene</span>
</div>
<div class="view-toolbar">
 <div role="tablist" aria-label="Council view"><button id="view-synthesis" role="tab" aria-selected="true" aria-controls="messages" data-council-view="synthesis">Synthesized consensus</button>
 <button id="view-debate" role="tab" aria-selected="false" tabindex="-1" aria-controls="messages" data-council-view="debate">Full debate</button></div>
 <button id="workspace-stop" type="button" hidden>Stop</button><button id="clear-view" type="button">Clear view</button>
</div>
"""

WORKSPACE_METRICS_HTML = r"""
<div class="council-metrics" aria-label="Discussion progress"><span>ADVISORS <strong id="metric-advisors">0 / 0</strong></span><span>VALID VOTES <strong id="metric-votes">0</strong></span><span>ELAPSED <strong id="metric-elapsed">0s</strong></span></div>
"""

WORKSPACE_CONTROLS_STYLE = r"""
body.council-app{--bg:#0b101b;--surface:#111b2c;--panel:#18243a;--boost:#21324a;--border:#29394f;--primary:#67dce9;--accent:#c3a5ef;--muted:#a1b1c6;--fg:#e6eef8;grid-template-columns:320px minmax(0,1fr)}
.council-app .workspace-sidebar{background:#0c1422}.council-app .workspace-brand .brand-icon{border-radius:9px;background:#152c3b;color:var(--primary);border-color:#325266}
.council-app .sidebar-primary{background:#152b3c;border-color:#315266}.council-app .welcome h2{font-family:Bahnschrift,'Segoe UI',sans-serif;font-size:clamp(27px,3.4vw,44px);font-weight:600;letter-spacing:-.035em}
.council-app .welcome{border:1px solid var(--border);border-radius:16px;background:var(--surface);padding:30px;max-width:730px}
.council-app .welcome p{font-size:14px}.council-app .welcome-icon{color:var(--primary)}
.council-toolbar,.view-toolbar{display:flex;align-items:center;gap:16px;padding:10px 24px;border-bottom:1px solid var(--border);flex-shrink:0;background:#0e1726}
.preset-control{display:flex;align-items:center;gap:12px;color:var(--muted);font:11px var(--utility)}
.preset-control select{max-width:190px;background:var(--panel);color:var(--fg);padding:8px 10px;border:1px solid var(--border);border-radius:6px}
.preset-note{font-size:11px;color:var(--muted)}.view-toolbar{justify-content:space-between;padding-block:8px}
.view-toolbar [role=tablist]{display:flex;gap:6px;min-width:0}.view-toolbar button{padding:8px 10px;border:1px solid transparent;border-radius:6px;color:var(--muted);background:transparent;font-size:12px}
.view-toolbar button[aria-selected=true]{border-color:#315d73;background:#123044;color:var(--primary)}.view-toolbar #workspace-stop{border-color:#775267;color:#f5a9b6}.view-toolbar #clear-view{border-color:var(--border);flex-shrink:0}
.pipeline{display:flex;align-items:center;justify-content:space-between;gap:15px;padding:13px 24px;border-bottom:1px solid var(--border);background:var(--bg);flex-shrink:0}
.pipeline ol{display:flex;gap:20px;list-style:none;padding:0;margin:0}.pipeline li{display:flex;align-items:center;gap:7px;font:11px var(--utility);color:var(--muted)}
.pipeline li[data-state=active]{color:var(--primary)}.pipeline li[data-state=complete]{color:var(--success)}
#pipeline-state{font-size:11px;color:var(--muted);text-align:right}.pipeline[data-state=error] #pipeline-state{color:var(--error)}
body.council-app #bench .seat{grid-template-columns:34px minmax(0,1fr);gap:4px 10px;padding:12px;border-radius:10px;background:#111d2e}
body.council-app #bench .seat[data-chairman=true]{border-color:#725794;background:#211c36}
.seat-details{grid-column:1/-1;border-top:1px solid var(--border);padding-top:8px;margin-top:5px;font-size:11px;line-height:1.7;color:var(--muted);overflow-wrap:anywhere}
.seat-details>span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.seat-details strong{color:var(--fg);font-weight:400;display:block}.council-metrics{display:flex;justify-content:space-between;gap:7px;padding:14px 0 0;border-top:1px solid var(--border);font:9px var(--utility);color:var(--muted)}
.council-metrics strong{display:block;font-size:13px;color:var(--primary);margin-top:5px;font-weight:500}
#messages[data-view=synthesis]>.agent:not(.chairman-stream),#messages[data-view=synthesis]>.vote-row,#messages[data-view=synthesis]>.phase{display:none}
#messages[data-view=debate]{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-content:start;gap:16px}#messages[data-view=debate]>*{grid-column:1/-1}#messages[data-view=debate]>.agent:not(.chairman-stream){grid-column:auto;align-self:start}#messages[data-view=debate]>.verdict{display:none}.synthesis-wait{padding:20px;border:1px dashed var(--border);border-radius:10px;color:var(--muted);font-size:13px}
#messages[data-view=debate]>.synthesis-wait{display:none}.council-app #messages{padding-top:24px}
.council-app .clarification{background:#132d3b;border-color:#3c7585}.council-app .clarification button{background:#203e52;border-color:#426479}
.council-app .verdict{background:#162337;border-color:#726099}.council-app #decision-options,.council-app #decision-criteria{background:#0b1421}
@media(max-width:1050px){#messages[data-view=debate]{grid-template-columns:minmax(0,1fr)}.preset-note{display:none}.pipeline{flex-wrap:wrap;gap:8px}#pipeline-state{text-align:left}.pipeline ol{gap:12px}}
@media(max-width:760px){body.council-app{grid-template-columns:minmax(0,1fr)}.council-toolbar,.view-toolbar,.pipeline{padding:9px 12px}.pipeline li{font-size:9px}.pipeline ol{gap:10px}.pipeline li span{max-width:78px}.view-toolbar{gap:5px}.view-toolbar button{font-size:10px;padding:8px 6px}.council-app .welcome{padding:20px}.council-app .sidebar-footer{padding:12px 18px}}
"""

WORKSPACE_CONTROLS_SCRIPT = r"""
const workspaceViewButtons=[...document.querySelectorAll('[data-council-view]')];
function switchCouncilView(view){messages.dataset.view=view;messages.setAttribute('aria-labelledby','view-'+view);workspaceViewButtons.forEach(button=>{const selected=button.dataset.councilView===view;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});}
workspaceViewButtons.forEach((button,index)=>{button.onclick=()=>switchCouncilView(button.dataset.councilView);button.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?1:1-index;workspaceViewButtons[next].click();workspaceViewButtons[next].focus();};});
switchCouncilView('synthesis');
let workspaceStarted=0,workspaceElapsed=0,workspaceAnswered=new Set(),workspaceTotal=0,workspaceVotes=0,workspaceCompleted=false;
function updateWorkspaceMetrics(){ $('metric-advisors').textContent=workspaceAnswered.size+' / '+workspaceTotal;$('metric-votes').textContent=workspaceVotes;$('metric-elapsed').textContent=Math.floor((workspaceStarted?Date.now()-workspaceStarted:workspaceElapsed)/1000)+'s'; }
setInterval(updateWorkspaceMetrics,1000);
function workspaceStage(stage,label){const order=['analysis','voting','synthesis'],index=order.indexOf(stage);$('council-pipeline').dataset.state=stage;
 document.querySelectorAll('[data-stage]').forEach(node=>{const position=order.indexOf(node.dataset.stage);node.dataset.state=stage==='complete'?'complete':position===index?'active':position<index?'complete':'waiting';});$('pipeline-state').textContent=label;}
function workspaceBegin(){workspaceStarted=Date.now();workspaceElapsed=0;workspaceAnswered.clear();workspaceVotes=0;workspaceCompleted=false;workspaceStage('analysis','Broadcasting the prompt…');updateWorkspaceMetrics();
 const pending=el('div','synthesis-wait');pending.textContent='Your council is working. Follow individual responses in Full debate; the consolidated answer will appear here.';messages.appendChild(pending);}
function workspaceEnd(){if(!workspaceCompleted&&agentEls.chairman){agentEls.chairman.wrap.querySelector('.who-tag').textContent='incomplete synthesis';seatState('chairman','failed','incomplete');}if(workspaceStarted){workspaceElapsed=Date.now()-workspaceStarted;workspaceStarted=0;}if(!workspaceCompleted&&$('council-pipeline').dataset.state!=='error')workspaceStage('stopped','Discussion stopped');messages.querySelectorAll('.synthesis-wait').forEach(node=>node.remove());updateWorkspaceMetrics();}
let workspacePanelRequest=0;
async function loadCouncilPanel(){const request=++workspacePanelRequest;try{const response=await fetch('/api/council/panel?preset='+encodeURIComponent($('council-preset').value),{signal:AbortSignal.timeout(10000)});if(!response.ok)throw Error();const panel=await response.json();if(es||request!==workspacePanelRequest||!panel.agents)return;buildBench([...panel.agents,...(panel.chairman?[panel.chairman]:[])]);workspaceTotal=panel.agents.length;updateWorkspaceMetrics();}catch(_){setStatus('Advisor preview unavailable. You can retry by changing the preset.');}}
$('council-preset').onchange=()=>{workspaceAnswered.clear();workspaceVotes=0;workspaceElapsed=0;workspaceStage('ready','Ready to convene');loadCouncilPanel();};loadCouncilPanel();
$('workspace-stop').onclick=()=>$('stop-council').click();
$('clear-view').onclick=()=>{if(es)return;messages.replaceChildren();const welcome=el('div','welcome');const title=el('h2');title.textContent='Ready for the next question.';const note=el('p');note.textContent='View cleared. The council keeps this discussion’s context. Use New discussion to reset it.';welcome.append(title,note);messages.appendChild(welcome);};
"""
