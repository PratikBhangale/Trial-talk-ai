// Staff portal widgets: emergency alerts, observability, token usage and the team (staff accounts) page.
// They poll quietly (no request-log noise) while a staff user is signed in, and each shows when it last refreshed.

const Widgets = { timers: [], alertScope: 'active', seen: null, baseTitle: document.title, open: 0, team: [], editing: null };

function startWidgets() {
  stopWidgets();
  Widgets.seen = null;
  loadAlerts(); loadTeam(); loadUsage();
  // Observability polls only while its page is open and the tab is visible: it is the heaviest query set.
  Widgets.timers = [setInterval(loadAlerts, 4000), setInterval(() => obsActive() && loadObs(), 5000), setInterval(loadUsage, 15000)];
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

// ------------------------------------------------------------ observability
// One page over trial.telemetry_spans: KPIs, three charts, recent traces (each opens as a waterfall) and behavior drift.
// Every number is an aggregate or a timing; nothing here names a participant. Costs are estimates at list prices.
const usd = v => { v = +v || 0; return '$' + (v > 0 && v < 0.01 ? v.toFixed(4) : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })); };
const dur = v => v === null || v === undefined ? '—' : v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms`;
const compactN = n => Number(n || 0) >= 10000 ? new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n) : nf(n);
const ROOT_LABEL = { 'whatsapp.ingest': 'WhatsApp message', 'whatsapp.route': 'WhatsApp routing', 'whatsapp.legacy': 'WhatsApp (legacy)',
  'agent.invoke': 'Agent reply', 'agent.call': 'Agent call', 'reminder.send': 'Reminder send', 'reconcile.run': 'Schedule reconcile' };
const SPAN_STATUS = { OK: ['tone-ok', 'OK'], ERROR: ['tone-bad', 'Error'], UNSET: ['tone-neutral', 'Unset'] };
const spanStatus = s => { const [tone, label] = SPAN_STATUS[s] || ['tone-neutral', humanize(s || 'unknown')];
  return `<span class="pill ${tone}"><span class="dot"></span>${esc(label)}</span>`; };
const hourFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric' });
const stampFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
// Token days are UTC days (that is how the API buckets them), so the change flags on that chart are stamped in UTC too.
const dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
const utcStampFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC' });

// Polls only while the page is open and the tab is visible: these are the heaviest queries the portal makes.
const obsActive = () => !!TT.user && state.page === 'observability' && !document.hidden;

async function loadObs(loud) {
  if (!TT.user || Widgets.obsBusy) return;
  Widgets.obsBusy = true;
  if (!Widgets.obsLoaded) ['#obsKpis', '#obsRequests', '#obsLatency', '#obsTokens', '#obsTraces', '#obsDrift']
    .forEach(s => { if (!$(s).childElementCount) $(s).innerHTML = skeleton(3); });
  try {
    const [o, { traces }] = await Promise.all([
      api('GET', '/staff/observability?hours=24', undefined, { quiet: !loud }),
      api('GET', '/staff/observability/traces?limit=50', undefined, { quiet: !loud })]);
    Widgets.obsLoaded = true;
    setLive('obsLive', true);
    renderObs(o, traces);
  } catch { setLive('obsLive', false); }
  finally { Widgets.obsBusy = false; }
}

function renderObs(o, traces) {
  const s = o.summary, rate = s.error_rate * 100;
  renderStable($('#obsKpis'), `<div class="kpis">
    <div class="kpi"><span>Requests, 24 h</span><b>${nf(s.requests)}</b><small>${nf(s.traces)} traces · ${nf(s.errors)} failed</small></div>
    <div class="kpi"><span>p95 latency</span><b>${esc(dur(s.p95_ms))}</b><small>Median ${esc(dur(s.p50_ms))} · entry spans</small></div>
    <div class="kpi ${rate >= 5 ? 'tone-bad' : rate >= 1 ? 'tone-warn' : ''}"><span>Error rate</span><b>${s.requests ? rate.toFixed(1) + '%' : '—'}</b><small>${nf(s.errors)} of ${nf(s.requests)} requests</small></div>
    <div class="kpi"><span>Tokens, 24 h</span><b>${esc(compactN(s.tokens_in + s.tokens_out))}</b><small>${nf(s.tokens_in)} in · ${nf(s.tokens_out)} out</small></div>
    <div class="kpi"><span>Est. cost, 24 h</span><b>${esc(usd(s.est_cost_usd))}</b><small>Estimate at list prices</small></div>
  </div>`);

  // Requests: 15-minute buckets, stacked as succeeded + errors, so the bar height is all traffic.
  const rq = o.series.requests, rqX = rq.map(b => Date.parse(b.t));
  const rqTotal = rq.reduce((n, b) => n + b.count, 0), rqErr = rq.reduce((n, b) => n + b.errors, 0);
  obsChart($('#obsRequests'), 'bars', {
    x: rqX, step: 900000, stacked: true, integer: true, yLabel: 'Requests per 15 min',
    label: `Requests per 15 minutes over the last 24 hours: ${rqTotal} requests, ${rqErr} errors.`,
    series: [{ name: 'Succeeded', color: '--ch-1', values: rq.map(b => b.count - b.errors) },
      { name: 'Errors', color: '--bad-solid', values: rq.map(b => b.errors) }],
    xFormat: t => hourFmt.format(t), tipFormat: t => `${stampFmt.format(t)} – ${timeFmt.format(t + 900000)}`,
  }, rqTotal ? '' : emptyState('activity', 'No requests in the last 24 hours', 'Each WhatsApp message, chat reply and scheduled job shows up here as it is processed.'));

  const lt = o.series.latency, ltX = lt.map(b => Date.parse(b.t));
  obsChart($('#obsLatency'), 'line', {
    x: ltX, step: 3600000, yLabel: 'Duration', yFormat: dur,
    label: `Hourly p95 and median duration of each trace's entry span over the last 24 hours. Overall p95 ${dur(s.p95_ms)}.`,
    series: [{ name: 'p95', color: '--ch-1', values: lt.map(b => b.p95) }, { name: 'Median (p50)', color: '--ch-2', values: lt.map(b => b.p50) }],
    xFormat: t => hourFmt.format(t), tipFormat: t => `${stampFmt.format(t)} – ${timeFmt.format(t + 3600000)}`,
  }, lt.some(b => b.p95 !== null) ? '' : emptyState('clock', 'No timings yet', 'Response times appear once requests are traced.'));

  // Tokens: 14 UTC days, stacked input + output, with a flag at each agent configuration change.
  const tk = o.series.tokens, tkX = tk.map(d => Date.parse(d.day + 'T00:00:00Z'));
  const cost = Object.fromEntries(tk.map((d, i) => [tkX[i], d.est_cost_usd]));
  const tkCost = tk.reduce((n, d) => n + d.est_cost_usd, 0), tkAny = tk.some(d => d.input_tokens + d.output_tokens > 0);
  $('#obsTokensTag').textContent = tkAny ? `14 days · est. ${usd(tkCost)}` : '14 days';
  obsChart($('#obsTokens'), 'bars', {
    x: tkX, step: 86400000, stacked: true, yLabel: 'Tokens per day', xTitle: 'Day',
    label: `Input and output tokens per day over 14 days, estimated cost ${usd(tkCost)}. ${o.drift.config_changes.length} configuration changes.`,
    series: [{ name: 'Input', color: '--ch-1', values: tk.map(d => d.input_tokens) }, { name: 'Output', color: '--ch-2', values: tk.map(d => d.output_tokens) }],
    xFormat: t => dayFmt.format(t), tipFormat: t => `${dayFmt.format(t)} · est. ${usd(cost[t])}`,
    markers: o.drift.config_changes.map(c => ({ at: Date.parse(c.at), label: `Agent config changed ${utcStampFmt.format(Date.parse(c.at))} UTC`,
      detail: `config ${c.config_hash}${c.model ? ` · model ${c.model}` : ''}` })),
  }, tkAny ? '' : emptyState('usage', 'No agent replies in the last 14 days', 'Token counts appear once participants chat with the agent.'));

  renderTraces(traces);
  renderDrift(o.drift.metrics);
}

