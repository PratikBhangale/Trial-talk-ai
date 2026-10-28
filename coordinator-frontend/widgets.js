// Staff portal widgets: emergency alerts, OpenTelemetry, token usage and the team (staff accounts) page.
// They poll quietly (no request-log noise) while a staff user is signed in, and each shows when it last refreshed.

const Widgets = { timers: [], alertScope: 'active', seen: null, baseTitle: document.title, open: 0, team: [], editing: null };
const SELF_ROUTES = new Set(['GET /staff/alerts', 'GET /staff/observability']); // the widgets' own polling

function startWidgets() {
  stopWidgets();
  Widgets.seen = null;
  loadAlerts(); loadOtel(); loadTeam(); loadUsage();
  Widgets.timers = [setInterval(loadAlerts, 4000), setInterval(loadOtel, 5000), setInterval(loadUsage, 15000)];
}
function stopWidgets() {
  Widgets.timers.forEach(clearInterval); Widgets.timers = [];
  Widgets.open = 0; setTitle(); syncBanner();
  $$('.live').forEach(el => setLive(el.id, null));
}

function setTitle() {
  document.title = (Widgets.open ? `(${Widgets.open}) Emergency · ` : '') + Widgets.baseTitle;
}
// The red banner follows staff to every page except the alerts page itself.
function syncBanner() {
  const n = Widgets.open, show = n > 0 && state.page !== 'alerts';
  $('#alertBanner').classList.toggle('hidden', !show);
  $('#alertBannerText').textContent = `${n} open emergency alert${n === 1 ? '' : 's'} waiting for a response`;
}
// ok: true = refreshed now, false = last refresh failed, null = not polling
function setLive(id, ok) {
  const el = $('#' + id); if (!el) return;
  el.className = 'live' + (ok === null ? ' off' : ok ? '' : ' stale');
  const t = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  el.innerHTML = ok === null ? '<i></i>Paused' : ok ? `<i></i>Updated ${t}` : `<i></i>Reconnecting, last try ${t}`;
  el.title = ok ? 'Refreshes automatically every few seconds' : '';
  // The dot moves only when fresh data actually arrived: one short flash, never a constant pulse.
  if (ok && !matchMedia('(prefers-reduced-motion: reduce)').matches)
    $('i', el).animate([{ opacity: 0.25 }, { opacity: 1 }], { duration: 600, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' });
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)(), o = ctx.createOscillator();
    o.frequency.value = 880; o.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.25);
  } catch { /* browsers block audio until the page has been interacted with */ }
}

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - parseTs(iso)) / 1000));
  return s < 60 ? 'Just now' : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : when(iso);
}

// ------------------------------------------------------------ emergency alerts
const SEV_LABEL = { life_threatening: 'Life-threatening', urgent: 'Urgent', concern: 'Concern' };
const SRC_LABEL = { sos_button: 'Reported an adverse event', chat_keyword: 'Detected in chat' };

// Audit fields store the actor as "staff:<username>"; people read names.
function staffName(actor) {
  const u = String(actor || '').replace(/^staff:/, '');
  const m = Widgets.team.find(x => x.username === u || x.email === u);
  return m?.name || (u === TT.user && TT.identity?.name) || u;
}

// The API returns keywords as a JSON string ('["chest pain"]') from the jsonb column; accept an array too.
function keywordsOf(a) {
  let k = a.keywords;
  if (typeof k === 'string') { try { k = JSON.parse(k); } catch { k = [k]; } }
  return Array.isArray(k) ? k.filter(Boolean).map(String) : [];
}
// One malformed alert must never blank the list: render the rest, and show this one as a plain fallback.
function safeAlertCard(a) {
  try { return alertCard(a); } catch (e) {
    console.error('alert render failed', a?.alert_id, e);
    return `<article class="alert-card sev-${esc(a?.severity)}"><div class="top">${sevPill(a?.severity, a?.status === 'open')}
      <a href="#/participants" class="who" data-pid="${esc(a?.participant_id)}">${esc(a?.full_name || a?.participant_id)}</a></div></article>`;
  }
}

