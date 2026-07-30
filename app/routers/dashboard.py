"""The analytics dashboard — a self-contained HTML page served by the control
plane itself, so it is always live and needs no external hosting.

The page carries no data and no secret: it is plain markup. It reads the API key
from its own URL (`?key=…`, the same key the connector uses) and its JavaScript
calls the read-only /analytics/* JSON endpoints with that key as a bearer token.
So the page is public but the numbers still require the key — open it at
https://api-coolify.bogdanripa.com/analytics/dashboard?key=<PAAS_KEY>.

Charts are drawn with inline SVG/CSS — no external scripts or fonts — so it works
offline and behind a strict CSP.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

# Not on the require_key router and hidden from the schema, so it is a plain page,
# never an MCP tool. include_in_schema=False keeps fastapi-mcp from seeing it.
router = APIRouter(tags=["analytics"])

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pironman Analytics</title>
<style>
  :root {
    --bg:#0f1115; --panel:#181b22; --line:#262b36; --fg:#e6e9ef; --mut:#8b93a3;
    --accent:#5b9dff; --accent2:#3ecf8e; --bar:#2a3140;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f5f6f8; --panel:#fff; --line:#e5e8ee; --fg:#1a1d24;
            --mut:#697488; --bar:#e9edf4; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { display:flex; flex-wrap:wrap; gap:12px; align-items:center;
           padding:18px 22px; border-bottom:1px solid var(--line); }
  h1 { font-size:18px; margin:0; font-weight:600; }
  h1 span { color:var(--mut); font-weight:400; }
  .controls { margin-left:auto; display:flex; gap:8px; }
  select { background:var(--panel); color:var(--fg); border:1px solid var(--line);
           border-radius:8px; padding:7px 10px; font:inherit; }
  main { padding:22px; max-width:1100px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:14px; margin-bottom:22px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
          padding:16px 18px; }
  .card .k { color:var(--mut); font-size:12px; text-transform:uppercase;
             letter-spacing:.04em; }
  .card .v { font-size:30px; font-weight:650; margin-top:6px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px;
           padding:18px; margin-bottom:22px; }
  .panel h2 { font-size:14px; margin:0 0 14px; font-weight:600; color:var(--mut);
              text-transform:uppercase; letter-spacing:.04em; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:500; }
  td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
  .cohort td { text-align:center; border:1px solid var(--bg); border-radius:4px; }
  .cohort td.lbl { text-align:left; white-space:nowrap; color:var(--mut); }
  .legend { display:flex; gap:16px; font-size:12px; color:var(--mut);
            margin-bottom:8px; }
  .legend i { display:inline-block; width:11px; height:11px; border-radius:3px;
              margin-right:5px; vertical-align:-1px; }
  .muted { color:var(--mut); }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
         margin-right:6px; vertical-align:1px; }
  .up { background:var(--accent2); } .down { background:#e5484d; }
  .warn { color:#e5a13a; } .bad { color:#e5484d; }
  .bar { height:6px; border-radius:3px; background:var(--bar); overflow:hidden; }
  .bar > span { display:block; height:100%; background:var(--accent); }
  svg { width:100%; height:220px; display:block; }
  .err { background:#3a1d1d; border:1px solid #6b2b2b; color:#ffb4b4;
         padding:14px 16px; border-radius:10px; }
</style>
</head>
<body>
<header>
  <h1>Pironman Analytics <span id="scopeLabel"></span></h1>
  <div class="controls">
    <select id="app"></select>
    <select id="days">
      <option value="7">Last 7 days</option>
      <option value="30" selected>Last 30 days</option>
      <option value="90">Last 90 days</option>
    </select>
  </div>
</header>
<main id="root">
  <div id="msg" class="muted">Loading…</div>
  <div id="content" style="display:none">
    <div class="cards" id="cards"></div>
    <div class="panel">
      <h2>Resources <span id="hostline" class="muted" style="font-weight:400;text-transform:none;letter-spacing:0"></span></h2>
      <div id="resources"><span class="muted">Loading live stats…</span></div>
    </div>
    <div class="panel">
      <h2>Daily traffic</h2>
      <div class="legend">
        <span><i style="background:var(--accent)"></i>Unique visitors</span>
        <span><i style="background:var(--bar)"></i>Hits</span>
      </div>
      <div id="chart"></div>
    </div>
    <div class="panel">
      <h2>Last HTTP calls <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">· live tail</span></h2>
      <div id="recent"><span class="muted">Loading…</span></div>
    </div>
    <div class="panel">
      <h2>Traffic sources <span id="botsline" class="muted" style="font-weight:400;text-transform:none;letter-spacing:0"></span></h2>
      <div id="agents"><span class="muted">Loading…</span></div>
    </div>
    <div class="panel" id="perAppPanel" style="display:none">
      <h2>By app</h2>
      <div id="perApp"></div>
    </div>
    <div class="panel">
      <h2>Weekly retention cohorts</h2>
      <div id="cohorts"></div>
    </div>
  </div>
</main>
<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function api(path) {
  const r = await fetch(path, { headers: { Authorization: 'Bearer ' + KEY } });
  if (!r.ok) throw new Error(path + ' → ' + r.status + ' ' + r.statusText);
  return r.json();
}
function appQ() {
  const a = $('app').value;
  return a ? 'app_id=' + encodeURIComponent(a) + '&' : '';
}

function renderCards(o) {
  const cards = [
    ['Unique visitors', o.unique_visitors],
    ['Hits', o.hits],
    ['DAU', o.dau], ['WAU', o.wau], ['MAU', o.mau],
  ];
  $('cards').innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="k">${k}</div><div class="v">${v.toLocaleString()}</div></div>`
  ).join('');
}

function renderChart(series) {
  const el = $('chart');
  if (!series.length) { el.innerHTML = '<p class="muted">No traffic in this window yet.</p>'; return; }
  const W = 900, H = 220, padB = 22, padT = 10, padX = 4;
  const n = series.length;
  const maxV = Math.max(1, ...series.map(d => d.unique_visitors));
  const maxH = Math.max(1, ...series.map(d => d.hits));
  const bw = (W - padX * 2) / n;
  const x = i => padX + i * bw;
  const yV = v => padT + (H - padT - padB) * (1 - v / maxV);
  const bars = series.map((d, i) =>
    `<rect x="${x(i) + bw * 0.15}" y="${padT + (H - padT - padB) * (1 - d.hits / maxH)}" ` +
    `width="${bw * 0.7}" height="${(H - padT - padB) * (d.hits / maxH)}" ` +
    `fill="var(--bar)" rx="1"/>`).join('');
  const pts = series.map((d, i) => `${x(i) + bw / 2},${yV(d.unique_visitors)}`).join(' ');
  const dots = series.map((d, i) =>
    `<circle cx="${x(i) + bw / 2}" cy="${yV(d.unique_visitors)}" r="2.5" fill="var(--accent)"/>`).join('');
  const labels = series.map((d, i) =>
    (n <= 14 || i % Math.ceil(n / 12) === 0)
      ? `<text x="${x(i) + bw / 2}" y="${H - 6}" text-anchor="middle" font-size="10" fill="var(--mut)">${d.day.slice(5)}</text>`
      : '').join('');
  el.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}` +
    `<polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>` +
    `${dots}${labels}</svg>`;
}

function renderPerApp(rows) {
  const panel = $('perAppPanel');
  if (!rows) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  $('perApp').innerHTML =
    '<table><tr><th>App</th><th class="n">Unique visitors</th><th class="n">Hits</th></tr>' +
    (rows.length ? rows.map(r =>
      `<tr><td>${esc(r.app_id)}</td><td class="n">${r.unique_visitors.toLocaleString()}</td>` +
      `<td class="n">${r.hits.toLocaleString()}</td></tr>`).join('')
      : '<tr><td colspan="3" class="muted">No traffic yet.</td></tr>') +
    '</table>';
}

function renderCohorts(data) {
  const el = $('cohorts');
  const cs = data.cohorts || [];
  if (!cs.length) { el.innerHTML = '<p class="muted">Not enough history yet — cohorts build up over weeks.</p>'; return; }
  const wk = data.weeks;
  let head = '<tr><th class="lbl">Cohort week</th><th class="n">Size</th>';
  for (let i = 0; i < wk; i++) head += `<th class="n">W${i}</th>`;
  head += '</tr>';
  const cell = pct => {
    const a = Math.max(0, Math.min(1, pct / 100));
    const bg = a === 0 ? 'transparent' : `rgba(62,207,142,${0.12 + a * 0.6})`;
    return bg;
  };
  const rows = cs.map(c => {
    let r = `<tr><td class="lbl">${esc(c.cohort_week)}</td><td class="n">${c.size}</td>`;
    for (let i = 0; i < wk; i++) {
      const p = c.retention_pct[i] ?? 0;
      const shown = c.size ? (i < c.retained.length ? c.retention_pct[i] + '%' : '') : '';
      r += `<td style="background:${cell(p)}">${i < c.retained.length && c.size ? shown : ''}</td>`;
    }
    return r + '</tr>';
  }).join('');
  el.innerHTML = `<div style="overflow-x:auto"><table class="cohort">${head}${rows}</table></div>`;
}

function fmtMb(v) {
  if (v == null) return '<span class="muted">—</span>';
  return v >= 1024 ? (v / 1024).toFixed(2) + ' GB' : v.toLocaleString() + ' MB';
}
function renderResources(data) {
  const apps = data.apps || [], h = data.host || {};
  if (h.mem_total_mb) {
    const usedPct = Math.round(100 * (h.mem_used_by_containers_mb || 0) / h.mem_total_mb);
    let line = `· host: ${h.ncpu ?? '?'} CPU · ` +
      `${(h.mem_used_by_containers_mb || 0).toLocaleString()} / ${h.mem_total_mb.toLocaleString()} MB RAM in containers (${usedPct}%)`;
    if (h.disk_total_gb) line += ` · disk ${h.disk_used_gb} / ${h.disk_total_gb} GB (${h.disk_used_pct}%)`;
    $('hostline').textContent = line;
  }
  const win = data.traffic_window_days;
  const ms = v => v != null ? v + '' : '<span class="muted">—</span>';
  let head = '<table><tr><th>App</th><th>Status</th><th class="n">CPU</th>' +
    '<th class="n">RAM</th><th class="n">Disk</th><th class="n">DB size</th>' +
    `<th class="n">Req ${win}d</th><th class="n">Err %</th>` +
    '<th class="n">p50 ms</th><th class="n">p95 ms</th></tr>';
  const rows = apps.map(a => {
    const t = a.traffic || {};
    const errCls = (t.server_error_pct > 0) ? 'bad' : (t.error_pct > 5 ? 'warn' : '');
    return '<tr>' +
      `<td>${esc(a.id)}</td>` +
      `<td><span class="dot ${a.running ? 'up' : 'down'}"></span>${a.running ? 'running' : 'stopped'}</td>` +
      `<td class="n">${a.cpu_pct != null ? a.cpu_pct + '%' : '<span class="muted">—</span>'}</td>` +
      `<td class="n">${a.mem_mb != null ? a.mem_mb.toLocaleString() + ' MB' : '<span class="muted">—</span>'}</td>` +
      `<td class="n">${fmtMb(a.disk_rw_mb)}</td>` +
      `<td class="n">${a.db_engine ? fmtMb(a.db_size_mb) : '<span class="muted">—</span>'}</td>` +
      `<td class="n">${(t.requests || 0).toLocaleString()}</td>` +
      `<td class="n ${errCls}">${(t.error_pct || 0)}%</td>` +
      `<td class="n">${ms(t.p50_ms)}</td>` +
      `<td class="n">${ms(t.p95_ms)}</td>` +
      '</tr>';
  }).join('');
  $('resources').innerHTML =
    '<div style="overflow-x:auto">' + head +
    (apps.length ? rows : '<tr><td colspan="8" class="muted">No apps.</td></tr>') +
    '</table></div>';
}
function renderBots(byAgent) {
  if (!byAgent) { $('botsline').textContent = ''; return; }
  const h = byAgent.human || {}, b = byAgent.bot || {};
  $('botsline').textContent =
    `· ${(h.visitors || 0).toLocaleString()} human vs ${(b.visitors || 0).toLocaleString()} bot visitors` +
    ` (${(h.hits || 0).toLocaleString()} / ${(b.hits || 0).toLocaleString()} hits)`;
}
function renderAgents(data) {
  const rows = data.agents || [];
  $('agents').innerHTML = rows.length ? (
    '<div style="overflow-x:auto"><table><tr><th>User agent</th><th>Type</th><th class="n">Hits</th></tr>' +
    rows.map(a =>
      `<tr><td style="max-width:640px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.ua)}</td>` +
      `<td>${a.is_bot ? '🤖 bot' : '🙂 human'}</td>` +
      `<td class="n">${(a.hits || 0).toLocaleString()}</td></tr>`).join('') +
    '</table></div>'
  ) : '<span class="muted">No user-agents recorded yet.</span>';
}
async function loadAgents(days) {
  try { renderAgents(await api('/analytics/agents?' + appQ() + 'days=' + days + '&limit=20')); }
  catch (e) { $('agents').innerHTML = '<span class="muted">Unavailable: ' + esc(e.message) + '</span>'; }
}
function renderRecent(data) {
  const rows = data.requests || [];
  const sc = s => (s >= 500) ? 'bad' : (s >= 400 ? 'warn' : '');
  $('recent').innerHTML = rows.length ? (
    '<div style="overflow-x:auto"><table><tr><th>Time (UTC)</th><th>App</th><th>Method</th><th>URL</th><th class="n">Status</th></tr>' +
    rows.map(r =>
      `<tr><td class="muted">${esc((r.time || '').slice(0, 19).replace('T', ' '))}</td>` +
      `<td>${esc(r.app || '')}</td>` +
      `<td>${esc(r.method || '')}</td>` +
      `<td style="max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.path || '')}</td>` +
      `<td class="n ${sc(r.status)}">${r.status ?? ''}</td></tr>`).join('') +
    '</table></div>'
  ) : '<span class="muted">No recent requests in the proxy buffer.</span>';
}
async function loadRecent() {
  try { renderRecent(await api('/analytics/recent?' + appQ() + 'limit=40')); }
  catch (e) { $('recent').innerHTML = '<span class="muted">Unavailable: ' + esc(e.message) + '</span>'; }
}
async function loadResources(days) {
  try {
    const d = await api('/stats/apps?traffic_days=' + days);
    renderResources(d);
  } catch (e) {
    $('resources').innerHTML = '<span class="muted">Stats unavailable: ' + esc(e.message) + '</span>';
  }
}

async function loadApps() {
  const o = await api('/analytics/overview?days=30');
  const sel = $('app');
  const apps = (o.per_app || []).map(r => r.app_id);
  sel.innerHTML = '<option value="">All apps</option>' +
    apps.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
}

async function refresh() {
  const days = $('days').value;
  $('scopeLabel').textContent = '· ' + ($('app').value || 'all apps') + ' · ' + days + 'd';
  try {
    const [o, ts, co] = await Promise.all([
      api('/analytics/overview?' + appQ() + 'days=' + days),
      api('/analytics/timeseries?' + appQ() + 'days=' + days),
      api('/analytics/cohorts?' + appQ() + 'weeks=' + Math.min(12, Math.ceil(days / 7) + 1)),
    ]);
    renderCards(o);
    renderChart(ts.series);
    renderBots(o.by_agent);
    renderPerApp($('app').value ? null : (o.per_app || []));
    renderCohorts(co);
    $('msg').style.display = 'none';
    $('content').style.display = '';
    loadResources(days);  // independent, slower probes — fill in after the rest
    loadAgents(days);
    loadRecent();
  } catch (e) {
    $('content').style.display = 'none';
    $('msg').className = 'err';
    $('msg').style.display = '';
    $('msg').textContent = KEY
      ? 'Could not load analytics: ' + e.message
      : 'Add your API key to the URL: …/analytics/dashboard?key=YOUR_KEY';
  }
}

$('app').onchange = refresh;
$('days').onchange = refresh;
(async () => {
  if (!KEY) { refresh(); return; }
  try { await loadApps(); } catch (e) { /* refresh() surfaces the error */ }
  refresh();
})();
</script>
</body>
</html>"""


@router.get("/analytics/dashboard", include_in_schema=False)
async def dashboard():
    return HTMLResponse(_PAGE)