// Charts redraw only when their data changed, so a 5 s poll never resets a tooltip someone is reading.
function obsChart(el, kind, opts, empty) {
  const key = JSON.stringify([kind, opts, empty]);
  if (el._key === key) return;
  el._key = key;
  if (empty) { el._tt = null; el.className = ''; el.innerHTML = empty; return; }
  ttCharts[kind](el, opts);
}

// The newest 10 by default (50 cards is a long scroll on a phone); one button shows the rest.
const TRACES_SHOWN = 10;
function renderTraces(traces) {
  Widgets.obsTraces = traces;
  const all = traces;
  traces = Widgets.obsAllTraces ? all : all.slice(0, TRACES_SHOWN);
  renderStable($('#obsTraces'), traces.length ? `<div class="table-wrap"><table class="data-table rows-link obs-traces">
    <thead><tr><th scope="col">Started</th><th scope="col">Entry point</th><th scope="col" class="r" title="How long the entry span took (the whole trace, queue and agent included, is in its waterfall)">Entry span</th>
      <th scope="col" class="r">Spans</th><th scope="col" class="r">Tokens</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Open</span></th></tr></thead>
    <tbody>${traces.map(t => { const id = esc(t.trace_id), label = ROOT_LABEL[t.root_name] || t.root_name;
      return `<tr data-trace="${id}" ${t.trace_id === Widgets.obsTrace ? 'aria-current="true"' : ''}>
      <td data-label="Started"><button type="button" class="row-link" aria-label="Open trace: ${esc(label)}, ${esc(ago(t.started_at))}">
        <span class="who"><b>${esc(ago(t.started_at))}</b><span>${esc(stampFmt.format(Date.parse(t.started_at)))}</span></span></button></td>
      <td data-label="Entry point"><span class="obs-entry"><b>${esc(label)}</b><span class="tag">${esc(t.service)}</span></span></td>
      <td data-label="Entry span" class="r num">${esc(dur(t.duration_ms))}</td>
      <td data-label="Spans" class="r num">${nf(t.span_count)}</td>
      <td data-label="Tokens" class="r num">${t.tokens === null ? '<span class="muted">—</span>' : nf(t.tokens)}</td>
      <td data-label="Status">${spanStatus(t.status)}</td>
      <td class="actions-cell go-cell" aria-hidden="true">${icon('chevron', 'sm')}</td></tr>`; }).join('')}</tbody></table></div>`
    + (all.length > TRACES_SHOWN ? `<div class="obs-more"><button type="button" class="small" data-obs-more>${Widgets.obsAllTraces ? `Show the newest ${TRACES_SHOWN}` : `Show all ${all.length}`}</button></div>` : '')
    : emptyState('activity', 'No traces yet', 'Send a WhatsApp message or chat on the participant site, and its trace appears here within a few seconds.'));
}

