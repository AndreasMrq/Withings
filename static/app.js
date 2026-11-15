/* Pulse – self-hosted health dashboard for Withings data. Vanilla JS + Chart.js. */
'use strict';

// ------------------------------------------------------------------------------------
// utilities
// ------------------------------------------------------------------------------------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? '–' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const hm = (s) => { if (!s && s !== 0) return '–'; const m = Math.round(s / 60); return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`; };
const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const addDays = (iso, n) => { const d = new Date(iso + 'T12:00:00'); d.setDate(d.getDate() + n); return isoDate(d); };
const prettyDate = (iso, opts = { weekday: 'long', day: 'numeric', month: 'long' }) => new Date(iso + 'T12:00:00').toLocaleDateString([], opts);
const shortDate = (iso) => new Date(iso + 'T12:00:00').toLocaleDateString([], { day: 'numeric', month: 'short' });
const avg = (xs) => { const v = xs.filter((x) => x !== null && x !== undefined); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
const rolling = (xs, n) => xs.map((_, i) => avg(xs.slice(Math.max(0, i - n + 1), i + 1)));

const MOODS = [['😣', 'awful'], ['🙁', 'bad'], ['😐', 'okay'], ['🙂', 'good'], ['😄', 'great']];
const ENERGY = [['🪫', 'drained'], ['😮‍💨', 'low'], ['⚖️', 'normal'], ['🔋', 'good'], ['⚡', 'high']];
const WORKOUT_NAMES = { 1: 'Walk', 2: 'Run', 3: 'Hiking', 4: 'Skating', 6: 'Cycling', 7: 'Swimming', 8: 'Surfing', 12: 'Tennis', 15: 'Badminton', 16: 'Strength', 17: 'Calisthenics', 18: 'Elliptical', 19: 'Pilates', 20: 'Basketball', 21: 'Soccer', 27: 'Golf', 28: 'Yoga', 29: 'Dancing', 30: 'Boxing', 33: 'Martial arts', 34: 'Skiing', 35: 'Snowboarding', 36: 'Other', 187: 'Rowing', 188: 'Zumba', 195: 'Climbing', 272: 'Multi-sport', 306: 'Indoor walk', 307: 'Treadmill', 308: 'Indoor cycling' };

let STATE = { status: null, tags: [], today: isoDate(new Date()) };
let charts = [];

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (_) { /* ignore */ } throw new Error(m); }
  return r.json();
}
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 2200); }

// status colours (always paired with a text label)
const STATUS = {
  good: () => css('--good'), warn: () => css('--warning'), serious: () => css('--serious'), bad: () => css('--critical'), neutral: () => css('--axis'),
};
function scoreTone(v) { if (v === null || v === undefined) return 'neutral'; return v >= 75 ? 'good' : v >= 50 ? 'warn' : v >= 25 ? 'serious' : 'bad'; }
const HRV_TONE = { 'Balanced': 'good', 'Unbalanced (low)': 'serious', 'Unbalanced (high)': 'warn', 'Low': 'bad' };
const LOAD_TONE = { 'Productive': 'good', 'Maintaining': 'good', 'Recovery': 'warn', 'High load': 'warn', 'Detraining': 'serious', 'Strained': 'bad', 'Overreaching': 'bad' };
function badge(text, tone) { if (!text) return ''; return `<span class="badge"><i class="dot" style="background:${STATUS[tone || 'neutral']()}"></i>${esc(text)}</span>`; }

// ------------------------------------------------------------------------------------
// charts
// ------------------------------------------------------------------------------------
function chartDefaults() {
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 11;
  Chart.defaults.color = css('--muted');
  Chart.defaults.borderColor = css('--grid');
  Chart.defaults.animation = false;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.plugins.legend.display = false;
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: css('--surface'), titleColor: css('--ink'), bodyColor: css('--ink-2'),
    borderColor: css('--border'), borderWidth: 1, padding: 10, cornerRadius: 10, boxPadding: 4, usePointStyle: true,
  });
}
function destroyCharts() { charts.forEach((c) => c.destroy()); charts = []; }
function mkChart(canvas, cfg) { if (!canvas) return null; const c = new Chart(canvas, cfg); charts.push(c); return c; }

const crosshair = {
  id: 'crosshair',
  afterDraw(chart) {
    const a = chart.tooltip?.getActiveElements?.();
    if (!a || !a.length) return;
    const x = a[0].element.x; const { top, bottom } = chart.chartArea; const ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = css('--axis'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};
// shaded bands (e.g. sleep windows, HRV baseline)
const bands = {
  id: 'bands',
  beforeDatasetsDraw(chart, _a, opts) {
    if (!opts?.items?.length) return;
    const { ctx, chartArea: { top, bottom, left, right }, scales: { x } } = chart;
    ctx.save();
    for (const b of opts.items) {
      const x0 = Math.max(left, x.getPixelForValue(b.from)); const x1 = Math.min(right, x.getPixelForValue(b.to));
      if (x1 <= x0) continue;
      ctx.fillStyle = b.color; ctx.fillRect(x0, top, x1 - x0, bottom - top);
      if (b.label) { ctx.fillStyle = css('--muted'); ctx.font = '11px system-ui'; ctx.fillText(b.label, x0 + 4, top + 12); }
    }
    ctx.restore();
  },
};
Chart.register(crosshair, bands);

function timeAxis(min, max) {
  return { type: 'linear', min, max, grid: { display: false }, border: { color: css('--axis') },
    ticks: { maxTicksLimit: 7, callback: (v) => new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) } };
}
function yAxis(extra = {}) { return { grid: { color: css('--grid') }, border: { display: false }, ticks: { maxTicksLimit: 5 }, ...extra }; }
function lineDs(label, data, color, extra = {}) {
  return { label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: css('--surface'), tension: 0.25, spanGaps: true, ...extra };
}
function dateLine(canvas, dates, series, opts = {}) {
  return mkChart(canvas, {
    type: 'line',
    data: { labels: dates, datasets: series },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: { x: { grid: { display: false }, border: { color: css('--axis') }, ticks: { maxTicksLimit: 6, maxRotation: 0, callback(v) { return shortDate(this.getLabelForValue(v)); } } }, y: yAxis(opts.y || {}) },
      plugins: { tooltip: { callbacks: { title: (i) => prettyDate(i[0].label, { weekday: 'short', day: 'numeric', month: 'short' }), ...(opts.tooltip || {}) } }, legend: { display: !!opts.legend, position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, color: css('--ink-2') } } },
    },
  });
}
function dateBars(canvas, dates, datasets, opts = {}) {
  return mkChart(canvas, {
    type: 'bar',
    data: { labels: dates, datasets: datasets.map((d) => ({ borderRadius: 4, borderSkipped: 'start', maxBarThickness: 18, ...d })) },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: { x: { stacked: !!opts.stacked, grid: { display: false }, border: { color: css('--axis') }, ticks: { maxTicksLimit: 6, maxRotation: 0, callback(v) { return shortDate(this.getLabelForValue(v)); } } }, y: yAxis({ stacked: !!opts.stacked, ...(opts.y || {}) }) },
      plugins: { tooltip: { callbacks: { title: (i) => prettyDate(i[0].label, { weekday: 'short', day: 'numeric', month: 'short' }), ...(opts.tooltip || {}) } }, legend: { display: !!opts.legend, position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, color: css('--ink-2') } } },
    },
  });
}

function ring(value, max, color, sub) {
  const r = 52; const c = 2 * Math.PI * r; const frac = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value / max));
  return `<svg class="ring" viewBox="0 0 130 130" role="img" aria-label="${esc(sub)} ${value ?? 'no data'}">
    <circle cx="65" cy="65" r="${r}" fill="none" stroke="${css('--surface-2')}" stroke-width="11"/>
    <circle cx="65" cy="65" r="${r}" fill="none" stroke="${color}" stroke-width="11" stroke-linecap="round"
      stroke-dasharray="${c * frac} ${c}" transform="rotate(-90 65 65)"/>
    <text x="65" y="70" text-anchor="middle" class="val">${value ?? '–'}</text>
    <text x="65" y="90" text-anchor="middle" class="sub">${esc(sub)}</text></svg>`;
}
function tile(label, value, unit, meta = '', extra = '', right = '') {
  return `<div class="card tile"><div class="label"><span>${label}</span>${right}</div><div class="value num">${value}${unit ? `<small>${unit}</small>` : ''}</div>${meta ? `<div class="meta">${meta}</div>` : ''}${extra}</div>`;
}
function parts(obj, labels) {
  return `<div class="parts">${Object.entries(obj || {}).map(([k, v]) => {
    const max = labels[k]?.[1] || 100; const pct = Math.round((100 * v) / max);
    return `<div class="part"><span>${labels[k]?.[0] || k}</span><div class="track"><i style="width:${pct}%;background:${STATUS[scoreTone(pct)]()}"></i></div><span class="num muted">${Math.round(v)}${max !== 100 ? '/' + max : ''}</span></div>`;
  }).join('')}</div>`;
}