function sevPill(sev, open) {
  const label = esc(SEV_LABEL[sev] || humanize(sev));
  if (sev === 'life_threatening') return `<span class="pill solid">${icon('alert')}${label}</span>`;
  if (sev === 'urgent') return `<span class="pill solid tone-warn">${icon('alert')}${label}</span>`;
  return `<span class="pill tone-info"><span class="dot"></span>${label}</span>`;
}

async function loadAlerts() {
  if (!TT.user) return;
  let active, shown;
  try {
    active = await api('GET', '/staff/alerts?status=active', undefined, { quiet: true });
    shown = Widgets.alertScope === 'active' ? active : await api('GET', '/staff/alerts?status=' + Widgets.alertScope, undefined, { quiet: true });
  } catch { setLive('alertsLive', false); setLive('ovLive', false); return; }
  setLive('alertsLive', true); setLive('ovLive', true);

  const { alerts, open } = active;
  const first = Widgets.seen === null;
  const fresh = alerts.filter(a => a.status === 'open' && Widgets.seen && !Widgets.seen.has(a.alert_id));
  Widgets.seen = new Set([...(Widgets.seen || []), ...alerts.map(a => a.alert_id)]);
  Widgets.arriving = new Set(fresh.map(a => a.alert_id));  // only these cards animate in, once
  if (!first && fresh.length) {
    toast(`New emergency alert: ${fresh.map(a => `${a.full_name} (${a.participant_id}), ${SEV_LABEL[a.severity]}`).join('; ')}`, true);
    beep();  // the original portal's alert beep: a safety signal for a new emergency, not an interface sound
  }
  Widgets.open = open; setTitle(); syncBanner();
  setCount('#navAlerts', open, 'bad');
  $('#kpiAlerts').textContent = open;
  $('#kpiAlertsTile').classList.toggle('tone-bad', open > 0);
  $('#kpiAlertsSub').textContent = open ? 'Waiting for a person' : alerts.length ? `${alerts.length} being handled` : 'All clear';
  $('#alertBadge').innerHTML = open ? `<span class="pill solid">${icon('alert')}${open} open</span>` : chip('resolved', 'None open');
  $$('#alertTabs button').forEach(b => { const on = b.dataset.scope === Widgets.alertScope; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });

  renderStable($('#alertList'), shown.alerts.length ? shown.alerts.map(safeAlertCard).join('')
    : Widgets.alertScope === 'resolved' ? emptyState('check', 'No resolved alerts', 'Alerts resolved in the last 30 days appear here with their resolution notes.')
      : emptyState('check', 'No active alerts', 'When a participant reports an adverse event or uses emergency words in chat, the alert appears here right away.'));
  renderStable($('#ovAlerts'), alerts.length ? `<ul class="list">${alerts.slice(0, 4).map(alertRow).join('')}</ul>`
    : emptyState('check', 'No active alerts', 'All clear at your sites.'));
  Widgets.arriving = new Set(); Widgets.justAcked = null;
}

