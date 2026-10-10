/* AUTO-agent observatory: token-gated polling frontend. No secrets baked in. */
(function(){
"use strict";
const $=s=>document.querySelector(s);
const state={tab:"overview",page:1,per:25,f:{},demo:null,timer:null,lastOk:0};
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const token=()=>sessionStorage.getItem("dash_token")||"";
const demoMode=()=>new URLSearchParams(location.search).has("demo");

async function api(path,opts){
  opts=opts||{};
  opts.headers=Object.assign({"Authorization":"Bearer "+token()},opts.headers||{});
  const r=await fetch(path,opts);
  if(r.status===401){showErr("unauthorized — set the operator token");throw new Error("auth");}
  if(!r.ok){const t=await r.text().catch(()=> "");throw new Error("HTTP "+r.status+" "+t.slice(0,120));}
  return r.json();
}
function showErr(m){const b=$("#errBanner");if(!m){b.hidden=true;return;}b.textContent=m;b.hidden=false;}
function stamp(){const el=$("#refreshStamp");if(!state.lastOk){el.textContent="never refreshed";return;}
  el.textContent="refreshed "+new Date(state.lastOk).toLocaleTimeString();}
function healthDot(level){const d=$("#healthDot");d.className="dot "+(level||"");}
function money(n){return "$"+Number(n||0).toFixed(2);}
function bar(pct,cls){pct=Math.max(0,Math.min(100,pct));return '<div class="bar"><i class="'+(cls||"")+'" style="width:'+pct+'%"></i></div>';}

async function load(){
  showErr(null);
  if(demoMode()&&!state.demo){
    try{const r=await fetch("export.json");state.demo=await r.json();$("#demoBanner").hidden=false;}
    catch(e){showErr("demo snapshot missing — run main.py --export and place export.json next to index.html");}
  }
  try{
    if(state.demo){renderAll(state.demo);}
    else{
      const [ov,ev]=await Promise.all([api("/api/overview"),api(eventsURL())]);
      renderAll({overview:ov,events:ev});
      state.lastOk=Date.now();stamp();healthDot(ov.stale?"warn":"ok");
      $("#staleBanner").hidden=!ov.stale;
    }
  }catch(e){if(String(e.message)!=="auth")showErr("backend unreachable: "+e.message);healthDot("err");}
}
function eventsURL(){
  const p=new URLSearchParams({page:state.page,per_page:state.per});
  if(state.f.sev)p.set("severity",state.f.sev);
  if(state.f.cat)p.set("category",state.f.cat);
  if(state.f.since)p.set("since",state.f.since);
  if(state.f.q)p.set("search",state.f.q);
  return "/api/events?"+p.toString();
}

function renderAll(d){
  const ov=d.overview;
  renderOverview(ov);
  if(d.goals)renderGoals({goals:d.goals});
  if(d.revenue)renderRevenue(d.revenue);
  if(d.infra)renderInfra(d.infra);
  if(d.events)renderEvents(d.events);
  renderControls(ov);
  if(!state.demo){api("/api/goals").then(g=>renderGoals(g)).catch(()=>{});
    api("/api/revenue").then(r=>renderRevenue(r)).catch(()=>{});
    api("/api/infra").then(i=>renderInfra(i)).catch(()=>{});}
}
function renderOverview(ov){
  if(!ov)return;
  const pct=ov.target_usd?100*ov.verified_gross_usd/ov.target_usd:0;
  const dl=new Date(ov.deadline_at*1000).toLocaleString();
  const st=new Date(ov.started_at*1000).toLocaleString();
  $("#tab-overview").innerHTML=
   '<div class="grid">'
  +'<div class="card"><h3>status</h3><div class="big">'+esc(ov.status)+'</div><div class="mut">'+esc(ov.terminal_reason||"active")+(ov.kill_switch?' · paused (kill switch)':"")+'</div></div>'
  +'<div class="card"><h3>revenue</h3><div class="big">'+money(ov.verified_gross_usd)+' <span class="mut">/ '+money(ov.target_usd)+'</span></div>'+bar(pct,pct>=100?"ok":"")+ stunt(ov)+'</div>'
  +'<div class="card"><h3>time</h3><div>elapsed <b>'+ov.elapsed_hours.toFixed(1)+'h</b> · left <b>'+ov.remaining_hours.toFixed(1)+'h</b></div><div class="mut">start '+esc(st)+'<br>deadline '+esc(dl)+'</div></div>'
  +'<div class="card"><h3>spend</h3><div>LLM '+money(ov.usage.llm_spend_usd)+' · infra '+money(ov.usage.infra_spend_usd)+'</div><div class="mut">calls '+ov.usage.llm_calls+' llm / '+ov.usage.tool_calls+' tools</div></div>'
  +'</div>'
  +'<div class="card"><h3>right now</h3><div>'+esc((ov.current_goal||{}).description||"no active goal")+'</div><div class="mut">next: '+esc(ov.next_action||"-")+'</div></div>'
  +'<div class="grid"><div class="card"><h3>last success</h3><div>'+esc(JSON.stringify(ov.last_ok||"none"))+'</div></div>'
  +'<div class="card"><h3>latest error</h3><div class="'+(ov.last_error?"err":"")+'">'+esc((ov.last_error||{}).summary||(ov.last_error?JSON.stringify(ov.last_error):"none"))+'</div></div></div>';
  function stunt(){return "";}
}
function renderEvents(d){
  const list=$("#evList");
  if(!d.events||!d.events.length){list.innerHTML='<div class="empty">no events match — the stream starts recording on the next agent cycle</div>';}
  else list.innerHTML=d.events.map(e=>'<div class="ev"><span class="sev '+esc(e.severity)+'">'+esc(e.severity)+'</span> '
    +' <b>'+esc(e.category)+'</b> '+esc(e.summary)
    +'<div class="meta">'+new Date(e.ts*1000).toLocaleString()+' · goal #'+e.goal_id+' · cycle '+e.cycle+' · '+esc(e.event_id||"")+'</div>'
    +'<details><summary>details</summary><pre>'+esc(e.details_json||"{}")+'</pre></details></div>').join("");
  const pages=Math.max(1,Math.ceil((d.total||0)/state.per));
  $("#pgInfo").textContent="page "+state.page+" / "+pages+" · "+(d.total||0)+" events";
  $("#evCount").textContent=(d.total||0)+" events";
}
async function renderGoals(d){
  const el=$("#tab-goals");
  if(!d.goals.length){el.innerHTML='<div class="empty">no goals yet — the agent generates its own on the first cycle</div>';return;}
  el.innerHTML=d.goals.map(g=>'<div class="card"><h3>#'+g.id+' · '+esc(g.status)+' · score '+g.score+'</h3>'
    +'<div>'+esc(g.description)+'</div>'
    +'<div class="mut">why: novelty '+g.why_selected.novelty+', feasibility '+g.why_selected.feasibility+', usefulness '+g.why_selected.usefulness+', cost '+g.why_selected.cost+'</div>'
    +(g.tasks&&g.tasks.length?'<details><summary>subtasks ('+g.tasks.length+')</summary><ul>'+g.tasks.map(t=>'<li>'+esc(t.title)+' — '+esc(t.status)+(t.evidence?' · '+esc(t.evidence):"")+'</li>').join("")+'</ul></details>':"")
    +'</div>').join("");
}
function renderRevenue(r){
  const rows=(r.transactions||[]).map(t=>'<tr><td>'+esc(String(t.id).slice(0,12))+'</td><td>'+money(t.amount)+' '+esc(t.currency)+'</td><td>'+esc(t.status)+'</td><td>'+(t.genuine?'<span class="ok">genuine</span>':'<span class="mut">excluded</span>')+'</td></tr>').join("");
  $("#tab-revenue").innerHTML='<div class="grid">'
    +'<div class="card"><h3>gross (provider)</h3><div class="big">'+money(r.gross_usd)+'</div><div class="mut">basis: '+esc(r.basis)+'</div></div>'
    +'<div class="card"><h3>refunds</h3><div class="big">'+money(r.refunded_usd)+'</div><div class="mut">provider rows flagged refunded/disputed</div></div>'
    +'<div class="card"><h3>fees</h3><div class="big">'+money(r.fees_usd)+'</div><div class="mut">basis: '+esc(r.fees_basis)+' — '+(r.fees_basis==="estimate"?"DODO_FEE_PCT applied":"set DODO_FEE_PCT for an estimate")+'</div></div>'
    +'<div class="card"><h3>profit</h3><div class="big">'+money(r.profit_estimate_usd)+'</div><div class="mut">basis: '+esc(r.profit_basis)+' (gross − refunds − fees − spend)</div></div>'
    +'</div><div class="card"><h3>transactions</h3>'+(rows?'<table><tr><th>id</th><th>amount</th><th>status</th><th>counts?</th></tr>'+rows+'</table>':'<div class="empty">no provider transactions mirrored yet</div>')+'</div>'
    +'<div class="card"><h3>visitors &amp; conversion</h3><div class="mut">unavailable — PostHog not configured. Local funnel stubs are not shown as analytics.</div></div>';
}
function renderInfra(v){
  const q=Object.entries(v.queue||{}).map(([k,x])=>'<tr><td>'+esc(k)+'</td><td>'+x.count+'</td><td>'+x.retries+'</td></tr>').join("");
  const fj=(v.failed_jobs||[]).map(j=>'<tr><td>'+j.id+'</td><td>'+esc(j.kind)+'</td><td>'+j.attempts+'</td></tr>').join("");
  $("#tab-infra").innerHTML='<div class="grid">'
    +'<div class="card"><h3>failures</h3><div class="big">'+v.total_failures+'</div><div class="mut">streak '+v.consecutive_failures+' · llm fails '+v.llm_failures_recent+'</div></div>'
    +'<div class="card"><h3>services</h3><div>github: '+(v.github.configured?"<span class='ok'>token set</span>":"<span class='mut'>awaiting credentials</span>")+'</div><div>cloudflare: '+(v.cloudflare.configured?"<span class='ok'>token set</span>":"<span class='mut'>awaiting credentials</span>")+'</div></div>'
    +'<div class="card"><h3>restarts</h3><div class="mut">'+(v.restarts&&v.restarts.length?esc(v.restarts[0].summary):"none recorded")+'</div></div>'
    +'</div><div class="card"><h3>queue</h3>'+(q?'<table><tr><th>status</th><th>jobs</th><th>retries</th></tr>'+q+'</table>':'<div class="empty">queue empty</div>')+'</div>'
    +'<div class="card"><h3>failed jobs</h3>'+(fj?'<table><tr><th>id</th><th>kind</th><th>attempts</th></tr>'+fj+'</table>':'<div class="empty">none</div>')+'</div>';
}
function renderControls(ov){
  $("#tab-controls").innerHTML='<div class="card"><h3>operator controls</h3>'
    +'<div class="mut">Every action routes through the independent supervisor and is audit-logged. Caps, deadline, and revenue records cannot be changed here.</div>'
    +'<div class="row"><button data-act="pause">pause</button><button data-act="resume">resume</button><button data-act="shutdown">safe shutdown</button><button data-act="diagnostic">diagnostic run</button></div>'
    +'<div id="ctlOut" class="mut"></div></div>'
    +'<div class="card"><h3>pending approvals</h3><div id="apprList" class="mut">loading…</div></div>'
    +'<div class="card"><h3>blocked tasks</h3><div id="blockedList" class="mut">loading…</div></div>';
  document.querySelectorAll("#tab-controls [data-act]").forEach(b=>b.onclick=()=>doControl(b.dataset.act));
  if(!state.demo){
    api("/api/approvals").then(a=>{
      $("#apprList").innerHTML=a.pending.length?a.pending.map(p=>'<div>#'+p.id+' <b>'+esc(p.kind)+'</b> '+esc(p.context_json||"")+' <button data-ap="'+p.id+'" data-d="1">approve</button> <button data-ap="'+p.id+'" data-d="0">deny</button></div>').join(""):'<div class="empty">none pending</div>';
      document.querySelectorAll("[data-ap]").forEach(x=>x.onclick=()=>decideApproval(x.dataset.ap,x.dataset.d));
    }).catch(()=>{$("#apprList").textContent="unavailable";});
    api("/api/goals").then(g=>{
      const blocked=(g.goals||[]).filter(x=>["blocked","failed","abandoned"].includes(x.status));
      $("#blockedList").innerHTML=blocked.length?blocked.map(x=>'<div>#'+x.id+' '+esc(x.status)+' — '+esc(x.description)+'</div>').join(""):'<div class="empty">none</div>';
    }).catch(()=>{$("#blockedList").textContent="unavailable";});
  }
}
async function doControl(act){
  try{const r=await api("/api/controls/"+act,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({actor:"dashboard"})});
    $("#ctlOut").textContent=JSON.stringify(r);load();}
  catch(e){$("#ctlOut").textContent="failed: "+e.message;}
}
async function decideApproval(id,approve){
  try{await api("/api/approvals/"+id+"/decide",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({approve:approve==="1",actor:"dashboard"})});renderControls();}
  catch(e){showErr("approval failed: "+e.message);}
}
document.querySelectorAll("#tabs button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("#tabs button").forEach(x=>x.classList.remove("on"));b.classList.add("on");
  state.tab=b.dataset.tab;
  document.querySelectorAll(".tab").forEach(s=>s.hidden=s.id!=="tab-"+state.tab);
});
$("#filters").onsubmit=e=>{e.preventDefault();state.page=1;
  state.f={q:$("#fSearch").value,sev:$("#fSev").value,cat:$("#fCat").value,
    since:$("#fRange").value?Date.now()/1000-Number($("#fRange").value):null};load();};
$("#prevPg").onclick=()=>{if(state.page>1){state.page--;load();}};
$("#nextPg").onclick=()=>{state.page++;load();};
$("#loginBtn").onclick=()=>{$("#loginModal").hidden=false;};
$("#loginCancel").onclick=()=>{$("#loginModal").hidden=true;};
$("#loginForm").onsubmit=e=>{e.preventDefault();sessionStorage.setItem("dash_token",$("#tokenInput").value);$("#tokenInput").value="";$("#loginModal").hidden=true;load();};
load();setInterval(load,5000);
})();