const STRAIN_TONE = { normal: 'good', watch: 'warn', alert: 'bad' };
const STRAIN_SIG = { rhr: ['Resting HR', 'bpm', 0, 1], hrv: ['HRV', 'ms', 1, -1], respiration: ['Breathing rate', 'brpm', 1, 1], skin_temp: ['Skin temp', '°C', 2, 1], spo2_night: ['Night SpO₂', '%', 1, -1] }; // label, unit, decimals, worse direction
const ordinal = (n) => `${n}${[, 'st', 'nd', 'rd'][n % 100 > 3 && n % 100 < 21 ? 0 : n % 10] || 'th'}`;
const clockH = (ts, wrap) => { const t = new Date(ts * 1000); const h = t.getHours() + t.getMinutes() / 60; return +(wrap && h < 12 ? h + 24 : h).toFixed(2); };
const hhmm = (h) => { const m = Math.round(((h % 24) + 24) % 24 * 60); return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`; };
function strainCard(m) {
  const sig = m.strain_signals || {}; const keys = Object.keys(STRAIN_SIG).filter((k) => sig[k]);
  const note = '<p class="small muted">Early-warning hint from your own overnight data, not a medical device.</p>';
  if (!m.strain_level || keys.length < 2) return `<div class="card mt"><div class="card-head"><h3>Body signals</h3></div><p class="muted">Building baseline — needs about two weeks of nights to compare against.</p>${note}</div>`;
  const hi = keys.filter((k) => sig[k].z >= 1.5);
  const phrase = (dir) => { const n = hi.filter((k) => STRAIN_SIG[k][3] === dir).map((k) => STRAIN_SIG[k][0].toLowerCase()); return n.length ? `${n.join(' and ')} ${n.length > 1 ? 'are' : 'is'} ${dir > 0 ? 'above' : 'below'} your baseline` : ''; };
  const sent = hi.length && m.strain_level !== 'normal'
    ? `${[phrase(1), phrase(-1)].filter(Boolean).join(' and ').replace(/^./, (c) => c.toUpperCase())}${m.strain_streak > 1 ? ` for the ${ordinal(m.strain_streak)} day` : ''} — consider taking it easy.` : '';
  return `<div class="card mt"><div class="card-head"><h3>Body signals</h3><span class="hint">last night vs. your 28-day baseline</span>${badge(m.strain_level, STRAIN_TONE[m.strain_level])}</div>
    ${sent ? `<p>${esc(sent)}</p>` : ''}
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Signal</th><th class="r">Last night</th><th class="r">Baseline</th><th class="r">Deviation</th></tr></thead><tbody>
    ${keys.map((k) => { const [lbl, unit, dg] = STRAIN_SIG[k]; const s = sig[k]; const diff = s.value - s.baseline; const tone = s.z >= 2.5 ? 'bad' : s.z >= 1.5 ? 'warn' : 'good';
      return `<tr><td>${lbl}</td><td class="r">${fmt(s.value, dg)} ${unit}</td><td class="r muted">${fmt(s.baseline, dg)} ${unit}</td><td class="r">${badge(`${diff > 0 ? '+' : ''}${fmt(diff, dg)} ${unit}`, tone)}</td></tr>`; }).join('')}
    </tbody></table></div>${note}</div>`;
}

// ------------------------------------------------------------------------------------
// router
// ------------------------------------------------------------------------------------
const ROUTES = { today: viewToday, sleep: viewSleep, trends: viewTrends, training: viewTraining, insights: viewInsights, journal: viewJournal, settings: viewSettings };
async function route() {
  const [name, arg] = (location.hash.replace(/^#\/?/, '').split('?')[0] || 'today').split('/');
  const fn = ROUTES[name] || viewToday;
  $$('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.r === name));
  $('.icon-btn').classList.toggle('active', name === 'settings');
  destroyCharts();
  chartDefaults();
  const view = $('#view');
  view.innerHTML = '<div class="empty muted">Loading…</div>';
  try { await fn(view, arg); } catch (e) { view.innerHTML = `<div class="card empty"><h2>Something went wrong</h2><p>${esc(e.message)}</p></div>`; console.error(e); }
}

async function refreshStatus() {
  STATE.status = await api('/api/status');
  STATE.today = STATE.status.today;
  const s = STATE.status; const b = $('#banner');
  if (s.demo) b.innerHTML = '<div class="notice">You are looking at <b>demo data</b>. <a href="#/settings">Connect your Withings account</a> to use your own.</div>';
  else if (!s.data.days && !s.withings_connected) b.innerHTML = '<div class="notice">No data yet. <a href="#/settings">Connect Withings</a> or load demo data to explore.</div>';
  else if (s.sync.last_error) b.innerHTML = `<div class="notice">Last sync failed: ${esc(s.sync.last_error)} <a href="#/settings">Settings</a></div>`;
  else b.innerHTML = '';
}

function dateNav(base, date) {
  const isToday = date >= STATE.today;
  return `<div class="datenav">
    <a class="btn sm" href="#/${base}/${addDays(date, -1)}" aria-label="Previous day">‹</a>
    <input type="date" value="${date}" max="${STATE.today}" onchange="location.hash='#/${base}/'+this.value">
    <a class="btn sm" ${isToday ? 'aria-disabled="true" style="opacity:.4;pointer-events:none"' : ''} href="#/${base}/${addDays(date, 1)}" aria-label="Next day">›</a>
    ${isToday ? '' : `<a class="btn sm" href="#/${base}">Today</a>`}
  </div>`;
}

function noData(view) {
  view.innerHTML = `<div class="card empty"><h2>No data yet</h2><p>Connect your Withings account in settings, or load demo data to explore the app.</p><a class="btn primary" href="#/settings">Open settings</a></div>`;
}

// ------------------------------------------------------------------------------------
// Today
// ------------------------------------------------------------------------------------
async function viewToday(view, date) {
  date = date || STATE.today;
  if (!STATE.status?.data?.days) return noData(view);
  const d = await api(`/api/day/${date}`);
  const m = d.metrics; const prof = await api('/api/profile');
  const isToday = date === STATE.today;
  const energyNow = d.energy.length ? Math.round(d.energy[d.energy.length - 1][1]) : null;
  const energyVal = isToday ? energyNow : m.energy_end;
  const stepPct = m.steps ? Math.min(100, (100 * m.steps) / (prof.step_goal || 10000)) : 0;
  const recNow = m.recovery_h;

  view.innerHTML = `
  <div class="page-head"><h2>${isToday ? 'Today' : prettyDate(date)}</h2>${dateNav('today', date)}</div>
  <div class="hero">
    <a class="card ring-card" href="#/today/${date}" onclick="document.getElementById('readinessCard').scrollIntoView({behavior:'smooth'});return false;">
      <h3>Readiness</h3>${ring(m.readiness, 100, STATUS[scoreTone(m.readiness)](), m.readiness_label || 'no data')}
      <div class="ring-meta">${m.readiness_label ? 'Training readiness' : 'Needs sleep data'}</div></a>
    <div class="card ring-card"><h3>Body energy</h3>${ring(energyVal, 100, css('--aqua'), isToday ? 'now' : 'end of day')}
      <div class="ring-meta num">wake ${fmt(m.energy_wake)} · +${fmt(m.energy_charged)} / −${fmt(m.energy_drained)}</div></div>
    <a class="card ring-card" href="#/sleep/${date}"><h3>Sleep</h3>${ring(m.sleep_score, 100, css('--blue'), m.sleep_total_s ? hm(m.sleep_total_s) : 'no data')}
      <div class="ring-meta">${m.sleep_score_withings ? `Withings: ${m.sleep_score_withings}` : 'Sleep score'}</div></a>
  </div>

  <div class="grid tiles mt">
    ${tile('HRV', fmt(m.hrv), 'ms', `7-day avg ${fmt(m.hrv_7d)} ms${m.hrv_base_low ? ` · range ${fmt(m.hrv_base_low)}–${fmt(m.hrv_base_high)}` : ''}`, '', badge(m.hrv_status === 'Building baseline' ? 'baseline' : m.hrv_status, HRV_TONE[m.hrv_status]))}
    ${tile('Resting HR', fmt(m.rhr), 'bpm', `sleep avg ${fmt(m.sleep_hr_avg)} · min ${fmt(m.sleep_hr_min)}`)}
    ${tile('Stress', fmt(m.stress_avg), '/100', m.stress_min ? `high ${fmt(m.stress_min.high)}m · rest ${fmt(m.stress_min.rest)}m` : '', stressBar(m.stress_min))}
    ${tile('Steps', fmt(m.steps), '', `goal ${fmt(prof.step_goal)} · ${fmt((m.distance_m || 0) / 1000, 1)} km`, `<div class="bar"><i style="width:${stepPct}%"></i></div>`)}
    ${tile('Intensity min', fmt(m.intensity_min), '', 'moderate + 2× vigorous')}
    ${tile('Training load', fmt(m.training_load), '', `7d ${fmt(m.load_acute)} · 28d ${fmt(m.load_chronic)}`, '', badge(m.load_status, LOAD_TONE[m.load_status]))}
    ${tile('Recovery', fmt(recNow), 'h', recNow ? 'until fully recovered' : 'fully recovered')}
    ${tile('VO₂ max', fmt(m.vo2max, 1), '', `fitness age ${fmt(m.fitness_age)}${m.vo2max_source === 'estimate' ? ' · estimate' : ''}`)}
    ${tile('Respiration', fmt(m.respiration, 1), 'brpm', 'avg during sleep')}
    ${tile('SpO₂', fmt(m.spo2, 1), '%', m.spo2_night ? `night ${fmt(m.spo2_night, 1)}%` : '')}
    ${tile('Calories', fmt(m.calories_total), 'kcal', `active ${fmt(m.calories_active)}`)}
    ${m.weight ? tile('Weight', fmt(m.weight, 1), 'kg', m.fat_ratio ? `body fat ${fmt(m.fat_ratio, 1)}%` : '') : ''}
    ${m.skin_temp ? tile('Skin temp', fmt(m.skin_temp, 2), '°C', '') : ''}
  </div>

  ${strainCard(m)}

  <div class="grid g2 mt">
    <div class="card span2"><div class="card-head"><h3>Heart rate</h3><span class="hint">shaded = sleep · max ${fmt(m.hr_max)} bpm</span></div><div class="chart-box"><canvas id="cHr"></canvas></div></div>
    <div class="card"><div class="card-head"><h3>Body energy</h3><span class="hint">charged by sleep & rest, drained by stress & activity</span></div><div class="chart-box short"><canvas id="cEnergy"></canvas></div></div>
    <div class="card"><div class="card-head"><h3>Stress</h3><span class="hint">from HR while not moving</span></div><div class="chart-box short"><canvas id="cStress"></canvas></div>
      <div class="legend mt"><span><i style="background:${css('--light')}"></i>rest</span><span><i style="background:#f3a27f"></i>low</span><span><i style="background:${css('--orange')}"></i>medium</span><span><i style="background:#a83c12"></i>high</span><span><i style="background:${css('--axis')}"></i>active</span></div></div>
  </div>

  <div class="grid g2 mt">
    <div class="card" id="readinessCard"><div class="card-head"><h3>Readiness breakdown</h3></div>
      ${m.readiness_parts && Object.keys(m.readiness_parts).length ? parts(m.readiness_parts, { sleep: ['Sleep'], sleep_history: ['Sleep history'], hrv: ['HRV vs baseline'], resting_hr: ['Resting HR'], recovery: ['Recovery time'], load_balance: ['Load balance'] }) : '<p class="muted">Not enough data.</p>'}
      <p class="small muted">Higher is better for each factor. Built from last night's sleep, your HRV and resting HR against your personal baseline, remaining recovery time and acute/chronic load.</p></div>
    <div class="card"><div class="card-head"><h3>Journal</h3><button class="btn sm" onclick="openLog({ts:${isToday ? 'null' : `Math.floor(new Date('${date}T20:00').getTime()/1000)`}})">+ Add</button></div>
      ${d.journal.length ? `<div class="entries">${d.journal.map(entryHtml).join('')}</div>` : '<p class="muted">Nothing logged for this day. Tap + to note mood, energy or anything special (alcohol, headache, little sleep…).</p>'}</div>
  </div>
  ${d.workouts.length ? `<div class="card mt"><div class="card-head"><h3>Workouts</h3></div>${d.workouts.map((w) => `<div class="row"><b>${WORKOUT_NAMES[w.category] || 'Workout'}</b><span class="muted">${timeOf(w.start)}–${timeOf(w.end)} · ${hm(w.end - w.start)}</span><span class="spacer"></span><span class="num">avg ${fmt(w.data.hr_average)} bpm · ${fmt(w.data.calories)} kcal</span></div>`).join('')}</div>` : ''}
  `;

  const a = new Date(date + 'T00:00:00').getTime(); const b = a + 86400000;
  const lo = m.sleep_start ? Math.min(a, m.sleep_start * 1000) : a;
  const sleepBand = m.sleep_start ? [{ from: m.sleep_start * 1000, to: m.sleep_end * 1000, color: `color-mix(in srgb, ${css('--blue')} 10%, transparent)`, label: 'sleep' }] : [];
  mkChart($('#cHr'), {
    type: 'line',
    data: { datasets: [lineDs('Heart rate', d.hr.map(([t, v]) => ({ x: t * 1000, y: v })), css('--red'), { borderWidth: 1.5, tension: 0.15 })] },
    options: { interaction: { mode: 'nearest', axis: 'x', intersect: false }, scales: { x: timeAxis(lo, isToday ? Date.now() : b), y: yAxis({ suggestedMin: 40 }) },
      plugins: { bands: { items: sleepBand }, tooltip: { callbacks: { title: (i) => timeOf(i[0].parsed.x / 1000), label: (i) => ` ${i.parsed.y} bpm` } } } },
  });
  mkChart($('#cEnergy'), {
    type: 'line',
    data: { datasets: [lineDs('Body energy', d.energy.map(([t, v]) => ({ x: t * 1000, y: v })), css('--aqua'), { fill: { target: 'origin', above: `color-mix(in srgb, ${css('--aqua')} 18%, transparent)` } })] },
    options: { interaction: { mode: 'nearest', axis: 'x', intersect: false }, scales: { x: timeAxis(a, isToday ? Date.now() : b), y: yAxis({ min: 0, max: 100 }) },
      plugins: { tooltip: { callbacks: { title: (i) => timeOf(i[0].parsed.x / 1000), label: (i) => ` Energy ${Math.round(i.parsed.y)}` } } } },
  });
  const sCol = (s) => (s < 0 ? css('--axis') : s <= 25 ? css('--light') : s <= 50 ? '#f3a27f' : s <= 75 ? css('--orange') : '#a83c12');
  mkChart($('#cStress'), {
    type: 'bar',
    data: { datasets: [{ label: 'Stress', data: d.stress.map(([t, v]) => ({ x: t * 1000, y: v < 0 ? 8 : Math.max(2, v) })), backgroundColor: d.stress.map(([, v]) => sCol(v)), barPercentage: 1, categoryPercentage: 1, borderRadius: 2, borderSkipped: 'start' }] },
    options: { scales: { x: timeAxis(a, isToday ? Date.now() : b), y: yAxis({ min: 0, max: 100 }) },
      plugins: { tooltip: { callbacks: { title: (i) => timeOf(i[0].parsed.x / 1000), label: (i) => { const v = d.stress[i.dataIndex][1]; return v < 0 ? ' active' : ` stress ${v}`; } } } } },
  });
}
function stressBar(sm) {
  if (!sm) return '';
  const tot = Object.values(sm).reduce((x, y) => x + y, 0) || 1;
  const cols = { rest: css('--light'), low: '#f3a27f', medium: css('--orange'), high: '#a83c12' };
  return `<div class="stagebar" style="height:8px">${Object.entries(sm).map(([k, v]) => `<i title="${k} ${v} min" style="width:${(100 * v) / tot}%;background:${cols[k]}"></i>`).join('')}</div>`;
}

// ------------------------------------------------------------------------------------
// Sleep
// ------------------------------------------------------------------------------------
async function viewSleep(view, date) {
  date = date || STATE.today;
  if (!STATE.status?.data?.days) return noData(view);
  const [d, tr] = await Promise.all([api(`/api/day/${date}`), api(`/api/trends?days=30&end=${date}`)]);
  const m = d.metrics;
  const has = !!m.sleep_start;
  const stages = [['deep', m.sleep_deep_s, css('--deep')], ['light', m.sleep_light_s, css('--light')], ['REM', m.sleep_rem_s, css('--rem')], ['awake', m.sleep_awake_s, css('--awake')]];
  const tot = stages.reduce((x, s) => x + (s[1] || 0), 0) || 1;
  view.innerHTML = `
  <div class="page-head"><h2>Sleep · ${prettyDate(date, { weekday: 'short', day: 'numeric', month: 'short' })}</h2>${dateNav('sleep', date)}</div>
  ${has ? `
  <div class="grid g3">
    <div class="card ring-card"><h3>Sleep score</h3>${ring(m.sleep_score, 100, css('--blue'), hm(m.sleep_total_s))}
      <div class="ring-meta">${timeOf(m.sleep_start)} → ${timeOf(m.sleep_end)}${m.sleep_score_withings ? ` · Withings ${m.sleep_score_withings}` : ''}</div></div>
    <div class="card span2"><div class="card-head"><h3>Score breakdown</h3></div>
      ${parts(m.sleep_score_parts, { duration: ['Duration', 40], deep: ['Deep sleep', 15], rem: ['REM sleep', 15], efficiency: ['Efficiency', 15], restfulness: ['Restfulness', 15] })}
      <div class="kv mt small"><dt>Time in bed</dt><dd>${hm(m.sleep_inbed_s)} · efficiency ${fmt((m.sleep_efficiency || 0) * 100)}%</dd>
      <dt>Fell asleep in</dt><dd>${m.sleep_latency_s != null ? Math.round(m.sleep_latency_s / 60) + ' min' : '–'} · ${fmt(m.sleep_wakeups)} wake-ups</dd>
      ${m.naps_s ? `<dt>Naps</dt><dd>${hm(m.naps_s)}</dd>` : ''}</div></div>
  </div>
  <div class="card mt"><div class="card-head"><h3>Sleep stages</h3></div>
    <div id="hypno"></div>
    <div class="stagebar">${stages.map(([n, v, c]) => `<i title="${n}" style="width:${(100 * (v || 0)) / tot}%;background:${c}"></i>`).join('')}</div>
    <div class="legend">${stages.map(([n, v, c]) => `<span><i style="background:${c}"></i>${n} <b class="num">${hm(v)}</b> <span class="muted">${fmt((100 * (v || 0)) / tot)}%</span></span>`).join('')}</div></div>
  <div class="grid tiles mt">
    ${tile('HRV (RMSSD)', fmt(m.hrv), 'ms', `7d ${fmt(m.hrv_7d)} ms`, '', badge(m.hrv_status === 'Building baseline' ? 'baseline' : m.hrv_status, HRV_TONE[m.hrv_status]))}
    ${tile('Avg sleeping HR', fmt(m.sleep_hr_avg), 'bpm', `lowest ${fmt(m.sleep_hr_min)} bpm`)}
    ${tile('Respiration', fmt(m.respiration, 1), 'brpm')}
    ${tile('SpO₂', fmt(m.spo2_night, 1), '%')}
    ${tile('Snoring', m.snoring_s != null ? Math.round(m.snoring_s / 60) : '–', 'min')}
    ${m.breathing_disturbances != null ? tile('Breathing disturb.', fmt(m.breathing_disturbances), '', m.ahi != null ? `AHI ${fmt(m.ahi, 1)}` : '') : ''}
    ${tile('Body energy at wake', fmt(m.energy_wake), '/100')}
  </div>
  <div class="grid g2 mt">
    <div class="card"><div class="card-head"><h3>Heart rate during sleep</h3></div><div class="chart-box short"><canvas id="cSHr"></canvas></div></div>
    <div class="card"><div class="card-head"><h3>HRV during sleep</h3><span class="hint">RMSSD, 5-min</span></div><div class="chart-box short"><canvas id="cSHrv"></canvas></div></div>
  </div>` : '<div class="card empty"><h2>No sleep recorded</h2><p>No sleep session ends on this day.</p></div>'}
  <div class="grid tiles mt">
    ${tile('Regularity', fmt(m.sleep_regularity), '/100', 'bed & wake time consistency, 7 nights', '', m.sleep_regularity != null ? badge(m.sleep_regularity >= 75 ? 'regular' : m.sleep_regularity >= 50 ? 'variable' : 'irregular', scoreTone(m.sleep_regularity)) : '')}
    ${tile('Bedtime variability', m.bedtime_sd_min != null ? '±' + fmt(m.bedtime_sd_min) : '–', 'min', m.waketime_sd_min != null ? `wake-up ±${fmt(m.waketime_sd_min)} min` : 'needs 4 nights')}
    ${tile('Social jetlag', fmt(m.social_jetlag_min), 'min', m.social_jetlag_min != null ? 'free vs. work-day midpoint' : 'needs 2 free and 2 work nights')}
    ${tile('Sleep debt', fmt(m.sleep_debt_h, 1), 'h', 'last 7 nights vs. your target')}
  </div>
  <div class="card mt"><div class="card-head"><h3>Last 30 nights</h3><span class="hint">hours per stage</span></div><div class="chart-box tall"><canvas id="cS30"></canvas></div></div>
  <div class="card mt"><div class="card-head"><h3>Sleep score · 30 nights</h3></div><div class="chart-box short"><canvas id="cS30s"></canvas></div></div>
  <div class="card mt"><div class="card-head"><h3>Bedtime & wake-up · 30 nights</h3><span class="hint">when you slept, local clock time</span></div><div class="chart-box"><canvas id="cS30t"></canvas></div></div>
  `;
  if (has) {
    $('#hypno').innerHTML = hypnogram(d.sleep_states, m.sleep_start, m.sleep_end);
    const s0 = m.sleep_start * 1000; const s1 = m.sleep_end * 1000;
    const shr = d.hr.filter(([t]) => t * 1000 >= s0 && t * 1000 <= s1);
    mkChart($('#cSHr'), { type: 'line', data: { datasets: [lineDs('HR', shr.map(([t, v]) => ({ x: t * 1000, y: v })), css('--red'), { borderWidth: 1.5 })] },
      options: { interaction: { mode: 'nearest', axis: 'x', intersect: false }, scales: { x: timeAxis(s0, s1), y: yAxis() }, plugins: { tooltip: { callbacks: { title: (i) => timeOf(i[0].parsed.x / 1000), label: (i) => ` ${i.parsed.y} bpm` } } } } });
    const hv = (d.sleep_series || []).filter((r) => r[1]);
    mkChart($('#cSHrv'), { type: 'line', data: { datasets: [lineDs('RMSSD', hv.map(([t, v]) => ({ x: t * 1000, y: v })), css('--violet'), { borderWidth: 1.5 })] },
      options: { interaction: { mode: 'nearest', axis: 'x', intersect: false }, scales: { x: timeAxis(s0, s1), y: yAxis({ min: 0 }) }, plugins: { tooltip: { callbacks: { title: (i) => timeOf(i[0].parsed.x / 1000), label: (i) => ` ${Math.round(i.parsed.y)} ms` } } } } });
  }
  const dates = tr.map((x) => x.date); const h = (k) => tr.map((x) => (x[k] ? +(x[k] / 3600).toFixed(2) : null));
  const hTip = { label: (i) => ` ${i.dataset.label}: ${hm(i.parsed.y * 3600)}` };
  dateBars($('#cS30'), dates, [
    { label: 'Deep', data: h('sleep_deep_s'), backgroundColor: css('--deep') },
    { label: 'Light', data: h('sleep_light_s'), backgroundColor: css('--light') },
    { label: 'REM', data: h('sleep_rem_s'), backgroundColor: css('--rem') },
    { label: 'Awake', data: h('sleep_awake_s'), backgroundColor: css('--awake') },
  ].map((x) => ({ ...x, borderColor: css('--surface'), borderWidth: { top: 2 }, borderRadius: 0 })), { stacked: true, legend: true, tooltip: hTip });
  dateLine($('#cS30s'), dates, [lineDs('Sleep score', tr.map((x) => x.sleep_score), css('--blue'), { pointRadius: 2 })], { y: { suggestedMin: 40, max: 100 } });
  const bedH = (ts) => { const h = clockH(ts, false); return h > 12 ? +(h - 24).toFixed(2) : h; }; // before midnight = negative
  dateBars($('#cS30t'), dates, [{ label: 'Asleep', data: tr.map((x) => (x.sleep_start && x.sleep_end ? [bedH(x.sleep_start), clockH(x.sleep_end, false)] : null)), backgroundColor: css('--violet'), borderSkipped: false }],
    { y: { reverse: true, ticks: { stepSize: 2, callback: (v) => hhmm(v) } }, tooltip: { label: (i) => (i.raw ? ` ${hhmm(i.raw[0])} → ${hhmm(i.raw[1])}` : null) } });
}
function hypnogram(states, s0, s1) {
  const W = 1000; const H = 150; const pad = 44; const rows = { 0: 0, 3: 1, 1: 2, 2: 3 }; const rowH = (H - 24) / 4;
  const color = { 0: css('--awake'), 1: css('--light'), 2: css('--deep'), 3: css('--rem') };
  const x = (t) => pad + ((t - s0) / (s1 - s0)) * (W - pad);
  let out = `<svg class="hypno" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
  ['Awake', 'REM', 'Light', 'Deep'].forEach((l, i) => { out += `<text x="0" y="${i * rowH + rowH / 2 + 4}">${l}</text><line x1="${pad}" x2="${W}" y1="${(i + 1) * rowH}" y2="${(i + 1) * rowH}" stroke="${css('--grid')}" stroke-width="1"/>`; });
  for (const [a, b, st] of states) {
    const r = rows[st] ?? 2;
    out += `<rect x="${x(a)}" y="${r * rowH + 3}" width="${Math.max(1, x(Math.min(b, s1)) - x(a))}" height="${rowH - 6}" rx="3" fill="${color[st]}"><title>${['Awake', 'Light', 'Deep', 'REM'][st]} ${timeOf(a)}–${timeOf(b)}</title></rect>`;
  }
  const hours = []; for (let t = Math.ceil(s0 / 3600) * 3600; t < s1; t += 3600) hours.push(t);
  const every = Math.ceil(hours.length / 8);
  hours.forEach((t, i) => { if (i % every === 0) out += `<text x="${x(t)}" y="${H - 4}" text-anchor="middle">${new Date(t * 1000).getHours()}:00</text>`; });
  return out + '</svg>';
}

