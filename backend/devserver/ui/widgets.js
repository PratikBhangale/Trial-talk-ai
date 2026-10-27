// Staff portal widgets: emergency alerts, OpenTelemetry, and the team (staff accounts) panel.
// They poll quietly (no request-log noise) while a staff user is signed in.

const Widgets = { timers: [], alertScope: 'active', seen: null, baseTitle: document.title, team: [], editing: null };
const SELF_ROUTES = new Set(['GET /staff/alerts', 'GET /staff/observability']); // the widgets' own polling

function startWidgets() {
  stopWidgets();
  Widgets.seen = null;
  loadAlerts(); loadOtel(); loadTeam(); loadUsage();
  Widgets.timers = [setInterval(loadAlerts, 4000), setInterval(loadOtel, 5000), setInterval(loadUsage, 15000)];
}
function stopWidgets() {
  Widgets.timers.forEach(clearInterval); Widgets.timers = [];
  document.title = Widgets.baseTitle;
}

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`;
}
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)(), o = ctx.createOscillator();
    o.frequency.value = 880; o.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.25);
  } catch { /* browsers block audio until the page has been interacted with */ }
}

// ------------------------------------------------------------ emergency alerts
const SEV_LABEL = { life_threatening: 'Life-threatening', urgent: 'Urgent', concern: 'Concern' };
const SRC_LABEL = { sos_button: 'SOS button', chat_keyword: 'Chat emergency keywords' };

async function loadAlerts() {
  if (!TT.user) return;
  let out;
  try { out = await api('GET', '/staff/alerts?status=' + Widgets.alertScope, undefined, { quiet: true }); } catch { return; }
  const { alerts, open } = out;
  const first = Widgets.seen === null;
  const fresh = alerts.filter(a => a.status === 'open' && Widgets.seen && !Widgets.seen.has(a.alert_id));
  Widgets.seen = new Set([...(Widgets.seen || []), ...alerts.map(a => a.alert_id)]);
  if (!first && fresh.length) {
    toast(`NEW EMERGENCY: ${fresh.map(a => `${a.participant_id} ${a.full_name} (${SEV_LABEL[a.severity]})`).join(', ')}`, true);
    beep();
  }
  $('#alertsPanel').classList.toggle('live', open > 0);
  $('#alertBadge').innerHTML = open ? `<span class="sev life_threatening pulse">${open} open</span>` : '<span class="chip ok">none open</span>';
  document.title = (open ? `(${open}) EMERGENCY - ` : '') + Widgets.baseTitle;
  $$('#alertTabs button').forEach(b => b.classList.toggle('on', b.dataset.scope === Widgets.alertScope));
  $('#alertList').innerHTML = alerts.length ? alerts.map(alertCard).join('')
    : `<span class="muted">${Widgets.alertScope === 'resolved' ? 'No resolved alerts in the last 30 days.' : 'No active emergency alerts at your sites.'}</span>`;
}

function alertCard(a) {
  const done = a.status === 'resolved';
  const id = esc(a.alert_id);
  return `<div class="alert-card sev-${esc(a.severity)} ${done ? 'done' : ''}">
    <div class="top">
      <span class="sev ${esc(a.severity)} ${a.status === 'open' ? 'pulse' : ''}">${esc(SEV_LABEL[a.severity] || a.severity)}</span>
      ${chip(a.status)}
      <a href="#" data-pid="${esc(a.participant_id)}"><b>${esc(a.participant_id)}</b> ${esc(a.full_name)}</a>
      <span class="muted">${esc(a.site_id)} · ${esc(a.phone_e164)}</span>
      <span class="spacer"></span><span class="muted" title="${esc(a.created_at)}">${esc(ago(a.created_at))}</span>
    </div>
    <div class="muted">${esc(SRC_LABEL[a.source] || a.source)}${a.press_count > 1 ? ` · pressed ${a.press_count} times, last ${esc(ago(a.last_pressed_at))}` : ''}${a.keywords?.length ? ` · ${esc(a.keywords.join(', '))}` : ''}</div>
    ${a.message ? `<div>"${esc(a.message)}"</div>` : ''}
    ${a.acknowledged_by ? `<div class="muted">Acknowledged by ${esc(a.acknowledged_by)} ${esc(ago(a.acknowledged_at))}</div>` : ''}
    ${done ? `<div class="muted">Resolved by ${esc(a.resolved_by)}: ${esc(a.resolution_note)}</div>` : `<div class="row">
      ${a.status === 'open' ? `<button class="small primary" data-alert="acknowledge" data-id="${id}">Acknowledge</button>` : ''}
      <button class="small" data-alert="resolve" data-id="${id}">Resolve...</button>
      <a href="tel:${esc(a.phone_e164)}" class="small">Call ${esc(a.phone_e164)}</a></div>`}
  </div>`;
}

async function alertAction(action, id) {
  if (action === 'resolve') {
    const note = prompt('Resolve this alert. What happened and what was done? (required, kept in the record)');
    if (!note) return;
    await api('POST', `/staff/alerts/${id}/resolve`, { note });
  } else await api('POST', `/staff/alerts/${id}/acknowledge`);
  loadAlerts();
}

// ------------------------------------------------------------ OpenTelemetry
async function loadOtel() {
  if (!TT.user) return;
  let o;
  try { o = await api('GET', '/staff/observability', undefined, { quiet: true }); } catch { return; }
  const body = $('#otelBody');
  if (!o.enabled) {
    body.innerHTML = `<span class="muted">OpenTelemetry is not active: ${esc(o.reason)}.<br>Install it with <code>pip install opentelemetry-sdk</code> and restart the dev server.</span>`;
    return;
  }
  const showSelf = $('#otelSelf').checked;
  const keep = (method, route) => showSelf || !SELF_ROUTES.has(`${method} ${route}`);

  // Metrics: http.server.request.duration histogram -> per-route rows
  const hist = o.metrics.find(m => m.name === 'http.server.request.duration');
  const rows = {};
  for (const p of hist?.points || []) {
    const a = p.attributes, method = a['http.request.method'], route = a['http.route'];
    if (!keep(method, route)) continue;
    const r = rows[`${method} ${route}`] ||= { route: `${method} ${route}`, requests: 0, errors5xx: 0, client4xx: 0, sum: 0, max: 0 };
    r.requests += p.count; r.sum += p.sum; r.max = Math.max(r.max, p.max || 0);
    const code = +a['http.response.status_code'];
    if (code >= 500) r.errors5xx += p.count; else if (code >= 400) r.client4xx += p.count;
  }
  const list = Object.values(rows).sort((x, y) => y.requests - x.requests);
  const total = list.reduce((n, r) => n + r.requests, 0), err = list.reduce((n, r) => n + r.errors5xx, 0);
  const sum = list.reduce((n, r) => n + r.sum, 0), max = Math.max(0, ...list.map(r => r.max));
  const counters = o.metrics.filter(m => m.kind === 'counter').flatMap(m => m.points.map(p => ({
    metric: m.name, attributes: Object.entries(p.attributes).map(([k, v]) => `${k}=${v}`).join(' '), value: p.value })));

  body.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${total}</b><span>requests</span></div>
      <div class="kpi"><b>${total ? (err / total * 100).toFixed(1) : '0.0'}%</b><span>5xx error rate</span></div>
      <div class="kpi"><b>${total ? Math.round(sum / total * 1000) : 0} ms</b><span>average latency</span></div>
      <div class="kpi"><b>${Math.round(max * 1000)} ms</b><span>slowest request</span></div>
    </div>
    <div class="muted" style="margin-bottom:4px">${esc(o.service)} · ${o.otlp_export ? 'also exporting via OTLP' : 'in-process window only (set OTEL_EXPORTER_OTLP_ENDPOINT to export)'}</div>
    ${counters.length ? `<h2 style="font-size:13px;margin-top:10px">Counters</h2>${table(counters)}` : ''}`;
}