// One alert, read top to bottom: who and when, what state it is in, what they said, why it was flagged, then who has it
// and what to do. Only an alert nobody has picked up carries the full tint; once acknowledged it keeps a severity edge.
function alertCard(a) {
  const done = a.status === 'resolved', id = esc(a.alert_id), pid = esc(a.participant_id);
  const arrive = Widgets.arriving?.has(a.alert_id) ? 'arrive' : '';
  const kw = keywordsOf(a);
  const trail = done ? `Resolved by <b>${esc(staffName(a.resolved_by))}</b> · ${esc(ago(a.resolved_at))}`
    : a.acknowledged_by ? `Acknowledged by <b>${esc(staffName(a.acknowledged_by))}</b> · ${esc(ago(a.acknowledged_at))}`
    : 'Nobody has responded yet';
  return `<article class="alert-card sev-${esc(a.severity)} st-${esc(a.status)} ${done ? 'done' : ''} ${arrive}" aria-label="${esc(SEV_LABEL[a.severity] || a.severity)} alert for ${esc(a.full_name)}">
    <div class="top">
      <a href="#/participants" class="who" data-pid="${pid}">${esc(a.full_name)}</a><span class="tag">${pid}</span>
      <span class="when"><time datetime="${esc(a.created_at)}" title="${esc(when(a.created_at))}">${esc(ago(a.created_at))}</time></span>
    </div>
    <div class="meta">${sevPill(a.severity, a.status === 'open')}${chip(a.status)}
      <span>${icon(a.source === 'chat_keyword' ? 'message' : 'siren', 'sm')}${esc(SRC_LABEL[a.source] || humanize(a.source))}</span>
      <span>${esc(humanize(String(a.site_id).toLowerCase()))}</span>
      ${a.press_count > 1 ? `<span>Pressed ${a.press_count} times</span>` : ''}</div>
    ${a.message ? `<blockquote>${esc(a.message)}</blockquote>` : ''}
    ${a.source === 'chat_keyword' && !done ? `<p class="detected">Flagged for ${kw.length ? kw.map(k => `“${esc(k)}”`).join(', ') : 'emergency words'}. Confirm with the participant before acting.</p>` : ''}
    ${done && a.resolution_note ? `<p class="note">${esc(a.resolution_note)}</p>` : ''}
    <div class="foot">
      <span class="trail${Widgets.justAcked === a.alert_id ? ' enter' : ''}">${trail}</span>
      ${done ? '' : `<div class="row">
        <a href="tel:${esc(a.phone_e164)}" class="btn small" aria-label="Call ${esc(a.full_name)} on ${esc(a.phone_e164)}">${icon('phone', 'sm')}<span class="num">${esc(a.phone_e164)}</span></a>
        <button type="button" class="small" data-alert="resolve" data-id="${id}" data-name="${esc(a.full_name)}">Resolve</button>
        ${a.status === 'open' ? `<button type="button" class="small primary" data-alert="acknowledge" data-id="${id}">${icon('check', 'sm')}Acknowledge</button>` : ''}
      </div>`}
    </div>
  </article>`;
}

// Overview: one line per active alert. The full card, with its actions, lives on the Emergency alerts page.
function alertRow(a) {
  return `<li class="alert-row st-${esc(a.status)}">
    <div class="list-main"><b><a href="#/alerts">${esc(a.full_name)}</a></b>
      <span><span class="sev-text sev-${esc(a.severity)}">${esc(SEV_LABEL[a.severity] || humanize(a.severity))}</span> · ${esc(SRC_LABEL[a.source] || humanize(a.source))}${a.message ? ` · “${esc(a.message)}”` : ''}</span></div>
    <div class="list-side">${chip(a.status)}<time datetime="${esc(a.created_at)}">${esc(ago(a.created_at))}</time></div></li>`;
}

async function alertAction(action, id, name) {
  if (action === 'resolve') {
    const note = await ask({ title: `Resolve the alert for ${name}?`, message: 'Resolved alerts stay in the record with your note.',
      input: { label: 'What happened and what was done', required: true, multiline: true,
        placeholder: 'e.g. Called the participant. Pain eased, advised urgent care if it returns.', error: 'Describe what happened. It is kept in the record.' },
      confirm: 'Resolve alert' });
    if (!note) return;
    await api('POST', `/staff/alerts/${id}/resolve`, { note });
    toast('Alert resolved');
    // It leaves the active list: bridge the gap before the list re-renders without it.
    const leaving = Widgets.alertScope === 'resolved' ? [] : $$(`[data-alert][data-id="${CSS.escape(id)}"]`).map(b => collapseOut(b.closest('.alert-card')));
    await Promise.all(leaving.map(a => a?.finished.catch(() => {})));
    await loadAlerts().catch(() => {});
    leaving.forEach(a => a?.cancel());
    return;
  }
  await api('POST', `/staff/alerts/${id}/acknowledge`); toast('Alert acknowledged. The participant sees that you are responding.');
  Widgets.justAcked = id;  // its "Acknowledged by" line eases in, once
  loadAlerts();
}