// ------------------------------------------------------------------------------------
// Trends
// ------------------------------------------------------------------------------------
async function viewTrends(view, rangeArg) {
  if (!STATE.status?.data?.days) return noData(view);
  const days = +(rangeArg || localStorage.getItem('trendDays') || 90);
  try { localStorage.setItem('trendDays', days); } catch (_) { /* ignore */ }
  const tr = await api(`/api/trends?days=${days}`);
  const dates = tr.map((x) => x.date); const col = (k) => tr.map((x) => x[k] ?? null);
  const card = (id, title, hint = '') => `<div class="card"><div class="card-head"><h3>${title}</h3><span class="hint">${hint}</span></div><div class="chart-box"><canvas id="${id}"></canvas></div></div>`;
  const a = (k, dgt = 0) => fmt(avg(col(k)), dgt);
  view.innerHTML = `
  <div class="page-head"><h2>Trends</h2><div class="seg">${[30, 90, 180, 365].map((n) => `<button class="${n === days ? 'on' : ''}" onclick="location.hash='#/trends/${n}'">${n}d</button>`).join('')}</div></div>
  <div class="grid tiles">
    ${tile('Avg readiness', a('readiness'), '')}${tile('Avg sleep score', a('sleep_score'), '')}${tile('Avg sleep', hm(avg(col('sleep_total_s'))), '')}
    ${tile('Avg HRV', a('hrv'), 'ms')}${tile('Avg resting HR', a('rhr'), 'bpm')}${tile('Avg stress', a('stress_avg'), '')}${tile('Avg steps', a('steps'), '')}
  </div>
  <div class="grid g2 mt">
    ${card('tHrv', 'HRV', 'dots = nightly · line = 7-day avg · band = baseline')}
    ${card('tRhr', 'Resting heart rate', 'bpm')}
    ${card('tRead', 'Readiness')}
    ${card('tSleep', 'Sleep score & duration', 'score · 7-day avg')}
    ${card('tStress', 'Average stress')}
    ${card('tEnergy', 'Body energy', 'at wake-up vs. lowest of day')}
    ${card('tSteps', 'Steps')}
    ${card('tVo2', 'VO₂ max', tr.some((x) => x.vo2max_source === 'withings') ? 'Withings cardio fitness' : 'estimated from HRmax / resting HR')}
    ${card('tResp', 'Respiration rate (sleep)', 'breaths / min')}
    ${tr.some((x) => x.strain_score != null) ? card('tStrain', 'Body strain', '0–100 · deviation of overnight signals from your baseline') : ''}
    ${tr.some((x) => x.sleep_regularity != null) ? card('tReg', 'Sleep regularity', '0–100 · bed & wake time consistency') : ''}
    ${tr.some((x) => x.weight) ? card('tWeight', 'Weight', 'kg') : ''}
  </div>`;
  const hrv = col('hrv');
  const bandLo = col('hrv_base_low'); const bandHi = col('hrv_base_high');
  dateLine($('#tHrv'), dates, [
    lineDs('Baseline high', bandHi, 'transparent', { pointHoverRadius: 0, fill: '+1', backgroundColor: `color-mix(in srgb, ${css('--good')} 14%, transparent)`, tension: 0.3 }),
    lineDs('Baseline low', bandLo, 'transparent', { pointHoverRadius: 0, tension: 0.3 }),
    lineDs('7-day avg', col('hrv_7d'), css('--violet')),
    lineDs('Nightly', hrv, css('--violet'), { showLine: false, pointRadius: 2.5, pointBackgroundColor: `color-mix(in srgb, ${css('--violet')} 45%, transparent)`, pointBorderWidth: 0 }),
  ], { tooltip: { label: (i) => (i.parsed.y == null ? null : ` ${i.dataset.label}: ${fmt(i.parsed.y)} ms`) } });
  dateLine($('#tRhr'), dates, [lineDs('Resting HR', col('rhr'), css('--red'), { pointRadius: 1.5 }), lineDs('7-day avg', rolling(col('rhr'), 7), css('--ink-2'), { borderWidth: 1.5, borderDash: [4, 3] })], { legend: true });
  dateLine($('#tRead'), dates, [lineDs('Readiness', col('readiness'), css('--aqua'), { pointRadius: 1.5 })], { y: { min: 0, max: 100 } });
  dateLine($('#tSleep'), dates, [lineDs('Sleep score', col('sleep_score'), css('--blue'), { pointRadius: 1.5 }), lineDs('7-day avg', rolling(col('sleep_score'), 7), css('--ink-2'), { borderWidth: 1.5, borderDash: [4, 3] })], { legend: true, y: { max: 100 } });
  dateLine($('#tStress'), dates, [lineDs('Stress', col('stress_avg'), css('--orange'), { pointRadius: 1.5 })], { y: { min: 0 } });
  dateLine($('#tEnergy'), dates, [lineDs('At wake-up', col('energy_wake'), css('--aqua')), lineDs('Lowest', col('energy_low'), css('--yellow'))], { legend: true, y: { min: 0, max: 100 } });
  dateBars($('#tSteps'), dates, [{ label: 'Steps', data: col('steps'), backgroundColor: css('--blue') }]);
  dateLine($('#tVo2'), dates, [lineDs('VO₂ max', col('vo2max'), css('--green'))], { tooltip: { label: (i) => ` VO₂ max ${fmt(i.parsed.y, 1)}` } });
  dateLine($('#tResp'), dates, [lineDs('Respiration', col('respiration'), css('--blue'), { pointRadius: 1.5 })]);
  if ($('#tStrain')) dateLine($('#tStrain'), dates, [lineDs('Strain', col('strain_score'), css('--orange'), { pointRadius: 1.5 })], { y: { min: 0, max: 100 } });
  if ($('#tReg')) dateLine($('#tReg'), dates, [lineDs('Regularity', col('sleep_regularity'), css('--violet'), { pointRadius: 1.5 })], { y: { min: 0, max: 100 } });
  if ($('#tWeight')) dateLine($('#tWeight'), dates, [lineDs('Weight', col('weight'), css('--ink-2'), { stepped: true })], { tooltip: { label: (i) => ` ${fmt(i.parsed.y, 1)} kg` } });
}

