// Hand-rolled charts for the staff Observability page: a line chart, a column chart and a trace waterfall.
// No libraries. Every colour is a CSS custom property from common.css, so the charts follow light, dark and system
// themes with the rest of the portal. Data reaches the DOM through textContent / createTextNode only, never innerHTML.
//
//   ttCharts.line(el, { x, step, series, yLabel, yFormat, xFormat, tipFormat, markers })
//   ttCharts.bars(el, { x, step, series, stacked, integer, yLabel, yFormat, xFormat, tipFormat, markers })
//   ttCharts.waterfall(el, { spans, traceStart })
//
// x: bucket start times (ms since epoch), one per value; step: bucket width in ms. series: [{ name, color, values }],
// color a custom property name ("--ch-1"). markers: [{ at (ms), label, detail }], drawn as vertical rules.
// Every line/bar chart also renders a "Show as table" view, so no value is reachable only by hovering.
(() => {
  const SVG = 'http://www.w3.org/2000/svg';
  const H = 200, PAD = { top: 22, right: 12, bottom: 26, left: 44 };

  function node(tag, attrs = {}, parent, ns = SVG) {
    const n = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'text') n.textContent = v;
      else if (k === 'style') Object.assign(n.style, v);
      else n.setAttribute(k, v);
    }
    if (parent) parent.append(n);
    return n;
  }
  const html = (tag, attrs, parent) => node(tag, attrs, parent, null);
  const paint = name => `var(${name})`;

  const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
  const plain = new Intl.NumberFormat();
  const defaultY = v => (v === null || v === undefined ? '—' : Math.abs(v) >= 1000 ? compact.format(v) : plain.format(Math.round(v * 10) / 10));

  // 0 / 250 / 500 …: a step of 1, 2, 2.5 or 5 × 10^n that gives about four gridlines.
  // integer: counts never get a 0.5 gridline.
  function niceMax(max, integer) {
    if (!(max > 0)) return { top: 1, step: integer ? 1 : 0.25 };
    const raw = max / 4, mag = 10 ** Math.floor(Math.log10(raw));
    let step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
    if (integer) step = Math.max(1, Math.round(step));
    return { top: Math.ceil(max / step) * step, step };
  }

  // Charts size to their container in real pixels (so text stays 12 px) and redraw when it changes width.
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(entries => {
    for (const e of entries) {
      const el = e.target, w = Math.round(e.contentRect.width);
      if (el._tt && w && Math.abs(w - (el._tt.w || 0)) > 4) el._tt.draw();
    }
  }) : null;
  function mount(el, draw) {
    el._tt = { draw: () => { el._tt.w = Math.round(el.clientWidth); draw(); } };
    if (observer && !el._ttObserved) { observer.observe(el); el._ttObserved = true; }
    el._tt.draw();
  }

  // ------------------------------------------------------------------ shared frame: axes, grid, markers, hover
  function frame(el, o, kind) {
    const W = Math.max(280, el.clientWidth || 600), n = o.x.length;
    const plotW = W - PAD.left - PAD.right, plotH = H - PAD.top - PAD.bottom;
    const yFormat = o.yFormat || defaultY, xFormat = o.xFormat || (t => new Date(t).toLocaleTimeString([], { hour: 'numeric' }));
    const tipFormat = o.tipFormat || xFormat;
    const step = o.step || (n > 1 ? o.x[1] - o.x[0] : 1);
    const totals = o.x.map((_, i) => o.stacked
      ? o.series.reduce((s, ser) => s + (+ser.values[i] || 0), 0)
      : Math.max(0, ...o.series.map(ser => +ser.values[i] || 0)));
    const { top, step: yStep } = niceMax(Math.max(0, ...totals), o.integer);
    const y = v => PAD.top + plotH - (v / top) * plotH;
    // Bars sit in bands; lines put each point at the middle of its bucket. Either way bucket i spans
    // [x0 + i*step, x0 + (i+1)*step), so a marker lands at its true time inside the bucket.
    const band = plotW / Math.max(1, n);
    const xAt = t => PAD.left + ((t - o.x[0]) / step) * band;
    const xMid = i => PAD.left + (i + 0.5) * band;

    el.replaceChildren();
    el.classList.add('chart');
    const wrap = html('div', { class: 'chart-plot' }, el);
    const svg = node('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', tabindex: '0',
      'aria-label': o.label || '', class: 'chart-svg' }, wrap);

    if (o.yLabel) node('text', { x: 0, y: 12, class: 'ax-title', text: o.yLabel }, svg);
    const grid = node('g', { class: 'grid' }, svg);
    for (let v = 0; v <= top + 1e-9; v += yStep) {
      node('line', { x1: PAD.left, x2: W - PAD.right, y1: y(v), y2: y(v), class: v === 0 ? 'base' : '' }, grid);
      node('text', { x: PAD.left - 8, y: y(v) + 4, 'text-anchor': 'end', class: 'ax', text: yFormat(v) }, grid);
    }
    // About one x label per 90 px, on bucket boundaries.
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 90))));
    for (let i = 0; i < n; i += every) {
      const cx = kind === 'bars' && band >= 18 ? xMid(i) : PAD.left + i * band;
      node('text', { x: cx, y: H - 6, 'text-anchor': i === 0 && cx - PAD.left < 20 ? 'start' : 'middle', class: 'ax', text: xFormat(o.x[i]) }, svg);
    }

    const plot = node('g', {}, svg);
    const hover = node('g', { class: 'hover', 'aria-hidden': 'true' }, svg);

    // Config changes: a hairline rule at the exact time, a small flag on top, and the details in its tooltip.
    const markers = (o.markers || []).filter(m => m.at >= o.x[0] && m.at <= o.x[0] + n * step);
    const mg = node('g', { class: 'markers' }, svg);
    for (const m of markers) {
      const mx = xAt(m.at);
      const g = node('g', { class: 'marker' }, mg);
      node('line', { x1: mx, x2: mx, y1: PAD.top - 6, y2: PAD.top + plotH }, g);
      node('path', { d: `M${mx} ${PAD.top - 10} l4 4 l-4 4 l-4 -4 z`, class: 'flag' }, g);
      node('rect', { x: mx - 8, y: PAD.top - 12, width: 16, height: plotH + 12, class: 'hit' }, g);
      node('title', { text: [m.label, m.detail].filter(Boolean).join('\n') }, g);
    }

    // One tooltip for every series at the hovered bucket. Pointer and arrow keys move it the same way.
    const tip = html('div', { class: 'chart-tip', role: 'status', 'aria-live': 'polite' }, wrap);
    tip.hidden = true;
    let at = -1;
    function show(i) {
      at = Math.max(0, Math.min(n - 1, i));
      hover.replaceChildren();
      if (kind === 'bars') node('rect', { x: PAD.left + at * band, y: PAD.top, width: band, height: plotH, class: 'band' }, hover);
      else node('line', { x1: xMid(at), x2: xMid(at), y1: PAD.top, y2: PAD.top + plotH, class: 'cross' }, hover);
      if (kind === 'line') for (const s of o.series) {
        const v = s.values[at];
        if (v !== null && v !== undefined) node('circle', { cx: xMid(at), cy: y(v), r: 4, class: 'dot', style: { fill: paint(s.color) } }, hover);
      }
      tip.replaceChildren();
      html('div', { class: 'tip-head', text: tipFormat(o.x[at]) }, tip);
      const rows = kind === 'bars' && o.stacked ? [...o.series].reverse() : o.series;
      for (const s of rows) {
        const r = html('div', { class: 'tip-row' }, tip);
        html('i', { class: kind === 'line' ? 'key line' : 'key', style: { background: paint(s.color) } }, r);
        html('b', { text: yFormat(s.values[at]) }, r);
        html('span', { text: s.name }, r);
      }
      if (kind === 'bars' && o.stacked && o.series.length > 1) {
        const r = html('div', { class: 'tip-row total' }, tip);
        html('i', { class: 'key none' }, r); html('b', { text: yFormat(totals[at]) }, r); html('span', { text: 'Total' }, r);
      }
      const t0 = o.x[at], t1 = t0 + step;
      for (const m of markers.filter(m => m.at >= t0 && m.at < t1)) html('div', { class: 'tip-note', text: [m.label, m.detail].filter(Boolean).join(' · ') }, tip);
      tip.hidden = false;
      const px = xMid(at), tw = tip.offsetWidth;
      tip.style.left = `${Math.max(0, Math.min(W - tw, px + 12 + tw > W ? px - tw - 12 : px + 12))}px`;
    }
    function hide() { at = -1; hover.replaceChildren(); tip.hidden = true; }
    const idx = e => {
      const r = svg.getBoundingClientRect();
      return Math.floor((e.clientX - r.left - PAD.left) / band);
    };
    svg.addEventListener('pointermove', e => { const i = idx(e); if (i >= 0 && i < n) show(i); else hide(); });
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', e => {
      const d = { ArrowRight: 1, ArrowLeft: -1, Home: -n, End: n }[e.key];
      if (d === undefined) return;
      e.preventDefault(); show(at < 0 ? (d > 0 ? 0 : n - 1) : at + d);
    });

    legend(el, o.series, kind);
    if (markers.length) {
      const ul = html('ul', { class: 'chart-markers' }, el);
      for (const m of markers) {
        const li = html('li', {}, ul);
        html('i', { 'aria-hidden': 'true' }, li);
        html('b', { text: m.label }, li);
        if (m.detail) html('span', { text: m.detail }, li);
      }
    }
    dataTable(el, o, tipFormat, yFormat);
    return { svg, plot, y, xMid, band, top, plotH, W };
  }

  function legend(el, series, kind) {
    if (series.length < 2) return;
    const lg = html('div', { class: 'chart-legend' }, el);
    el.insertBefore(lg, el.firstChild);
    for (const s of series) {
      const sp = html('span', {}, lg);
      html('i', { class: kind === 'line' ? 'line' : '', style: { background: paint(s.color) } }, sp);
      sp.append(document.createTextNode(s.name));
    }
  }

  function dataTable(el, o, tipFormat, yFormat) {
    const d = html('details', { class: 'chart-table' }, el);
    html('summary', { text: 'Show as table' }, d);
    const wrap = html('div', { class: 'table-wrap plain' }, d);
    const t = html('table', {}, wrap);
    const hr = html('tr', {}, html('thead', {}, t));
    html('th', { scope: 'col', text: o.xTitle || 'Time' }, hr);
    for (const s of o.series) html('th', { scope: 'col', class: 'r', text: s.name }, hr);
    const tb = html('tbody', {}, t);
    o.x.forEach((x, i) => {
      const tr = html('tr', {}, tb);
      html('th', { scope: 'row', text: tipFormat(x) }, tr);
      for (const s of o.series) html('td', { class: 'r', text: yFormat(s.values[i]) }, tr);
    });
  }

  // ------------------------------------------------------------------ line
  function line(el, o) {
    mount(el, () => {
      const f = frame(el, o, 'line');
      for (const s of o.series) {
        // A null bucket (no traffic) breaks the line rather than pretending it was zero.
        let d = '', pen = false, last = null;
        s.values.forEach((v, i) => {
          if (v === null || v === undefined) { pen = false; return; }
          d += `${pen ? 'L' : 'M'}${f.xMid(i).toFixed(1)} ${f.y(v).toFixed(1)}`; pen = true; last = i;
        });
        node('path', { d, class: 'series-line', style: { stroke: paint(s.color) } }, f.plot);
        // Lone points (a bucket between two empty ones) still need a visible mark.
        s.values.forEach((v, i) => {
          const prev = s.values[i - 1], next = s.values[i + 1];
          if (v !== null && v !== undefined && (prev === null || prev === undefined) && (next === null || next === undefined))
            node('circle', { cx: f.xMid(i), cy: f.y(v), r: 3, class: 'dot', style: { fill: paint(s.color) } }, f.plot);
        });
        if (last !== null) node('circle', { cx: f.xMid(last), cy: f.y(s.values[last]), r: 4, class: 'dot', style: { fill: paint(s.color) } }, f.plot);
      }
    });
  }

  // ------------------------------------------------------------------ bars (columns)
  // Columns <= 24 px, square at the baseline, rounded at the data end; a 2 px surface gap between stacked segments.
  function bars(el, o) {
    mount(el, () => {
      const f = frame(el, o, 'bars');
      const gap = f.band >= 6 ? 2 : 1, groups = o.stacked ? [o.series] : o.series.map(s => [s]);
      const w = Math.max(1, Math.min(24, (f.band - gap) / groups.length - (groups.length > 1 ? gap : 0)));
      const r = Math.min(4, w / 2);
      o.x.forEach((_, i) => groups.forEach((segs, gi) => {
        const x0 = f.xMid(i) - (groups.length * w + (groups.length - 1) * gap) / 2 + gi * (w + gap);
        let base = 0;
        const parts = segs.map(s => ({ s, v: +s.values[i] || 0 })).filter(p => p.v > 0);
        parts.forEach((p, k) => {
          const yb = f.y(base) - (k ? gap / 2 : 0), yt = f.y(base + p.v) + (k < parts.length - 1 ? gap / 2 : 0);
          base += p.v;
          const h = yb - yt;
          if (h <= 0.4) return;
          const top = k === parts.length - 1, rr = top ? Math.min(r, h) : 0;
          node('path', { class: 'bar', style: { fill: paint(p.s.color) },
            d: `M${x0} ${yb}V${yt + rr}${rr ? `Q${x0} ${yt} ${x0 + rr} ${yt}` : ''}H${x0 + w - rr}${rr ? `Q${x0 + w} ${yt} ${x0 + w} ${yt + rr}` : ''}V${yb}Z` }, f.plot);
        });
      }));
    });
  }

  // ------------------------------------------------------------------ waterfall
  // One row per span, nested under its parent, positioned by its offset from the trace's first start. The gap between
  // whatsapp.ingest ending and whatsapp.route starting is time on the SQS queue: not a span, so it is drawn as its own
  // labelled row. Built as HTML so the labels wrap and stack on a phone.
  const STAGE = [
    [/^whatsapp\.ingest$/, 'ingest', 'Ingest'],
    [/^(whatsapp\.route|whatsapp\.legacy|agent\.call)$/, 'router', 'Router'],
    [/^agent\.invoke$/, 'agent', 'Agent'],
    [/^tool\./, 'tool', 'Agent tool'],
  ];
  const stageOf = s => (STAGE.find(([re]) => re.test(s.name || '')) || [null, 'other', 'Other'])[1];
  const ms = v => v >= 10000 ? `${(v / 1000).toFixed(1)} s` : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
  const tms = iso => Date.parse(iso);

  function waterfall(el, o) {
    const spans = (o.spans || []).map(s => ({ ...s, t: tms(s.start_ts), d: +s.duration_ms || 0 })).filter(s => !isNaN(s.t));
    el.replaceChildren();
    el.classList.add('waterfall');
    if (!spans.length) return;
    const start = o.traceStart ? tms(o.traceStart) : Math.min(...spans.map(s => s.t));
    const end = Math.max(...spans.map(s => s.t + s.d));
    const total = Math.max(1, end - start);

    // Depth-first order: each span followed by its children, by start time. Orphans (parent not in this trace) are roots.
    const ids = new Set(spans.map(s => s.span_id)), kids = new Map();
    for (const s of spans) {
      const p = s.parent_id && ids.has(s.parent_id) ? s.parent_id : null;
      if (!kids.has(p)) kids.set(p, []);
      kids.get(p).push(s);
    }
    const rows = [];
    (function walk(parent, depth) {
      for (const s of (kids.get(parent) || []).sort((a, b) => a.t - b.t)) {
        rows.push({ span: s, depth });
        // Queue wait goes right after ingest, at the depth of the routing span it led to.
        if (s.name === 'whatsapp.ingest') {
          const route = (kids.get(s.span_id) || []).find(c => c.name === 'whatsapp.route');
          const gap = route ? route.t - (s.t + s.d) : 0;
          if (route && gap > 0) rows.push({ queue: true, t: s.t + s.d, d: gap, depth: depth + 1 });
        }
        walk(s.span_id, depth + 1);
      }
    })(null, 0);

    const lg = html('div', { class: 'chart-legend wf-legend' }, el);
    const used = new Set(rows.map(r => r.queue ? 'queue' : stageOf(r.span)));
    if (spans.some(s => s.status === 'ERROR')) used.add('error');
    for (const [k, label] of [['ingest', 'Ingest'], ['queue', 'Queue wait'], ['router', 'Router'], ['agent', 'Agent'],
      ['tool', 'Agent tool'], ['other', 'Other'], ['error', 'Error']]) {
      if (!used.has(k)) continue;
      const sp = html('span', {}, lg);
      html('i', { class: `wf-${k}` }, sp);
      sp.append(document.createTextNode(label));
    }

    // Time axis: 0, ¼, ½, ¾ and the end of the trace.
    const axis = html('div', { class: 'wf-row wf-axis', 'aria-hidden': 'true' }, el);
    html('div', { class: 'wf-label' }, axis);
    html('span', { class: 'wf-dur' }, axis);
    const at = html('div', { class: 'wf-track' }, axis);
    [0, 0.25, 0.5, 0.75, 1].forEach(f => html('span', { text: ms(total * f), style: { left: `${f * 100}%` }, class: f === 1 ? 'end' : f === 0 ? 'start' : '' }, at));

    const list = html('ol', { class: 'wf-list', 'aria-label': `Trace timeline, ${ms(total)} in total` }, el);
    for (const r of rows) {
      const s = r.span, t = r.queue ? r.t : s.t, d = r.queue ? r.d : s.d;
      const kind = r.queue ? 'queue' : s.status === 'ERROR' ? 'error' : stageOf(s);
      const left = ((t - start) / total) * 100, width = (d / total) * 100;
      const li = html('li', { class: `wf-row${r.queue ? ' is-queue' : s.status === 'ERROR' ? ' is-error' : ''}` }, list);
      if (!r.queue) li.dataset.span = s.span_id;
      li.style.setProperty('--depth', r.depth);
      const lab = html('div', { class: 'wf-label' }, li);
      html('b', { text: r.queue ? 'Queue wait' : s.name }, lab);
      const meta = html('span', {}, lab);
      if (r.queue) meta.textContent = 'SQS, between ingest and router';
      else {
        const a = s.attrs || {}, bits = [s.service];
        if (a['gen_ai.usage.input_tokens'] != null) bits.push(`${plain.format(a['gen_ai.usage.input_tokens'])} in / ${plain.format(a['gen_ai.usage.output_tokens'] || 0)} out tokens`);
        if (a['trialtalk.kb.results'] != null) bits.push(`${a['trialtalk.kb.results']} result${+a['trialtalk.kb.results'] === 1 ? '' : 's'}`);
        if (a['http.response.status_code'] != null) bits.push(`HTTP ${a['http.response.status_code']}`);
        if (s.status === 'ERROR') bits.push('Error');
        meta.textContent = bits.filter(Boolean).join(' · ');
      }
      html('span', { class: 'wf-dur', text: ms(d), 'aria-hidden': 'true' }, li);
      const track = html('div', { class: 'wf-track', 'aria-hidden': 'true' }, li);
      const sum = `${r.queue ? 'Queue wait' : s.name}: starts at ${ms(t - start)}, takes ${ms(d)}${!r.queue && s.status === 'ERROR' ? ', error' : ''}`;
      html('i', { class: `wf-bar wf-${kind}`, title: sum, style: { left: `${left}%`, width: `max(2px, ${width}%)` } }, track);
      html('span', { class: 'sr-only', text: sum }, li);
    }
  }

  window.ttCharts = { line, bars, waterfall };
})();