// ------------------------------------------------------------ OpenTelemetry
async function loadOtel() {
  if (!TT.user) return;
  let o;
  try { o = await api('GET', '/staff/observability', undefined, { quiet: true }); } catch { setLive('otelLive', false); return; }
  setLive('otelLive', true);
  const body = $('#otelBody');
  if (!o.enabled) {
    body.innerHTML = `<div class="callout tone-info">${icon('activity')}<div class="body"><b>OpenTelemetry is not active</b>
      <span>${esc(o.reason)}. Install it with <code>pip install opentelemetry-sdk</code> and restart the dev server.</span></div></div>`;
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
  const errRate = total ? err / total * 100 : 0;
  const counters = o.metrics.filter(m => m.kind === 'counter').flatMap(m => m.points.map(p => ({
    metric: { __html: `<span class="num">${esc(m.name)}</span>` },
    attributes: { __html: `<span class="tag-list">${Object.entries(p.attributes).map(([k, v]) => `<span class="tag">${esc(k)}=${esc(v)}</span>`).join('')}</span>` },
    value: nf(p.value) })));


  renderStable(body, `
    <div class="kpis">
      <div class="kpi"><span>Requests</span><b>${nf(total)}</b></div>
      <div class="kpi ${err ? 'tone-bad' : 'tone-ok'}"><span>Error rate (5xx)</span><b>${errRate.toFixed(1)}%</b></div>
      <div class="kpi"><span>Average latency</span><b>${total ? Math.round(sum / total * 1000) : 0} ms</b></div>
      <div class="kpi"><span>Slowest request</span><b>${Math.round(max * 1000)} ms</b></div>
    </div>
    <p class="note" style="margin:0 0 16px">Service <span class="num">${esc(o.service)}</span> · ${o.otlp_export ? 'Also exporting over OTLP' : 'In-process window only. Set OTEL_EXPORTER_OTLP_ENDPOINT to export.'}</p>
    <h3 class="sub">By route</h3>
    ${table(list.map(r => ({ route: { __html: `<span class="num">${esc(r.route)}</span>` }, requests: nf(r.requests),
      client_errors: r.client4xx ? { __html: `<span class="pill tone-warn"><span class="dot"></span>${nf(r.client4xx)}</span>` } : '0',
      server_errors: r.errors5xx ? { __html: `<span class="pill tone-bad"><span class="dot"></span>${nf(r.errors5xx)}</span>` } : '0',
      average: `${Math.round(r.sum / r.requests * 1000)} ms`, slowest: `${Math.round(r.max * 1000)} ms` })), null, null,
      emptyState('activity', 'No requests yet', 'Use the portals and the request metrics appear here.'))}
    ${counters.length ? `<h3 class="sub">Counters</h3>${table(counters)}` : ''}`);
}

// ------------------------------------------------------------ token usage
const nf = n => Number(n || 0).toLocaleString();

async function loadUsage() {
  if (!TT.user) return;
  let u;
  try { u = await api('GET', '/staff/usage?days=' + $('#usageDays').value, undefined, { quiet: true }); } catch { setLive('usageLive', false); return; }
  setLive('usageLive', true);
  const t = u.totals, max = Math.max(1, ...u.by_day.map(d => +d.input_tokens + +d.output_tokens));
  renderStable($('#usageBody'), `
    <div class="kpis">
      <div class="kpi"><span>Total tokens</span><b>${nf(+t.input_tokens + +t.output_tokens)}</b></div>
      <div class="kpi"><span>Input</span><b>${nf(t.input_tokens)}</b></div>
      <div class="kpi"><span>Output</span><b>${nf(t.output_tokens)}</b></div>
      <div class="kpi"><span>Agent replies</span><b>${nf(t.calls)}</b></div>
    </div>
    <h3 class="sub">Tokens per day</h3>
    ${u.by_day.length ? usageChart(u.by_day) : emptyState('usage', 'No agent replies in this period', 'Token counts appear once participants chat with the agent.')}
    ${u.by_participant.length ? `<h3 class="sub">Top participants</h3>`
      + table(u.by_participant.map(p => ({ participant: { __html: `<a href="#/participants" data-pid="${esc(p.participant_id)}">${esc(p.full_name)}</a>` },
        id: p.participant_id, site: humanize(String(p.site_id).toLowerCase()), input: nf(p.input_tokens), output: nf(p.output_tokens), replies: nf(p.calls) })),
        ['participant', 'id', 'site', 'input', 'output', 'replies']) : ''}
    <p class="note">These numbers come from the local fake agent. The deployed agent reports real counts.</p>`);
}

// One row per day: the bar splits input (prompt) from output (reply) tokens and is scaled to the busiest day,
// so its length compares days; the exact numbers sit beside it in words. A real table, so it reads without the bars.
function usageChart(days) {
  const total = d => +d.input_tokens + +d.output_tokens, max = Math.max(1, ...days.map(total));
  const busiest = days.reduce((a, d) => total(d) > total(a) ? d : a, days[0]);
  return `<div class="table-wrap plain"><table class="bar-table">
      <thead><tr><th scope="col">Day</th>
        <th scope="col"><span class="chart-legend"><span><i class="lg-in"></i>Input (sent to the model)</span><span><i class="lg-out"></i>Output (the model's replies)</span></span></th>
        <th scope="col" class="r">Total tokens</th></tr></thead>
      <tbody>${days.map(d => `<tr>
        <th scope="row">${esc(when(String(d.day).slice(0, 10)))}</th>
        <td><span class="sr-only">${nf(d.input_tokens)} input, ${nf(d.output_tokens)} output</span>
          <span class="hbar" aria-hidden="true" title="${nf(d.input_tokens)} input · ${nf(d.output_tokens)} output" style="width:${Math.max(2, total(d) / max * 100)}%">
            <i class="in" style="flex-grow:${+d.input_tokens}"></i><i class="out" style="flex-grow:${+d.output_tokens}"></i></span></td>
        <td class="r num">${nf(total(d))}</td></tr>`).join('')}</tbody></table></div>
    <p class="note">Bar length is relative to the busiest day, ${esc(when(String(busiest.day).slice(0, 10)))} (${nf(total(busiest))} tokens).</p>`;
}

// ------------------------------------------------------------ team (staff accounts)
async function loadTeam() {
  if (!TT.user) return;
  let out;
  try { out = await api('GET', '/staff/accounts', undefined, { quiet: true }); } catch { return; }
  Widgets.team = out.accounts;
  $('#teamBody').innerHTML = out.accounts.length ? `<div class="table-wrap"><table class="data-table">
    <thead><tr><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Sites</th>
      <th scope="col">Study</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${out.accounts.map(teamRow).join('')}</tbody></table></div>`
    : emptyState('team', 'No staff accounts', 'Invite the first coordinator or PI.');
}

// One row per account, the same columns every time; secondary actions sit in the row's "⋯" menu.
function teamRow(u) {
  const name = u.name || u.email, menu = 'tm-' + u.username.replace(/[^\w-]/g, '_'), uu = esc(u.username);
  return `<tr>
    <td data-label="Name"><span class="who-cell"><span class="avatar sm" aria-hidden="true">${esc(initials(name))}</span><b>${esc(name)}</b></span></td>
    <td data-label="Email" class="email-cell">${copyable(u.email)}</td>
    <td data-label="Role">${esc(u.role === 'PI' ? 'PI' : humanize(u.role))}</td>
    <td data-label="Sites"><span class="tag-list">${u.site_ids.map(s => `<span class="tag">${esc(humanize(s.toLowerCase()))}</span>`).join('')}</span></td>
    <td data-label="Study">${u.study_ids.length ? esc(u.study_ids.join(', ')) : '<span class="muted">Any</span>'}</td>
    <td data-label="Status">${chip(u.enabled ? 'enabled' : 'disabled')}</td>
    <td class="actions-cell"><div class="row-actions">
      <button type="button" class="small" data-team="edit" data-u="${uu}">Edit access</button>
      <button type="button" class="icon-btn" popovertarget="${menu}" aria-label="More actions for ${esc(name)}" title="More actions">${icon('dots')}</button>
      <div popover id="${menu}" class="menu row-menu" aria-label="Actions for ${esc(name)}">
        ${u.enabled ? `<button type="button" class="menu-item danger" data-team="disable" data-u="${uu}">${icon('x')}<span class="mi-text"><b>Disable account</b></span></button>`
          : `<button type="button" class="menu-item" data-team="enable" data-u="${uu}">${icon('check')}<span class="mi-text"><b>Enable account</b></span></button>`}
        <button type="button" class="menu-item" data-team="reset-password" data-u="${uu}">${icon('envelope')}<span class="mi-text"><b>Send password reset</b></span></button>
      </div></div></td>
  </tr>`;
}

function openTeamForm(user) {
  Widgets.editing = user || null;
  const f = $('#teamForm');
  $('#teamFormWrap').classList.remove('hidden');
  $('#teamFormTitle').textContent = user ? `Edit access for ${user.name || user.email}` : 'Invite a staff member';
  $('#teamSave').textContent = user ? 'Save access' : 'Send invite';
  f.email.disabled = !!user;
  if (user) { f.email.value = user.email; f.name.value = user.name || ''; f.role.value = user.role; f.site_ids.value = user.site_ids.join(','); f.study_ids.value = user.study_ids.join(','); }
  else f.reset();
  (user ? f.name : f.email).focus();
}

async function teamAction(action, username) {
  $$('.menu:popover-open').forEach(m => m.hidePopover());
  const u = Widgets.team.find(x => x.username === username);
  if (action === 'edit') return openTeamForm(u);
  let body;
  if (action === 'disable') {
    const reason = await ask({ title: `Disable ${u.name || u.email}?`, message: 'They are signed out everywhere and cannot sign in until an account is enabled again.',
      input: { label: 'Reason', placeholder: 'e.g. Left the study team' }, confirm: 'Disable account', tone: 'danger-solid' });
    if (reason === null) return;
    body = { reason: reason || undefined };
  } else if (action === 'reset-password') {
    const ok = await ask({ title: `Reset the password for ${u.name || u.email}?`, message: `Cognito emails a reset code to ${u.email}.`, confirm: 'Send reset code' });
    if (!ok) return;
  }
  await api('POST', `/staff/accounts/${pathSeg(username)}/${action}`, body);
  toast({ disable: 'Account disabled', enable: 'Account enabled', 'reset-password': 'Reset code sent' }[action] || 'Done');
  await reloadIdentities(); loadTeam();
}

function wireWidgets() {
  $('#alertTabs').onclick = e => { const b = e.target.closest('[data-scope]'); if (b) { Widgets.alertScope = b.dataset.scope; loadAlerts(); } };
  $('#usageDays').onchange = loadUsage;
  $('#otelSelf').onchange = loadOtel;
  $('#otelRefresh').onclick = loadOtel;
  $('#inviteBtn').onclick = () => openTeamForm(null);
  $('#teamCancel').onclick = () => $('#teamFormWrap').classList.add('hidden');
  document.addEventListener('click', e => {
    const al = e.target.closest('[data-alert]'); if (al) alertAction(al.dataset.alert, al.dataset.id, al.dataset.name).catch(() => {});
    const tm = e.target.closest('[data-team]'); if (tm) teamAction(tm.dataset.team, tm.dataset.u).catch(() => {});
  });
  $('#teamForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target, split = v => v.split(',').map(s => s.trim()).filter(Boolean);
    const body = { name: f.name.value, role: f.role.value, site_ids: split(f.site_ids.value), study_ids: split(f.study_ids.value) };
    const out = Widgets.editing
      ? await api('PATCH', `/staff/accounts/${pathSeg(Widgets.editing.username)}`, body)
      : await api('POST', '/staff/accounts', { email: f.email.value, ...body });
    toast(Widgets.editing ? 'Access updated. It takes effect at their next sign-in.' : `Invited ${out.user.email}. They can now sign in.`);
    $('#teamFormWrap').classList.add('hidden');
    await reloadIdentities(); loadTeam();
  };
}