// ------------------------------------------------------------------------------------
// Training
// ------------------------------------------------------------------------------------
async function viewTraining(view) {
  if (!STATE.status?.data?.days) return noData(view);
  const [tr, ws] = await Promise.all([api('/api/trends?days=120'), api('/api/workouts?days=60')]);
  const last = tr[tr.length - 1] || {};
  const dates = tr.map((x) => x.date); const col = (k) => tr.map((x) => x[k] ?? null);
  const hrmax = STATE.status.hrmax || 190;
  const zcols = ['#cde2fb', '#86b6ef', '#3987e5', '#256abf', '#184f95', '#0d366b'];
  // weekly zone minutes (zones 1-5)
  const weeks = {};
  tr.forEach((x) => { const d = new Date(x.date + 'T12:00:00'); const mon = new Date(d); mon.setDate(d.getDate() - ((d.getDay() + 6) % 7)); const k = isoDate(mon); weeks[k] = weeks[k] || [0, 0, 0, 0, 0, 0]; (x.hr_zones_min || []).forEach((v, i) => { weeks[k][i] += v; }); });
  const wk = Object.keys(weeks).sort().slice(-12);
  view.innerHTML = `
  <div class="page-head"><h2>Training</h2></div>
  <div class="grid tiles">
    ${tile('Status', esc(last.load_status || '–'), '', 'based on acute vs. chronic load', '', last.load_status ? `<i class="dot" style="background:${STATUS[LOAD_TONE[last.load_status] || 'neutral']()}"></i>` : '')}
    ${tile('Acute load (7d)', fmt(last.load_acute), '', 'fatigue')}
    ${tile('Chronic load (28d)', fmt(last.load_chronic), '', 'fitness')}
    ${tile('Load ratio', fmt(last.acwr, 2), '', 'sweet spot 0.8 – 1.3')}
    ${tile('Recovery', fmt(last.recovery_h), 'h')}
    ${tile('VO₂ max', fmt(last.vo2max, 1), '', `fitness age ${fmt(last.fitness_age)}`)}
    ${tile('Max HR used', fmt(hrmax), 'bpm', 'set in settings')}
  </div>
  <div class="card mt"><div class="card-head"><h3>Acute vs chronic load</h3><span class="hint">acute above chronic = building fitness; far above = injury / overreaching risk</span></div><div class="chart-box tall"><canvas id="cLoad"></canvas></div></div>
  <div class="grid g2 mt">
    <div class="card"><div class="card-head"><h3>Daily training load</h3><span class="hint">TRIMP</span></div><div class="chart-box"><canvas id="cDaily"></canvas></div></div>
    <div class="card"><div class="card-head"><h3>Weekly time in HR zones</h3><span class="hint">minutes, zone 1–5</span></div><div class="chart-box"><canvas id="cZones"></canvas></div></div>
  </div>
  <div class="card mt"><div class="card-head"><h3>Workouts · last 60 days</h3><span class="hint">zones by % of max HR: 50/60/70/80/90</span></div>
    ${ws.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Date</th><th>Activity</th><th class="r">Duration</th><th class="r">Avg HR</th><th class="r">Max HR</th><th class="r">Load</th><th>Zones</th></tr></thead><tbody>
    ${ws.map((w) => { const tot = w.zones_min.slice(1).reduce((a, b) => a + b, 0) || 1; return `<tr><td>${shortDate(new Date(w.start * 1000).toISOString().slice(0, 10))} <span class="muted">${timeOf(w.start)}</span></td><td>${WORKOUT_NAMES[w.category] || 'Workout'}</td><td class="r">${hm(w.end - w.start)}</td><td class="r">${fmt(w.hr_avg)}</td><td class="r">${fmt(w.hr_max)}</td><td class="r"><b>${fmt(w.trimp)}</b></td>
      <td><div class="stagebar" style="min-width:120px;height:10px;margin:0">${w.zones_min.slice(1).map((z, i) => `<i title="Zone ${i + 1}: ${fmt(z)} min" style="width:${(100 * z) / tot}%;background:${zcols[i + 1]}"></i>`).join('')}</div></td></tr>`; }).join('')}
    </tbody></table></div>` : '<p class="muted">No workouts recorded. Start a workout on the watch to get detailed HR tracking.</p>'}</div>
  <details class="card mt"><summary>How these numbers are computed</summary>
    <p><b>Training load</b> is Banister's TRIMP: every minute above 50% of max HR is weighted exponentially by heart-rate reserve, so hard minutes count far more than easy ones. <b>Acute load</b> is a 7-day exponentially weighted sum (fatigue), <b>chronic load</b> a 28-day one (fitness). Their <b>ratio</b> between 0.8 and 1.3 is the classic "sweet spot"; above 1.5 suggests overreaching.</p>
    <p><b>Recovery time</b> grows with load and shrinks faster after good sleep. <b>VO₂ max</b> uses Withings' cardio fitness value if your watch provides it, otherwise the Uth et al. formula 15.3 × HRmax / HRrest.</p></details>`;
  dateLine($('#cLoad'), dates, [lineDs('Acute (7d)', col('load_acute'), css('--orange')), lineDs('Chronic (28d)', col('load_chronic'), css('--blue'))], { legend: true, y: { min: 0 } });
  dateBars($('#cDaily'), dates.slice(-42), [{ label: 'Load', data: col('training_load').slice(-42), backgroundColor: css('--orange') }]);
  mkChart($('#cZones'), {
    type: 'bar',
    data: { labels: wk, datasets: [1, 2, 3, 4, 5].map((z) => ({ label: `Zone ${z}`, data: wk.map((k) => Math.round(weeks[k][z])), backgroundColor: zcols[z], borderColor: css('--surface'), borderWidth: { top: 2 }, maxBarThickness: 28 })) },
    options: { interaction: { mode: 'index', intersect: false }, scales: { x: { stacked: true, grid: { display: false }, ticks: { callback(v) { return shortDate(this.getLabelForValue(v)); } } }, y: yAxis({ stacked: true }) },
      plugins: { legend: { display: true, position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 10, color: css('--ink-2') } }, tooltip: { callbacks: { title: (i) => `Week of ${shortDate(i[0].label)}`, label: (i) => ` ${i.dataset.label}: ${i.parsed.y} min` } } } },
  });
}

