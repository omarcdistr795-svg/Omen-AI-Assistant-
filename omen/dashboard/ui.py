"""Inline HUD dashboard UI. No external assets, no CDN, no secrets."""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Omen HUD</title>
  <style>
    :root { --bg:#071018; --panel:#0d1a24; --panel2:#102231; --line:#1f3b52; --text:#d7e7f5; --muted:#7ea0b8; --cyan:#62d7ff; --green:#7dffb2; --amber:#ffd166; --red:#ff6b7a; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; background:radial-gradient(circle at top,#10263a 0,#071018 45%,#040a10 100%); color:var(--text); }
    header { display:flex; justify-content:space-between; align-items:center; padding:16px 20px; border-bottom:1px solid var(--line); background:rgba(7,16,24,.85); position:sticky; top:0; backdrop-filter:blur(6px); }
    h1 { margin:0; font-size:20px; letter-spacing:.18em; color:var(--cyan); }
    .sub { color:var(--muted); font-size:12px; }
    .pill { display:inline-block; padding:2px 8px; border:1px solid var(--line); border-radius:999px; font-size:11px; margin-left:6px; color:var(--muted); }
    .ok { color:var(--green); border-color:#28543a; } .warn { color:var(--amber); border-color:#5a4a22; } .bad { color:var(--red); border-color:#5a2430; }
    main { display:grid; grid-template-columns:repeat(12,1fr); gap:14px; padding:16px; }
    section { background:linear-gradient(180deg,var(--panel),var(--panel2)); border:1px solid var(--line); border-radius:10px; padding:12px; min-height:120px; box-shadow:0 0 0 1px rgba(98,215,255,.03); }
    h2 { margin:0 0 10px; font-size:12px; color:var(--cyan); letter-spacing:.12em; text-transform:uppercase; }
    .span-4 { grid-column:span 4; } .span-6 { grid-column:span 6; } .span-8 { grid-column:span 8; } .span-12 { grid-column:span 12; }
    .kv { display:grid; grid-template-columns:160px 1fr; gap:4px 10px; font-size:12px; }
    .k { color:var(--muted); } .v { white-space:pre-wrap; word-break:break-word; }
    .feed { max-height:320px; overflow:auto; font-size:12px; }
    .event { padding:7px 0; border-bottom:1px solid rgba(31,59,82,.5); }
    .event:last-child { border-bottom:none; }
    .meta { color:var(--muted); font-size:11px; }
    button { background:#12293b; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:6px 10px; cursor:pointer; font:inherit; font-size:12px; }
    button:hover { border-color:var(--cyan); } button.danger:hover { border-color:var(--red); color:var(--red); }
    input { width:100%; background:#091622; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; font-size:12px; }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    td,th { padding:6px 4px; border-bottom:1px solid rgba(31,59,82,.45); text-align:left; vertical-align:top; }
    th { color:var(--muted); font-weight:normal; }
    .row { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
    .err { color:var(--red); font-size:12px; white-space:pre-wrap; }
    pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:11px; color:#bed3e3; }
    @media (max-width:1000px) { .span-4,.span-6,.span-8 { grid-column:span 12; } }
  </style>
</head>
<body>
  <header>
    <div><h1>OMEN HUD</h1><div class="sub">Local dashboard · reads/acts through public interfaces only</div></div>
    <div id="top-status"><span class="pill">connecting</span></div>
  </header>
  <main>
    <section class="span-4"><h2>System Health</h2><div id="health" class="kv"></div></section>
    <section class="span-4"><h2>Usage / Cost</h2><div id="cost" class="kv"></div></section>
    <section class="span-4"><h2>Settings</h2><div id="settings" class="kv"></div></section>
    <section class="span-6"><h2>Live Activity Feed</h2><div id="feed" class="feed"></div></section>
    <section class="span-6"><h2>Approvals</h2><div id="approvals"></div></section>
    <section class="span-6"><h2>Alerts</h2><div id="alerts"></div></section>
    <section class="span-6"><h2>Memory Browser</h2>
      <div class="row"><input id="mem-q" placeholder="Search memory..." /><button onclick="searchMemory()">Search</button><button onclick="loadMemory()">Recent</button></div>
      <div id="memory" class="feed"></div>
    </section>
    <section class="span-12"><h2>Recent Actions</h2><div id="actions"></div></section>
  </main>
<script>
const token = new URLSearchParams(location.search).get('token') || localStorage.getItem('omen_dashboard_token') || '';
if (token) localStorage.setItem('omen_dashboard_token', token);
async function api(path, opts={}) {
  const headers = Object.assign({'Content-Type':'application/json'}, opts.headers||{});
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = {raw:text}; }
  if (!res.ok) throw new Error(data.error || data.raw || res.statusText);
  return data;
}
function esc(s) { return String(s==null?'':s).replace(/[&<>"']/g, c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }
function kv(obj, keys) { return keys.map(([k,label])=>`<div class="k">${esc(label||k)}</div><div class="v">${esc(typeof obj[k]==='object'?JSON.stringify(obj[k]):obj[k])}</div>`).join(''); }
function pill(text, cls='') { return `<span class="pill ${cls}">${esc(text)}</span>`; }
function renderStatus(s) {
  const r=s.resources||{}, c=s.cost||{}, h=s.system_health||'unknown', k=s.safety?.kill_switch?.active;
  const cls = h==='ok' && !k ? 'ok' : (k ? 'bad' : 'warn');
  document.getElementById('top-status').innerHTML = pill(h, cls) + pill(r.level||'unknown') + (k?pill('KILL SWITCH','bad'):'');
  document.getElementById('health').innerHTML = kv({health:h,level:r.level,ram:`${r.ram_used_gb}/${r.ram_total_gb} GB (${r.ram_percent}%)`,cpu:`${r.cpu_percent}%`,models:(s.models?.loaded_models||[]).join(', ')||'none',degradation:s.degradation?.level,kill:k}, [['health','health'],['level','pressure'],['ram','RAM'],['cpu','CPU'],['models','models'],['degradation','degradation'],['kill','kill switch']]);
  document.getElementById('cost').innerHTML = kv({spent:`$${c.spent_usd} / $${c.monthly_budget_usd}`,percent:`${c.percent_used}%`,cap:c.token_cap,fallback:c.local_fallback,requests:c.requests,blocked:c.blocked_requests}, [['spent','spent/budget'],['percent','used'],['cap','token cap'],['fallback','fallback'],['requests','requests'],['blocked','blocked']]);
  const set=s.settings||{};
  document.getElementById('settings').innerHTML = kv(set, Object.keys(set).map(k=>[k,k]));
  const alerts=(s.alerts_detail||[]); 
  document.getElementById('alerts').innerHTML = alerts.length ? '<table><tr><th>type</th><th>detail</th></tr>'+alerts.map(a=>`<tr><td>${esc(a.type||a)}</td><td><pre>${esc(JSON.stringify(a))}</pre></td></tr>`).join('')+'</table>' : '<div class="sub">No alerts.</div>';
  const acts=(s.recent_actions||[]);
  document.getElementById('actions').innerHTML = acts.length ? '<table><tr><th>time</th><th>event</th><th>tool</th><th>status</th><th>summary</th></tr>'+acts.map(a=>`<tr><td>${esc(a.timestamp||'')}</td><td>${esc(a.event_type||'')}</td><td>${esc(a.tool||'')}</td><td>${esc(a.status||'')}</td><td>${esc(a.summary||'')}</td></tr>`).join('')+'</table>' : '<div class="sub">No recent actions.</div>';
}
async function loadApprovals() {
  try {
    const data = await api('/api/approvals');
    const items = data.approvals||[];
    if (!items.length) { document.getElementById('approvals').innerHTML='<div class="sub">No pending approvals.</div>'; return; }
    document.getElementById('approvals').innerHTML = '<table><tr><th>token</th><th>tool</th><th>target</th><th>risk</th><th></th></tr>' + items.map(a=>`<tr><td><code>${esc(a.token_id)}</code></td><td>${esc(a.tool)}</td><td>${esc(a.target)}</td><td>${esc(a.risk)}</td><td><button onclick="approve('${esc(a.token_id)}')">Approve</button> <button class="danger" onclick="deny('${esc(a.token_id)}')">Deny</button></td></tr>`).join('') + '</table>';
  } catch(e) { document.getElementById('approvals').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
async function approve(id){ try{ await api('/api/approvals/'+encodeURIComponent(id)+'/approve',{method:'POST'}); await refresh(); }catch(e){ alert(e.message); } }
async function deny(id){ try{ await api('/api/approvals/'+encodeURIComponent(id)+'/deny',{method:'POST'}); await refresh(); }catch(e){ alert(e.message); } }
async function loadMemory(q='') {
  try {
    const data = await api(q?'/api/memory/search?q='+encodeURIComponent(q):'/api/memory');
    const items = data.items||[];
    document.getElementById('memory').innerHTML = items.length ? items.map(i=>`<div class="event"><div><strong>${esc(i.kind||'memory')}</strong> ${esc(i.id||'')}</div><div>${esc(i.text||JSON.stringify(i))}</div><div class="meta">${esc(i.created_at||'')} ${esc(i.author||'')}</div></div>`).join('') : '<div class="sub">No memory items.</div>';
  } catch(e) { document.getElementById('memory').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
function searchMemory(){ loadMemory(document.getElementById('mem-q').value.trim()); }
function addFeed(ev) {
  const feed = document.getElementById('feed');
  const d = document.createElement('div'); d.className='event';
  d.innerHTML = `<div><strong>${esc(ev.event_type||'event')}</strong> ${esc(ev.tool||'')} <span class="meta">${esc(ev.status||'')}</span></div><div>${esc(ev.summary||'')}</div><div class="meta">${esc(ev.timestamp||'')}</div>`;
  feed.prepend(d); while (feed.children.length > 100) feed.removeChild(feed.lastChild);
}
async function refresh() {
  try { const s = await api('/api/status'); renderStatus(s); await loadApprovals(); }
  catch(e) { document.getElementById('top-status').innerHTML = pill('auth/error','bad'); document.getElementById('health').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
function startEvents() {
  const url = '/api/events' + (token ? '?token='+encodeURIComponent(token) : '');
  const es = new EventSource(url);
  es.onmessage = (e)=>{ try { addFeed(JSON.parse(e.data)); } catch {} };
  es.onerror = ()=>{ es.close(); setTimeout(startEvents, 3000); };
}
refresh(); loadMemory(); startEvents(); setInterval(refresh, 5000);
</script>
</body></html>
"""
