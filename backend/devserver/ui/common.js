// Shared helpers for the two portals and the inspector.
// "Sign-in" here is a dev stand-in for Cognito: the chosen user goes in the X-Dev-Identity header and the
// dev server turns it into the claims the JWT authorizer would pass. Real portals swap this for Cognito tokens.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const TT = { portal: null, user: null, onSignIn: null };
const PORTALS = { staff: 'Staff portal', participant: 'Participant site' };

function store(k, v) {
  try { if (v !== undefined) localStorage.setItem('tt.' + k, v); return localStorage.getItem('tt.' + k); } catch { return null; }
}

function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.style.display = 'block';
  t.style.background = bad ? 'var(--danger)' : 'var(--text)';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 4000);
}

// ------------------------------------------------------------ API
// opts.quiet: background polling. No error toast, so widgets can refresh freely.
async function api(method, path, body, opts = {}) {
  if (!TT.user) { if (!opts.quiet) toast('Sign in first', true); throw new Error('signed out'); }
  const identity = `${TT.portal}:${encodeURIComponent(TT.user)}`;
  const headers = { 'X-Dev-Identity': identity };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch('/api' + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (!opts.quiet) toast(`${res.status}: ${data.error || data.message || 'Request failed'}`, true);
    throw Object.assign(new Error(data.error || 'Request failed'), { status: res.status });
  }
  return data;
}

async function dev(method, path) {
  const res = await fetch(path, { method });
  const data = await res.json();
  if (!res.ok) { toast(data.error || 'Failed', true); throw new Error(data.error); }
  return data;
}

// ------------------------------------------------------------ rendering
function fmt(v) {
  if (v === null || v === undefined || v === '') return '<span class="muted">-</span>';
  if (typeof v === 'object') return esc(JSON.stringify(v));
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return esc(v);
}

const STATUS_WORDS = new Set(['active', 'paused', 'draft', 'cancelled', 'completed', 'enrolled', 'screening', 'prospect',
  'withdrawn', 'screen_failed', 'enabled', 'disabled', 'open', 'acknowledged', 'resolved']);

function table(rows, cols, extra) {
  if (!rows || !rows.length) return '<span class="muted">Nothing yet.</span>';
  cols = cols || [...new Set(rows.flatMap(r => Object.keys(r)))];
  const cell = v => STATUS_WORDS.has(v) ? `<td>${chip(v)}</td>`
    : `<td class="${typeof v === 'object' && v ? 'json' : ''}">${fmt(v)}</td>`;
  return `<table><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}${extra ? '<th></th>' : ''}</tr></thead><tbody>`
    + rows.map((r, i) => `<tr>${cols.map(c => cell(r[c])).join('')}${extra ? `<td>${extra(r, i)}</td>` : ''}</tr>`).join('')
    + '</tbody></table>';
}

function chip(status) {
  const cls = { active: 'ok', enrolled: 'ok', enabled: 'ok', paused: 'warn', draft: 'warn', screening: 'warn',
    withdrawn: 'bad', cancelled: 'bad', screen_failed: 'bad', disabled: 'bad', open: 'bad', acknowledged: 'warn', resolved: 'ok' }[status] || '';
  return `<span class="chip ${cls}">${esc(status)}</span>`;
}

function describeCron(cron) {
  const m = /^cron\((\d+) (\d+) \S+ \S+ (\S+) \S+\)$/.exec(cron || '');
  if (!m) return cron || '';
  const h = +m[2], t = `${(h % 12) || 12}:${String(m[1]).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
  return `${m[3] === '*' || m[3] === '?' ? 'Every day' : m[3]} at ${t}`;
}

function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.closest('.hidden')) continue;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.value !== '') out[el.name] = el.value;
  }
  return out;
}

// ------------------------------------------------------------ portal shell
function identityLabel(portal, u) {
  const off = u.enabled === false ? ' (disabled)' : '';
  if (portal === 'participant') return `${u.username} ${u.full_name || ''} (${u.status || 'no record'})${off}`;
  const role = u.groups.join('+');
  return `${u.name || u.username} · ${u.username}${role ? ' · ' + role : ''}${portal === 'staff' ? ' · ' + u.site_ids : ''}${off}`;
}

async function reloadIdentities() {
  const ids = await dev('GET', '/dev/identities');
  const list = ids[TT.portal] || [];
  const sel = $('#signIn');
  const keep = TT.user || store('user.' + TT.portal);
  sel.innerHTML = '<option value="">Signed out</option>'
    + list.map(u => `<option value="${esc(u.username)}">${esc(identityLabel(TT.portal, u))}</option>`).join('');
  sel.value = list.some(u => u.username === keep) ? keep : '';
  TT.user = sel.value || null;
}

function renderShell(portal, subtitle) {
  const others = Object.entries(PORTALS).filter(([k]) => k !== portal)
    .map(([k, v]) => `<a href="/${k}">${esc(v)}</a>`).join(' · ');
  $('header').innerHTML = `
    <div class="brand"><span class="portal-name">${esc(PORTALS[portal])}</span><small>TrialTalk · ${esc(subtitle)}</small></div>
    <span class="spacer"></span>
    <div class="devbar">
      <span class="tag">Dev sign-in</span>
      <select id="signIn" aria-label="Sign in as"></select>
      <a href="/inspector">Inspector</a> · ${others}
      <button class="small" id="resetBtn" title="Delete everything and reload the synthetic seed data">Reset data</button>
    </div>`;
  document.body.insertAdjacentHTML('beforeend', '<div id="toast"></div>');
}

// Call once per page: renders header + log, fills the sign-in picker, and calls onSignIn(user|null) on change.
async function initPortal(portal, subtitle, onSignIn) {
  TT.portal = portal; TT.onSignIn = onSignIn;
  renderShell(portal, subtitle);
  $('#signIn').onchange = () => { TT.user = $('#signIn').value || null; store('user.' + portal, TT.user || ''); signedIn(); };
  $('#resetBtn').onclick = async () => {
    if (!confirm('Delete all local data (every portal) and reload the seed?')) return;
    await dev('POST', '/dev/reset'); await reloadIdentities(); signedIn(); toast('Reset');
  };
  await reloadIdentities();
  signedIn();
}

function signedIn() {
  $$('[data-signed-in]').forEach(el => el.classList.toggle('hidden', !TT.user));
  $$('[data-signed-out]').forEach(el => el.classList.toggle('hidden', !!TT.user));
  Promise.resolve(TT.onSignIn(TT.user)).catch(e => console.error(e));
}