// ------------------------------------------------------------------------------------
// Insights
// ------------------------------------------------------------------------------------
async function viewInsights(view, tagArg) {
  const ins = await api('/api/insights');
  if (!ins.journal_days) {
    view.innerHTML = `<div class="page-head"><h2>Insights</h2></div><div class="card empty"><h2>Start journaling</h2><p>Log your mood, energy and tags like <i>alcohol</i>, <i>headache</i> or <i>little sleep</i> with the + button. After a couple of weeks Pulse compares your sensor data on tagged vs. untagged days and shows what actually moves the needle.</p><button class="btn primary" onclick="openLog()">Log first entry</button></div>`;
    return;
  }
  const sel = decodeURIComponent(tagArg || '') || (ins.tags.find((t) => t.n >= 3) || ins.tags[0] || {}).tag;
  const per = ins.tag_effects.find((t) => t.tag === sel);
  const strengthLbl = { strong: 'Strong evidence', likely: 'Likely', possible: 'Possible' };
  const tagChip = (t) => `<button class="chip ${t.tag === sel ? 'on' : ''}" onclick="location.hash='#/insights/${encodeURIComponent(t.tag)}'">${tagEmoji(t.tag)} ${esc(t.tag)} <span class="muted">${t.n}</span></button>`;
  view.innerHTML = `
  <div class="page-head"><h2>Insights</h2><span class="muted small">${ins.journal_days} journal days · ${ins.period_days} days analysed</span></div>
  <div class="card"><div class="card-head"><h3>Top patterns</h3><span class="hint">tagged days vs. other logged days</span></div>
    ${ins.findings.length ? `<div class="grid" style="gap:12px">${ins.findings.slice(0, 10).map((f) => `
      <div class="finding"><div class="stripe" style="background:${f.good ? css('--good') : css('--critical')}"></div>
        <div><div>${f.good ? '▲' : '▼'} ${esc(f.sentence)}</div>
        <div class="meta">${f.good ? 'favourable' : 'unfavourable'} · ${strengthLbl[f.strength]} · adjusted p=${f.q < 0.001 ? '<0.001' : f.q.toFixed(3)} · 95% range ${fmt(f.ci_low, 1)} to ${fmt(f.ci_high, 1)}${f.unit && f.unit !== '/5' ? ' ' + f.unit : ''} · effect size d=${f.d}</div></div></div>`).join('')}</div>`
      : '<p class="muted">No clear patterns yet. Keep logging — a tag needs at least 3 occurrences, and patterns usually emerge after 3–6 weeks.</p>'}
    <p class="small muted mt">These are correlations from your own data, not proof of cause. With many comparisons some "possible" findings will be chance — trust patterns that stay strong as data grows.</p></div>

  <div class="card mt"><div class="card-head"><h3>Explore a tag</h3></div>
    <div class="chips">${ins.tags.map(tagChip).join('')}</div>
    ${per && per.effects.length ? `<div class="tbl-wrap mt"><table class="tbl"><thead><tr><th>Metric</th><th class="r">With</th><th class="r">Without</th><th class="r">Change</th><th style="min-width:140px">worse ← → better</th><th class="r">n</th></tr></thead><tbody>
      ${per.effects.map((e) => { const better = e.good; const mag = Math.min(50, Math.abs(e.pct || 0)); const sig = e.strength !== 'none';
        return `<tr><td>${esc(e.label)}</td><td class="r">${fmtUnit(e.with, e.unit)}</td><td class="r">${fmtUnit(e.without, e.unit)}</td><td class="r">${e.pct > 0 ? '+' : ''}${fmt(e.pct)}%</td>
        <td><div class="effbar"><i style="${better ? 'left:50%' : `right:50%`};width:${mag}%;background:${better ? css('--good') : css('--critical')};opacity:${sig ? 1 : 0.35}"></i></div></td><td class="r muted">${e.n_with}</td></tr>`; }).join('')}
    </tbody></table></div><p class="small muted">Faded bars are not statistically clear yet.</p>` : `<p class="muted mt">${per ? `'${esc(sel)}' was logged ${per.n}×. Needs at least 3 days to compare.` : 'Pick a tag.'}</p>`}
  </div>

  <div class="grid g2 mt">
    <div class="card"><div class="card-head"><h3>What goes with a good mood</h3><span class="hint">rank correlation</span></div>${driversTable(ins.mood_drivers.filter((x) => x.target === 'mood'))}</div>
    <div class="card"><div class="card-head"><h3>What goes with feeling energetic</h3></div>${driversTable(ins.mood_drivers.filter((x) => x.target === 'self_energy'))}</div>
  </div>
  <div class="card mt"><div class="card-head"><h3>Mood vs readiness</h3><span class="hint">your self-rating (1–5) next to the sensor score</span></div><div class="grid g2"><div class="chart-box"><canvas id="iMood"></canvas></div><div class="chart-box"><canvas id="iRead"></canvas></div></div></div>`;
  const ms = ins.mood_series.slice(-90);
  dateLine($('#iMood'), ms.map((x) => x.date), [lineDs('Mood', ms.map((x) => x.mood), css('--magenta'), { pointRadius: 2 })], { y: { min: 1, max: 5 }, tooltip: { afterBody: (i) => { const t = ms[i[0].dataIndex].tags; return t.length ? 'Tags: ' + t.join(', ') : ''; } } });
  dateLine($('#iRead'), ms.map((x) => x.date), [lineDs('Readiness', ms.map((x) => x.readiness), css('--aqua'), { pointRadius: 2 })], { y: { min: 0, max: 100 } });
}
function fmtUnit(v, u) { return u === 'h' ? fmt(v, 1) + ' h' : u === '%' ? fmt(v) + '%' : u === '/5' ? fmt(v, 1) : u ? fmt(v) + ' ' + u : fmt(v); }
function driversTable(rows) {
  if (!rows.length) return '<p class="muted">Needs at least 8 days with a rating.</p>';
  return `<table class="tbl"><tbody>${rows.slice(0, 8).map((r) => `<tr><td>${esc(r.label)}</td><td style="width:45%"><div class="effbar"><i style="${r.r >= 0 ? 'left:50%' : 'right:50%'};width:${Math.abs(r.r) * 50}%;background:${r.r >= 0 ? css('--blue') : css('--orange')};opacity:${(r.q ?? r.p) < 0.1 ? 1 : 0.35}"></i></div></td><td class="r num">${r.r > 0 ? '+' : ''}${r.r.toFixed(2)}</td></tr>`).join('')}</tbody></table><p class="small muted">Blue = goes up together, orange = opposite. Faded = not clear yet.</p>`;
}

