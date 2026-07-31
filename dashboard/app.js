const PARAMS = new URLSearchParams(location.search);

// The dashboard is its own app, so the control plane is a different origin. The
// base URL is overridable with ?api= for pointing a local copy at the live box.
const API = (PARAMS.get('api') || 'https://api-coolify.bogdanripa.com').replace(/\/$/, '');

// The key lives in this browser, not in the URL. A key arriving as ?key= is
// stored and then removed by navigating to '/', so it does not sit in the
// address bar, in history, or in a referrer header on the way to anywhere else.
const KEY_STORE = 'pironman.key';
let KEY = localStorage.getItem(KEY_STORE) || '';

if (PARAMS.get('key')) {
  KEY = PARAMS.get('key');
  localStorage.setItem(KEY_STORE, KEY);
  // Drop only the key, keeping anything else (?api=), and use replace() so the
  // key-bearing URL does not stay in history either.
  PARAMS.delete('key');
  const rest = PARAMS.toString();
  location.replace('/' + (rest ? '?' + rest : ''));
}

function forgetKey(message) {
  KEY = '';
  localStorage.removeItem(KEY_STORE);
  askForKey(message);
}

function askForKey(message) {
  $('content').style.display = 'none';
  $('msg').style.display = 'none';
  $('forget').style.display = 'none';
  $('keyForm').style.display = '';
  const err = $('keyError');
  err.style.display = message ? '' : 'none';
  err.textContent = message || '';
  $('keyInput').value = '';
  $('keyInput').focus();
}
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

class AuthError extends Error {}

async function api(path) {
  const r = await fetch(API + path, { headers: { Authorization: 'Bearer ' + KEY } });
  if (r.status === 401 || r.status === 403) throw new AuthError('key rejected');
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
      `<td>${({
          static:  '<span class="muted">static</span>',
          running: '<span class="dot up"></span>running',
          asleep:  '<span class="dot sleep"></span>asleep',
          stopped: '<span class="dot down"></span>stopped',
        })[a.state || (a.running === null ? 'static' : a.running ? 'running' : 'stopped')]}</td>` +
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
let _recent = [], _recentShown = 10;
const _RECENT_STEP = 20;
function renderRecent() {
  const rows = _recent;
  if (!rows.length) { $('recent').innerHTML = '<span class="muted">No recent requests in the proxy buffer.</span>'; return; }
  const sc = s => (s >= 500) ? 'bad' : (s >= 400 ? 'warn' : '');
  const shown = rows.slice(0, _recentShown);
  const table =
    '<div style="overflow-x:auto"><table><tr><th>Time (UTC)</th><th>App</th><th>Method</th><th>URL</th><th class="n">Status</th></tr>' +
    shown.map(r =>
      `<tr><td class="muted">${esc((r.time || '').slice(0, 19).replace('T', ' '))}</td>` +
      `<td>${esc(r.app || '')}</td>` +
      `<td>${esc(r.method || '')}</td>` +
      `<td style="max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.path || '')}</td>` +
      `<td class="n ${sc(r.status)}">${r.status ?? ''}</td></tr>`).join('') +
    '</table></div>';
  const remaining = rows.length - shown.length;
  const more = remaining > 0
    ? `<div style="margin-top:12px"><a class="link" onclick="moreRecent(event)">Load more (${remaining} more)</a></div>`
    : '';
  $('recent').innerHTML = table + more;
}
window.moreRecent = e => { if (e) e.preventDefault(); _recentShown += _RECENT_STEP; renderRecent(); };
async function loadRecent() {
  _recentShown = 10;
  try {
    const d = await api('/analytics/recent?' + appQ() + 'limit=200');
    _recent = d.requests || [];
    renderRecent();
  } catch (e) { $('recent').innerHTML = '<span class="muted">Unavailable: ' + esc(e.message) + '</span>'; }
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
    if (e instanceof AuthError) { forgetKey('That key was rejected. Try another.'); return; }
    $('content').style.display = 'none';
    $('msg').className = 'err';
    $('msg').style.display = '';
    $('msg').textContent = 'Could not load analytics: ' + e.message;
  }
}

$('app').onchange = refresh;
$('days').onchange = refresh;
$('forget').onclick = () => forgetKey('');

$('keyEntry').onsubmit = e => {
  e.preventDefault();
  const v = $('keyInput').value.trim();
  if (!v) return;
  KEY = v;
  localStorage.setItem(KEY_STORE, KEY);
  boot();
};

async function boot() {
  if (!KEY) { askForKey(''); return; }
  $('keyForm').style.display = 'none';
  $('forget').style.display = '';
  $('msg').style.display = '';
  $('msg').className = 'muted';
  $('msg').textContent = 'Loading…';
  try { await loadApps(); } catch (e) {
    if (e instanceof AuthError) { forgetKey('That key was rejected. Try another.'); return; }
  }
  refresh();
}

boot();