async function openTrace(id) {
  const sheet = $('#traceSheet');
  Widgets.obsTrace = id;
  $$('#obsTraces tr[data-trace]').forEach(r => r.dataset.trace === id ? r.setAttribute('aria-current', 'true') : r.removeAttribute('aria-current'));
  if (!id) { if (sheet.open) sheet.close(); return; }
  $('#obsTraceTitle').textContent = 'Trace';
  $('#obsTraceMeta').innerHTML = '';
  $('#obsWaterfall').innerHTML = skeleton(5);
  if (!sheet.open) sheet.showModal();
  let spans;
  try { ({ spans } = await api('GET', `/staff/observability/traces/${encodeURIComponent(id)}`)); } catch { spans = null; }
  if (Widgets.obsTrace !== id) return;
  if (!spans) { $('#obsWaterfall').innerHTML = emptyState('alert', 'Unable to load this trace', 'Close this card and try again.'); return; }
  if (!spans.length) { $('#obsWaterfall').innerHTML = emptyState('search', 'Trace not found', 'It may be older than the retention window. Choose another trace.'); return; }
  const at = s => Date.parse(s.start_ts), root = spans.find(s => !s.parent_id) || spans[0];
  const t0 = Math.min(...spans.map(at)), t1 = Math.max(...spans.map(s => at(s) + (+s.duration_ms || 0)));
  const ingest = spans.find(s => s.name === 'whatsapp.ingest');
  const route = ingest && spans.find(s => s.name === 'whatsapp.route' && s.parent_id === ingest.span_id);
  const queue = route ? at(route) - at(ingest) - (+ingest.duration_ms || 0) : null;
  const tok = spans.filter(s => s.name === 'agent.invoke')
    .reduce((n, s) => n + (+s.attrs?.['gen_ai.usage.input_tokens'] || 0) + (+s.attrs?.['gen_ai.usage.output_tokens'] || 0), 0);
  const errs = spans.filter(s => s.status === 'ERROR').length;
  $('#obsTraceTitle').textContent = ROOT_LABEL[root.name] || root.name;
  $('#obsTraceMeta').innerHTML = `<div class="kpis obs-trace-kpis">
      <div class="kpi"><span>Total time</span><b>${esc(dur(t1 - t0))}</b><small>${esc(stampFmt.format(t0))}</small></div>
      <div class="kpi"><span>Queue wait</span><b>${queue !== null && queue > 0 ? esc(dur(queue)) : '—'}</b><small>${queue !== null ? 'Ingest to router, on SQS' : 'Not queued'}</small></div>
      <div class="kpi"><span>Tokens</span><b>${tok ? nf(tok) : '—'}</b><small>${nf(spans.length)} spans</small></div>
      <div class="kpi ${errs ? 'tone-bad' : ''}"><span>Status</span><b>${errs ? `${errs} error${errs === 1 ? '' : 's'}` : 'OK'}</b><small>${esc(root.service)}</small></div>
    </div>
    <p class="panel-note obs-trace-id">Trace <span class="num">${esc(id)}</span>${spans.some(s => s.attrs?.['trialtalk.synthetic']) ? ' <span class="tag">Synthetic demo data</span>' : ''}</p>`;
  ttCharts.waterfall($('#obsWaterfall'), { spans });
}