// ------------------------------------------------------------------------------------
// Journal
// ------------------------------------------------------------------------------------
function tagEmoji(name) { return (STATE.tags.find((t) => t.name === name) || {}).emoji || ''; }
function entryHtml(e) {
  return `<div class="entry card" style="padding:12px">
    <div class="face" title="mood">${e.mood ? MOODS[e.mood - 1][0] : '📝'}</div>
    <div><div class="when">${new Date(e.ts * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}
      ${e.mood ? ` · mood ${MOODS[e.mood - 1][1]}` : ''}${e.energy ? ` · energy ${ENERGY[e.energy - 1][1]}` : ''}</div>
      ${e.tags.length ? `<div class="tagrow">${e.tags.map((t) => `<span class="badge">${tagEmoji(t)} ${esc(t)}</span>`).join('')}</div>` : ''}
      ${e.note ? `<div class="note">${esc(e.note)}</div>` : ''}</div>
    <button class="btn sm" onclick='openLog(${JSON.stringify(e).replace(/'/g, '&#39;')})'>Edit</button></div>`;
}
async function viewJournal(view) {
  const [list, tags] = await Promise.all([api('/api/journal?days=3650'), api('/api/tags')]);
  STATE.tags = tags;
  const byDay = {};
  list.forEach((e) => { const k = isoDate(new Date((e.ts - 4 * 3600) * 1000)); (byDay[k] = byDay[k] || []).push(e); });
  view.innerHTML = `
  <div class="page-head"><h2>Journal</h2><button class="btn primary" onclick="openLog()">+ New entry</button></div>
  <div class="grid g2" style="align-items:start">
    <div>${list.length ? Object.keys(byDay).sort().reverse().map((k) => `<div class="day-label">${prettyDate(k)}</div><div class="entries">${byDay[k].map(entryHtml).join('')}</div>`).join('') : '<div class="card empty"><h2>No entries yet</h2><p>Tap + any time you want to note how you feel.</p></div>'}</div>
    <div class="card"><div class="card-head"><h3>Your tags</h3><span class="hint">tap × to remove</span></div>
      <div class="chips">${tags.map((t) => `<span class="chip">${t.emoji} ${esc(t.name)} <span class="muted">${t.count}</span><a href="#" class="x" onclick="delTag(event, '${esc(t.name).replace(/'/g, "\\'")}')" aria-label="remove">×</a></span>`).join('')}</div>
      <form class="row mt" onsubmit="addTag(event)"><input id="newTagEmoji" placeholder="😀" style="width:64px" maxlength="8"><input id="newTag" placeholder="new tag, e.g. 'cold shower'" style="flex:1;min-width:140px" maxlength="40"><button class="btn">Add</button></form>
      <p class="small muted">Tags power the Insights page. Good tags are things that happen on some days but not others: alcohol, late caffeine, headache, sauna, stressful meeting, period, travel…</p></div>
  </div>`;
}
async function addTag(ev) { ev.preventDefault(); const n = $('#newTag').value.trim(); if (!n) return; STATE.tags = await api('/api/tags', { method: 'POST', body: { name: n, emoji: $('#newTagEmoji').value.trim() } }); route(); }
async function delTag(ev, n) { ev.preventDefault(); if (!confirm(`Remove tag '${n}' from quick picks? Past entries keep it.`)) return; STATE.tags = await api(`/api/tags/${encodeURIComponent(n)}`, { method: 'DELETE' }); route(); }

// ------------------------------------------------------------------------------------
// Quick log sheet
// ------------------------------------------------------------------------------------
async function openLog(entry = {}) {
  if (!STATE.tags.length) STATE.tags = await api('/api/tags');
  const e = { mood: null, energy: null, tags: [], note: '', ...entry };
  const sel = new Set(e.tags);
  const tsDefault = e.ts || Math.floor(Date.now() / 1000);
  const local = new Date(tsDefault * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const sheet = $('#sheet');
  const allTags = [...STATE.tags.map((t) => t.name), ...e.tags.filter((t) => !STATE.tags.some((x) => x.name === t))];
  sheet.innerHTML = `
    <div class="row"><h2 style="flex:1;margin:0">${e.id ? 'Edit entry' : 'How are you?'}</h2><button class="btn sm" onclick="closeLog()" aria-label="Close">✕</button></div>
    <div class="sec">Mood</div><div class="scale" id="lMood">${MOODS.map(([f, l], i) => `<button type="button" data-v="${i + 1}" class="${e.mood === i + 1 ? 'on' : ''}">${f}<small>${l}</small></button>`).join('')}</div>
    <div class="sec">Energy / status</div><div class="scale" id="lEnergy">${ENERGY.map(([f, l], i) => `<button type="button" data-v="${i + 1}" class="${e.energy === i + 1 ? 'on' : ''}">${f}<small>${l}</small></button>`).join('')}</div>
    <div class="sec">What's going on?</div>
    <div class="chips" id="lTags">${allTags.map((t) => `<button type="button" class="chip ${sel.has(t) ? 'on' : ''}" data-t="${esc(t)}">${tagEmoji(t)} ${esc(t)}</button>`).join('')}<button type="button" class="chip add" id="lAddTag">+ tag</button></div>
    <div class="sec">Note</div><textarea id="lNote" placeholder="Anything special? e.g. 2 beers at dinner, headache since noon…">${esc(e.note)}</textarea>
    <div class="row mt"><label class="field" style="flex:1">When<input type="datetime-local" id="lTs" value="${local}"></label></div>
    <div class="row mt">${e.id ? '<button class="btn danger" id="lDel">Delete</button>' : ''}<span class="spacer"></span><button class="btn" onclick="closeLog()">Cancel</button><button class="btn primary" id="lSave">Save</button></div>`;
  const pick = (id, key) => $$(`#${id} button`).forEach((b) => b.addEventListener('click', () => { const v = +b.dataset.v; e[key] = e[key] === v ? null : v; $$(`#${id} button`).forEach((x) => x.classList.toggle('on', +x.dataset.v === e[key])); }));
  pick('lMood', 'mood'); pick('lEnergy', 'energy');
  const bindChip = (b) => b.addEventListener('click', () => { const t = b.dataset.t; sel.has(t) ? sel.delete(t) : sel.add(t); b.classList.toggle('on'); });
  $$('#lTags .chip[data-t]').forEach(bindChip);
  $('#lAddTag').addEventListener('click', () => {
    const n = (prompt('New tag (e.g. "sauna", "travel", "period")') || '').trim().toLowerCase();
    if (!n) return; sel.add(n);
    const b = document.createElement('button'); b.type = 'button'; b.className = 'chip on'; b.dataset.t = n; b.textContent = n; bindChip(b);
    $('#lAddTag').before(b);
  });
  $('#lSave').addEventListener('click', async () => {
    const body = { mood: e.mood, energy: e.energy, tags: [...sel], note: $('#lNote').value, ts: Math.floor(new Date($('#lTs').value).getTime() / 1000) };
    if (!body.mood && !body.energy && !body.tags.length && !body.note.trim()) { toast('Pick at least something'); return; }
    try {
      if (e.id) await api(`/api/journal/${e.id}`, { method: 'PUT', body }); else await api('/api/journal', { method: 'POST', body });
      STATE.tags = await api('/api/tags');
      closeLog(); toast('Saved ✓'); route();
    } catch (err) { toast(err.message); }
  });
  if (e.id) $('#lDel').addEventListener('click', async () => { if (!confirm('Delete this entry?')) return; await api(`/api/journal/${e.id}`, { method: 'DELETE' }); closeLog(); toast('Deleted'); route(); });
  sheet.hidden = false; $('#sheetBackdrop').hidden = false;
}
function closeLog() { $('#sheet').hidden = true; $('#sheetBackdrop').hidden = true; if (location.pathname === '/log') history.replaceState(null, '', '/#/today'); }