// ------------------------------------------------------------ token usage
const nf = n => Number(n || 0).toLocaleString();

async function loadUsage() {
  if (!TT.user) return;
  let u;
  try { u = await api('GET', '/staff/usage?days=' + $('#usageDays').value, undefined, { quiet: true }); } catch { return; }
  const t = u.totals, max = Math.max(1, ...u.by_day.map(d => +d.input_tokens + +d.output_tokens));
  $('#usageBody').innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${nf(+t.input_tokens + +t.output_tokens)}</b><span>total tokens</span></div>
      <div class="kpi"><b>${nf(t.input_tokens)}</b><span>input</span></div>
      <div class="kpi"><b>${nf(t.output_tokens)}</b><span>output</span></div>
      <div class="kpi"><b>${nf(t.calls)}</b><span>agent replies</span></div>
    </div>
    ${u.by_day.length ? u.by_day.map(d => `<div class="wf" title="${nf(d.input_tokens)} in / ${nf(d.output_tokens)} out"><span class="name">${esc(String(d.day).slice(0, 10))}</span>
      <span class="track"><i style="left:0;width:${(+d.input_tokens + +d.output_tokens) / max * 100}%"></i></span><span class="dur">${nf(+d.input_tokens + +d.output_tokens)}</span></div>`).join('') : '<span class="muted">No agent replies in this period.</span>'}
    ${u.by_participant.length ? `<h2 style="font-size:13px;margin-top:10px">Top participants</h2>`
      + table(u.by_participant.map(p => ({ participant: `${p.participant_id} ${p.full_name}`, site: p.site_id, input: nf(p.input_tokens),
        output: nf(p.output_tokens), replies: p.calls }))) : ''}
    <div class="muted" style="margin-top:6px">Dev numbers are made up by the fake agent. The deployed agent will report real counts.</div>`;
}

// ------------------------------------------------------------ team (staff accounts)
async function loadTeam() {
  if (!TT.user) return;
  let out;
  try { out = await api('GET', '/staff/accounts', undefined, { quiet: true }); } catch { return; }
  Widgets.team = out.accounts;
  $('#teamBody').innerHTML = table(out.accounts.map(u => ({
    name: u.name || u.email, email: u.email, role: u.role, sites: u.site_ids.join(', '), studies: u.study_ids.join(', ') || 'any',
    status: u.enabled ? 'enabled' : 'disabled', _u: u.username })), ['name', 'email', 'role', 'sites', 'studies', 'status'], r =>
    `<div class="row"><button class="small" data-team="edit" data-u="${esc(r._u)}">Edit</button>`
    + (r.status === 'enabled' ? `<button class="small danger" data-team="disable" data-u="${esc(r._u)}">Disable</button>`
      : `<button class="small" data-team="enable" data-u="${esc(r._u)}">Enable</button>`)
    + `<button class="small" data-team="reset-password" data-u="${esc(r._u)}">Reset password</button></div>`);
}

function openTeamForm(user) {
  Widgets.editing = user || null;
  const f = $('#teamForm');
  $('#teamFormWrap').classList.remove('hidden');
  $('#teamSave').textContent = user ? 'Save access' : 'Send invite';
  f.email.disabled = !!user;
  if (user) { f.email.value = user.email; f.name.value = user.name || ''; f.role.value = user.role; f.site_ids.value = user.site_ids.join(','); f.study_ids.value = user.study_ids.join(','); }
  else f.reset();
}

async function teamAction(action, username) {
  const u = Widgets.team.find(x => x.username === username);
  if (action === 'edit') return openTeamForm(u);
  let body;
  if (action === 'disable') {
    const reason = prompt(`Disable ${u.email}? They are signed out everywhere.\n\nReason (optional):`);
    if (reason === null) return;
    body = { reason: reason || undefined };
  } else if (action === 'reset-password' && !confirm(`Reset the password for ${u.email}? Cognito emails them a code.`)) return;
  await api('POST', `/staff/accounts/${encodeURIComponent(username)}/${action}`, body);
  toast('Done'); await reloadIdentities(); loadTeam();
}

function wireWidgets() {
  $('#alertTabs').onclick = e => { const b = e.target.closest('[data-scope]'); if (b) { Widgets.alertScope = b.dataset.scope; loadAlerts(); } };
  $('#usageDays').onchange = loadUsage;
  $('#otelSelf').onchange = loadOtel;
  $('#otelRefresh').onclick = loadOtel;
  $('#inviteBtn').onclick = () => openTeamForm(null);
  $('#teamCancel').onclick = () => $('#teamFormWrap').classList.add('hidden');
  document.addEventListener('click', e => {
    const al = e.target.closest('[data-alert]'); if (al) alertAction(al.dataset.alert, al.dataset.id).catch(() => {});
    const tm = e.target.closest('[data-team]'); if (tm) teamAction(tm.dataset.team, tm.dataset.u).catch(() => {});
  });
  $('#teamForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target, split = v => v.split(',').map(s => s.trim()).filter(Boolean);
    const body = { name: f.name.value, role: f.role.value, site_ids: split(f.site_ids.value), study_ids: split(f.study_ids.value) };
    const out = Widgets.editing
      ? await api('PATCH', `/staff/accounts/${encodeURIComponent(Widgets.editing.username)}`, body)
      : await api('POST', '/staff/accounts', { email: f.email.value, ...body });
    toast(Widgets.editing ? 'Access updated' : `Invited ${out.user.email}. They can now sign in.`);
    $('#teamFormWrap').classList.add('hidden');
    await reloadIdentities(); loadTeam();
  };
}