// Drift: one card per measure, today against the week before. The badge's colour says whether the move is in the bad
// direction for that measure (red), an improvement (green), or a measure with no good direction (neutral).
const DRIFT_FMT = { tokens_in_per_reply: v => nf(Math.round(v)), tokens_out_per_reply: v => nf(Math.round(v)), agent_p95_ms: dur,
  reply_length_chars: v => nf(Math.round(v)), kb_search_rate: v => `${Math.round(v * 100)}%`, escalation_rate: v => `${(v * 100).toFixed(1)}%` };
const DRIFT_UNIT = { tokens_in_per_reply: 'tokens', tokens_out_per_reply: 'tokens', reply_length_chars: 'characters' };
function renderDrift(metrics) {
  const any = metrics.some(m => m.current !== null || m.baseline !== null);
  renderStable($('#obsDrift'), any ? `<div class="drift-grid">${metrics.map(m => {
    const f = DRIFT_FMT[m.key] || nf, show = v => v === null ? '—' : f(v), c = m.change_pct;
    let tone = 'tone-neutral', verdict = 'no better or worse direction';
    if (c === null) verdict = 'no comparison';
    else if (c === 0) verdict = 'unchanged';
    else if (m.bad_direction !== 'none') {
      const worse = (m.bad_direction === 'up') === (c > 0);
      tone = worse ? 'tone-bad' : 'tone-ok'; verdict = worse ? 'worse' : 'better';
    }
    const badge = c === null ? `<span class="pill tone-neutral">${m.current === null ? 'No data today' : 'No baseline'}</span>`
      : `<span class="pill ${tone}" title="${esc(humanize(verdict))}, compared with the 7 days before"><span aria-hidden="true">${c > 0 ? '▲' : c < 0 ? '▼' : '–'}</span>${esc(Math.abs(c).toFixed(1))}%<span class="sr-only">${c > 0 ? ' up' : c < 0 ? ' down' : ''}, ${esc(verdict)}</span></span>`;
    return `<article class="drift-card">
      <div class="drift-top"><h3>${esc(m.label.replace(/ \((ms|chars)\)$/, ''))}</h3>${badge}</div>
      <b class="drift-now">${esc(show(m.current))}${m.current !== null && DRIFT_UNIT[m.key] ? `<small>${esc(DRIFT_UNIT[m.key])}</small>` : ''}</b>
      <span class="drift-base">Baseline ${esc(show(m.baseline))}</span>
    </article>`; }).join('')}</div>`
    : emptyState('activity', 'Not enough history yet', 'Drift compares the last 24 hours with the 7 days before. It fills in once the agent has replied in both windows.'));
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
      <div class="kpi"><span>Est. cost</span><b>${esc(usd(t.est_cost_usd))}</b><small>Estimate at list prices</small></div>
    </div>
    <h3 class="sub">Tokens per day</h3>
    ${u.by_day.length ? usageChart(u.by_day) : emptyState('usage', 'No agent replies in this period', 'Token counts appear once participants chat with the agent.')}
    ${u.by_participant.length ? `<h3 class="sub">Top participants</h3>`
      + table(u.by_participant.map(p => ({ participant: { __html: `<a href="#/participants" data-pid="${esc(p.participant_id)}">${esc(p.full_name)}</a>` },
        id: p.participant_id, site: humanize(String(p.site_id).toLowerCase()), input: nf(p.input_tokens), output: nf(p.output_tokens), replies: nf(p.calls),
        est_cost: { __html: `<span class="num">${esc(usd(p.est_cost_usd))}</span>` } })),
        ['participant', 'id', 'site', 'input', 'output', 'replies', 'est_cost']) : ''}
    <p class="note">Costs are estimates from token counts at list prices ($3 per million input tokens, $15 per million output), not a bill. Locally the numbers come from the fake agent; the deployed agent reports real counts.</p>`);
}

// One row per day: the bar splits input (prompt) from output (reply) tokens and is scaled to the busiest day,
// so its length compares days; the exact numbers sit beside it in words. A real table, so it reads without the bars.
function usageChart(days) {
  const total = d => +d.input_tokens + +d.output_tokens, max = Math.max(1, ...days.map(total));
  const busiest = days.reduce((a, d) => total(d) > total(a) ? d : a, days[0]);
  return `<div class="table-wrap plain"><table class="bar-table">
      <thead><tr><th scope="col">Day</th>
        <th scope="col"><span class="chart-legend"><span><i class="lg-in"></i>Input (sent to the model)</span><span><i class="lg-out"></i>Output (the model's replies)</span></span></th>
        <th scope="col" class="r">Total tokens</th><th scope="col" class="r">Est. cost</th></tr></thead>
      <tbody>${days.map(d => `<tr>
        <th scope="row">${esc(when(String(d.day).slice(0, 10)))}</th>
        <td><span class="sr-only">${nf(d.input_tokens)} input, ${nf(d.output_tokens)} output</span>
          <span class="hbar" aria-hidden="true" title="${nf(d.input_tokens)} input · ${nf(d.output_tokens)} output" style="width:${Math.max(2, total(d) / max * 100)}%">
            <i class="in" style="flex-grow:${+d.input_tokens}"></i><i class="out" style="flex-grow:${+d.output_tokens}"></i></span></td>
        <td class="r num">${nf(total(d))}</td><td class="r num">${esc(usd(d.est_cost_usd))}</td></tr>`).join('')}</tbody></table></div>
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
  $('#obsRefresh').onclick = () => loadObs(true);
  $('#obsTraceClose').onclick = () => openTrace(null);
  $('#obsTraces').addEventListener('click', e => {
    if (e.target.closest('[data-obs-more]')) { Widgets.obsAllTraces = !Widgets.obsAllTraces; renderTraces(Widgets.obsTraces || []); return; }
    const row = e.target.closest('tr[data-trace]');
    if (row) openTrace(row.dataset.trace);
  });
  document.addEventListener('visibilitychange', () => obsActive() && loadObs());
  // Closing the trace card (button, Esc or leaving the page) puts focus back on the row it was opened from.
  $('#traceSheet').addEventListener('close', () => {
    const was = Widgets.obsTrace; Widgets.obsTrace = null;
    $$('#obsTraces tr[aria-current]').forEach(r => r.removeAttribute('aria-current'));
    if (was && state.page === 'observability') $(`#obsTraces tr[data-trace="${CSS.escape(was)}"] .row-link`)?.focus();
  });
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