// ------------------------------------------------------------------------------------
// Settings
// ------------------------------------------------------------------------------------
async function viewSettings(view) {
  await refreshStatus();
  const s = STATE.status; const p = await api('/api/profile');
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  view.innerHTML = `
  <div class="page-head"><h2>Settings</h2></div>
  ${params.get('connected') ? '<div class="notice" style="border-left-color:var(--good)">Withings connected. Initial sync is running in the background — this can take a few minutes for 90 days of history.</div><br>' : ''}
  ${params.get('error') ? `<div class="notice" style="border-left-color:var(--critical)">Withings authorisation failed: ${esc(params.get('error'))}</div><br>` : ''}
  <div class="grid g2" style="align-items:start">
    <div class="card"><div class="card-head"><h3>Withings</h3>${badge(s.withings_connected ? 'connected' : 'not connected', s.withings_connected ? 'good' : 'neutral')}</div>
      ${!s.withings_configured ? `<p>Set <code>WITHINGS_CLIENT_ID</code> and <code>WITHINGS_CLIENT_SECRET</code> in your <code>.env</code> (create a free app at developer.withings.com) and restart. Use this callback URL:</p><p><code>${esc(s.redirect_uri)}</code></p>` : ''}
      <dl class="kv"><dt>Last sync</dt><dd>${esc(s.sync.last_sync || 'never')} ${s.sync.running ? '<span class="badge">running…</span>' : ''}</dd>
      <dt>Data</dt><dd>${s.data.days ? `${s.data.days} days (${s.data.first} → ${s.data.last})` : 'none'}</dd>
      <dt>Push updates</dt><dd>${s.notify_enabled ? 'enabled (Withings notifies on new data)' : 'polling every few minutes'}</dd>
      ${s.sync.last_error ? `<dt>Last error</dt><dd style="color:var(--critical)">${esc(s.sync.last_error)}</dd>` : ''}</dl>
      <div class="row mt">
        ${s.withings_configured && !s.withings_connected ? '<a class="btn primary" href="/api/withings/connect">Connect Withings</a>' : ''}
        ${s.withings_connected ? '<button class="btn primary" id="bSync">Sync now</button><button class="btn" id="bFull">Full re-sync</button><button class="btn danger" id="bDisc">Disconnect</button>' : ''}
      </div>
      ${!s.withings_connected ? `<hr style="border:0;border-top:1px solid var(--grid);margin:16px 0"><p class="small">No watch yet? Explore with ~5 months of realistic demo data (incl. journal entries with hidden patterns).</p>
        <div class="row">${s.demo ? '<button class="btn danger" id="bDemoClear">Remove demo data</button>' : '<button class="btn" id="bDemo">Load demo data</button>'}</div>` : ''}
    </div>
    <form class="card" id="profileForm"><div class="card-head"><h3>Profile</h3><span class="hint">used for HR zones, load & VO₂ max</span></div>
      <div class="grid g2">
        <label class="field">Birth year<input type="number" name="birth_year" value="${p.birth_year}" min="1900" max="2030"></label>
        <label class="field">Sex (for TRIMP)<select name="sex"><option value="m" ${p.sex === 'm' ? 'selected' : ''}>male</option><option value="f" ${p.sex === 'f' ? 'selected' : ''}>female</option></select></label>
        <label class="field">Max HR (blank = auto)<input type="number" name="max_hr" value="${p.max_hr ?? ''}" placeholder="auto: ${s.hrmax || ''}" min="100" max="240"></label>
        <label class="field">Sleep target (h)<input type="number" step="0.25" name="sleep_target_h" value="${p.sleep_target_h}" min="4" max="12"></label>
        <label class="field">Daily step goal<input type="number" name="step_goal" value="${p.step_goal}" min="0"></label>
        <label class="field">Time zone<input name="tz" value="${esc(p.tz || '')}" placeholder="${esc(p.tz_effective)}"></label>
      </div>
      <div class="row mt"><span class="spacer"></span><button class="btn primary">Save & recompute</button></div></form>
    <div class="card"><div class="card-head"><h3>Your data</h3></div>
      <p class="small">Everything lives in one SQLite file on your server (<code>data/health.db</code>). Back it up and you have everything.</p>
      <div class="row"><a class="btn" href="/api/export/daily.csv">Export daily metrics (CSV)</a><a class="btn" href="/api/export/journal.csv">Export journal (CSV)</a></div></div>
    <div class="card"><div class="card-head"><h3>App</h3></div>
      <div class="row"><span>Theme</span><div class="seg" id="themeSeg">${['auto', 'light', 'dark'].map((t) => `<button type="button" data-t="${t}" class="${(localStorage.getItem('theme') || 'auto') === t ? 'on' : ''}">${t}</button>`).join('')}</div></div>
      <p class="small muted">Tip: on your phone, use "Add to home screen". Long-press the icon for a direct "Log how I feel" shortcut.</p>
      ${s.auth ? '<button class="btn" id="bLogout">Log out</button>' : '<p class="small muted">No password set (APP_PASSWORD). Only run like this on a trusted network.</p>'}</div>
  </div>`;
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
  on('#bSync', async () => { await api('/api/sync', { method: 'POST' }); toast('Sync started'); setTimeout(route, 1500); });
  on('#bFull', async () => { await api('/api/sync?full=true', { method: 'POST' }); toast('Full sync started'); setTimeout(route, 1500); });
  on('#bDisc', async () => { if (confirm('Disconnect Withings? Your stored data is kept.')) { await api('/api/withings/disconnect', { method: 'POST' }); route(); } });
  on('#bDemo', async (ev) => { ev.target.disabled = true; ev.target.textContent = 'Generating…'; await api('/api/demo', { method: 'POST' }); await refreshStatus(); toast('Demo data loaded'); location.hash = '#/today'; });
  on('#bDemoClear', async () => { if (confirm('Remove all demo data and demo journal entries?')) { await api('/api/demo', { method: 'DELETE' }); await refreshStatus(); route(); } });
  on('#bLogout', async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; });
  $$('#themeSeg button').forEach((b) => b.addEventListener('click', () => { setTheme(b.dataset.t); route(); }));
  $('#profileForm').addEventListener('submit', async (ev) => {
    ev.preventDefault(); const f = new FormData(ev.target);
    const body = { birth_year: +f.get('birth_year'), sex: f.get('sex'), max_hr: f.get('max_hr') ? +f.get('max_hr') : null, sleep_target_h: +f.get('sleep_target_h'), step_goal: +f.get('step_goal'), tz: f.get('tz') || null };
    try { await api('/api/profile', { method: 'PUT', body }); await refreshStatus(); toast('Saved — metrics recomputed'); } catch (e) { toast(e.message); }
  });
  if (s.sync.running) setTimeout(() => { if (location.hash.startsWith('#/settings')) route(); }, 4000);
}
function setTheme(t) {
  try { localStorage.setItem('theme', t); } catch (_) { /* ignore */ }
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
}

// ------------------------------------------------------------------------------------
// boot
// ------------------------------------------------------------------------------------
(async function boot() {
  try { const t = localStorage.getItem('theme'); if (t && t !== 'auto') document.documentElement.dataset.theme = t; } catch (_) { /* ignore */ }
  $('#fab').addEventListener('click', () => openLog());
  $('#sheetBackdrop').addEventListener('click', closeLog);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLog(); });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', route);
  window.addEventListener('hashchange', route);
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  try { await refreshStatus(); STATE.tags = await api('/api/tags'); } catch (e) { console.error(e); }
  await route();
  if (location.pathname === '/log') openLog();
  setInterval(async () => { if (document.visibilityState === 'visible' && location.hash.match(/^#?\/?(today)?$/)) { await refreshStatus(); route(); } }, 5 * 60 * 1000);
})();
