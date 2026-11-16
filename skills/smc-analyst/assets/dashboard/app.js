/* SMC Analyst dashboard - UI logic (vanilla JS, no build step). */
(function () {
  'use strict';
  const LWC = window.LightweightCharts;
  const { TimeMapper, OverlayPrimitive } = window.SMCOverlays;
  const TF_SECONDS = { '1M': 2592000, '1W': 604800, '1D': 86400, '4H': 14400, '1H': 3600, '15m': 900, '5m': 300, '1m': 60 };

  const LAYERS = [
    ['structure', 'BOS / CHoCH (swing)', 'bull', true],
    ['internal', 'Internal structure', 'muted', false],
    ['ob', 'Order blocks', 'bull', true],
    ['fvg', 'Fair value gaps', 'info', true],
    ['sd', 'Supply / demand', 'warn', false],
    ['liquidity', 'Liquidity (BSL/SSL, EQH/EQL, PDH...)', 'warn', true],
    ['sweeps', 'Sweeps (x) + inducement', 'bear', true],
    ['pd', 'Premium / discount / OTE', 'muted', true],
    ['swings', 'Swing labels (HH/HL...)', 'muted', false],
    ['htf', 'HTF zones on this timeframe (H)', 'info', true],
    ['position', 'Position tool (P)', 'bull', true],
    ['ghost', 'Opposite scenario (ghost)', 'muted', true],
  ];

  const S = {
    meta: null, tf: null, chart: null, series: null, bottom: null, top: null, mapper: new TimeMapper(),
    analysis: null, candles: [], layers: {}, theme: 'dark', ws: null, wsDelay: 1000, lastPrice: null,
    prevClose: null, reloadTimer: null,
  };

  // ------------------------------------------------------------------ utilities
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem('smc.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('smc.' + k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } },
  };
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const alpha = (hex, a) => {
    const h = hex.replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };
  const toEpoch = (iso) => (iso ? Math.floor(Date.parse(iso) / 1000) : null);
  function fmt(p) {
    if (p === null || p === undefined || Number.isNaN(p)) return '-';
    const d = S.meta ? S.meta.instrument.price_precision : 2;
    return Number(p).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch (e) { /* not json */ }
      const err = new Error(msg); err.status = r.status; throw err;
    }
    return r.json();
  }

  // ------------------------------------------------------------------ theme + chart
  function palette() {
    return {
      bull: cssVar('--bull'), bear: cssVar('--bear'), info: cssVar('--info'), warn: cssVar('--warn'),
      text: cssVar('--text'), muted: cssVar('--muted'), border: cssVar('--border'), bg: cssVar('--bg'),
      surface: cssVar('--surface'), accent: cssVar('--accent'),
    };
  }

  function applyTheme(theme) {
    S.theme = theme;
    document.documentElement.dataset.theme = theme;
    store.set('theme', theme);
    if (!S.chart) return;
    const c = palette();
    S.chart.applyOptions({
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.muted },
      grid: { vertLines: { color: alpha(c.border, 0.35) }, horzLines: { color: alpha(c.border, 0.35) } },
      rightPriceScale: { borderColor: c.border }, timeScale: { borderColor: c.border },
    });
    S.series.applyOptions({ upColor: c.bull, downColor: c.bear, wickUpColor: c.bull, wickDownColor: c.bear, borderVisible: false });
    renderOverlays();
  }

  function createChart() {
    S.chart = LWC.createChart($('chart'), {
      autoSize: true,
      crosshair: { mode: 0 },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 18 },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.08 } },
    });
    S.series = S.chart.addSeries(LWC.CandlestickSeries, {});
    S.bottom = new OverlayPrimitive('bottom');
    S.top = new OverlayPrimitive('top');
    S.bottom.setMapper(S.mapper); S.top.setMapper(S.mapper);
    S.series.attachPrimitive(S.bottom);
    S.series.attachPrimitive(S.top);
    applyTheme(S.theme);
  }

  // ------------------------------------------------------------------ overlays
  function zoneColor(z, c) {
    if (z.kind === 'fvg') return z.dir > 0 ? c.info : c.warn;
    return z.dir > 0 ? c.bull : c.bear;
  }

  function buildShapes() {
    const a = S.analysis && S.analysis.overlays;
    const rects = [], segments = [], glyphs = [], texts = [];
    const bottom = { rects: [], segments: [], glyphs: [], texts: [] };
    if (!a) return { bottom, top: { rects, segments, glyphs, texts } };
    const c = palette(), L = S.layers;

    if (L.pd && a.dealing_range) {
      const d = a.dealing_range;
      bottom.rects.push({ t1: d.from, t2: null, p1: d.eq, p2: d.high, fill: alpha(c.bear, 0.045) });
      bottom.rects.push({ t1: d.from, t2: null, p1: d.low, p2: d.eq, fill: alpha(c.bull, 0.045) });
      bottom.rects.push({ t1: d.from, t2: null, p1: d.ote_bottom, p2: d.ote_top, fill: alpha(c.accent, 0.08), label: 'OTE 62-79%', labelColor: c.accent, labelAlign: 'right' });
      bottom.segments.push({ t1: d.from, t2: null, p: d.eq, color: alpha(c.muted, 0.8), dash: 'dash', label: 'EQ', labelPos: 'end' });
      bottom.segments.push({ t1: d.from, t2: null, p: d.high, color: alpha(c.bear, 0.5), dash: 'dot', label: 'Premium', labelPos: 'end' });
      bottom.segments.push({ t1: d.from, t2: null, p: d.low, color: alpha(c.bull, 0.5), dash: 'dot', label: 'Discount', labelPos: 'end' });
    }

    const zoneOn = (k) => (k === 'order_block' ? L.ob : k === 'fvg' ? L.fvg : L.sd);
    for (const z of a.zones) {
      if (!zoneOn(z.kind)) continue;
      const col = zoneColor(z, c);
      const name = z.kind === 'order_block' ? (z.dir > 0 ? 'Bull OB' : 'Bear OB') : z.kind === 'fvg' ? (z.dir > 0 ? 'Bull FVG' : 'Bear FVG') : (z.kind === 'demand' ? 'Demand' : 'Supply');
      bottom.rects.push({ t1: z.from, t2: z.to, p1: z.bottom, p2: z.top, fill: alpha(col, z.kind === 'fvg' ? 0.14 : 0.18),
        stroke: alpha(col, 0.6), dash: z.kind === 'demand' || z.kind === 'supply' ? 'dash' : null,
        label: z.state === 'mitigated' ? null : name, labelColor: col });
    }
    if (L.htf) {
      for (const z of a.htf_zones) {
        const col = zoneColor(z, c);
        bottom.rects.push({ t1: z.from, t2: z.to, p1: z.bottom, p2: z.top, fill: alpha(col, 0.07), stroke: alpha(col, 0.9),
          dash: 'dot', lineWidth: 1.5, label: z.label, labelColor: col, labelAlign: 'right' });
      }
    }

    if (L.structure || L.internal) {
      for (const b of a.breaks) {
        const internal = b.structure === 'internal';
        if ((internal && !L.internal) || (!internal && !L.structure)) continue;
        const col = b.dir > 0 ? c.bull : c.bear;
        segments.push({ t1: b.from, t2: b.to, p: b.level, color: internal ? alpha(col, 0.7) : col,
          width: internal ? 1 : (b.kind === 'CHoCH' ? 2 : 1.5), dash: internal ? 'dash' : null,
          label: (internal ? 'i-' : '') + b.kind, labelColor: col });
      }
    }
    if (L.liquidity) {
      for (const lv of a.liquidity) {
        if (lv.status === 'taken' && lv.kind.startsWith('swing')) continue; // consumed pivots add noise
        const col = lv.side === 'buy' ? c.warn : c.info;
        const strong = lv.kind !== 'swing_high' && lv.kind !== 'swing_low';
        segments.push({ t1: lv.from, t2: lv.to, p: lv.price, color: alpha(col, lv.status === 'resting' ? 0.85 : 0.4),
          width: strong ? 1.5 : 1, dash: 'dash', label: strong || lv.status === 'resting' ? lv.label : null,
          labelPos: lv.to ? 'mid' : 'end', labelColor: col });
      }
    }
    if (L.sweeps) {
      for (const s of a.sweeps) glyphs.push({ t: s.time, p: s.price, kind: 'x', color: c.bear, size: 4.5 });
      const ind = a.inducement;
      if (ind) {
        glyphs.push({ t: ind.time, p: ind.price, kind: 'diamond', color: c.accent, size: 5,
          label: ind.swept ? 'IDM (swept)' : 'IDM', above: true });
        segments.push({ t1: ind.time, t2: ind.swept_time, p: ind.price, color: alpha(c.accent, 0.8), dash: 'dot' });
      }
    }
    if (L.swings) {
      for (const s of a.swings) {
        if (s.level !== 'swing' || !s.label) continue;
        texts.push({ t: s.time, p: s.price, text: s.label, color: c.muted, above: s.kind === 'high' });
      }
    }

    const plan = S.analysis.plan;
    if (plan && plan.entry != null) {
      const lastT = S.candles.length ? S.candles[S.candles.length - 1].time : null;
      const t0 = Math.max(toEpoch(plan.entry_zone && plan.entry_zone.created_time) || lastT, S.candles.length ? S.candles[Math.max(0, S.candles.length - 40)].time : 0);
      if (L.position) {
        const long = plan.direction > 0;
        rects.push({ t1: t0, t2: null, p1: plan.entry, p2: plan.stop_loss, fill: alpha(c.bear, 0.2), stroke: alpha(c.bear, 0.8),
          label: `SL ${fmt(plan.stop_loss)}`, labelColor: c.bear, labelBg: alpha(c.surface, 0.9) });
        let prev = plan.entry;
        plan.targets.forEach((t, i) => {
          rects.push({ t1: t0, t2: null, p1: prev, p2: t.price, fill: alpha(c.bull, 0.2 - i * 0.05), stroke: alpha(c.bull, 0.8),
            label: `${t.name} ${fmt(t.price)} (${t.rr.toFixed(2)}R)`, labelColor: c.bull, labelBg: alpha(c.surface, 0.9) });
          prev = t.price;
        });
        segments.push({ t1: t0, t2: null, p: plan.entry, color: c.text, width: 1.5,
          label: `${long ? 'LONG' : 'SHORT'} entry ${fmt(plan.entry)}${plan.status === 'valid' ? '' : ' (not valid)'}`, labelPos: 'start', labelColor: c.text, labelBg: alpha(c.surface, 0.9) });
      }
      const o = plan.opposite;
      if (L.ghost && o && o.entry != null && lastT) {
        const g = alpha(c.muted, 0.9);
        rects.push({ t1: lastT, t2: null, p1: o.entry, p2: o.stop_loss, fill: alpha(c.muted, 0.06), stroke: g, dash: 'dash' });
        if (o.target != null) rects.push({ t1: lastT, t2: null, p1: o.entry, p2: o.target, fill: alpha(c.muted, 0.04), stroke: g, dash: 'dash',
          label: `ghost ${o.side.toUpperCase()} if invalidated (${o.rr ?? '-'}R)`, labelColor: c.muted, labelAlign: 'right', labelBg: alpha(c.surface, 0.9) });
        segments.push({ t1: lastT, t2: null, p: o.trigger_level, color: g, dash: 'dot', label: 'flip trigger', labelPos: 'end' });
      }
    }
    return { bottom, top: { rects, segments, glyphs, texts } };
  }

  function renderOverlays() {
    if (!S.bottom) return;
    const { bottom, top } = buildShapes();
    S.bottom.setShapes(bottom);
    S.top.setShapes(top);
    renderLegend();
  }

  function renderLegend() {
    const a = S.analysis && S.analysis.overlays;
    if (!a) { $('legend').innerHTML = ''; return; }
    const d = a.dealing_range;
    $('legend').innerHTML = `<span><b>${esc(S.tf)}</b></span>` +
      `<span>swing <b class="${a.trend === 'bullish' ? 'up' : a.trend === 'bearish' ? 'down' : ''}">${esc(a.trend)}</b></span>` +
      `<span>internal <b>${esc(a.internal_trend)}</b></span>` +
      (d ? `<span>price in <b>${esc(d.price_zone)}</b></span>` : '') +
      `<span>${a.zones.length} zones, ${a.htf_zones.length} HTF</span>`;
  }

  // ------------------------------------------------------------------ side panel
  function renderPanel() {
    const cas = S.analysis && S.analysis.cascade, plan = S.analysis && S.analysis.plan;
    if (!cas) return;
    const badge = $('status-badge');
    badge.textContent = cas.status.replace('_', ' ');
    badge.className = 'badge ' + cas.status;

    $('setup-summary').innerHTML =
      `<div class="big"><span class="dot ${esc(cas.bias_name)}"></span>${esc(cas.bias_name)} bias` +
      `${cas.bias_source ? ` <span class="sub">(${esc(cas.bias_source)})</span>` : ''} &middot; ${esc(cas.status.replace('_', ' '))}</div>` +
      (cas.waiting_for ? `<div>Waiting for ${esc(cas.waiting_for)}.</div>` : '') +
      (cas.warnings && cas.warnings.length ? `<div class="note">${cas.warnings.map(esc).join('<br>')}</div>` : '');
    const score = (cas.confluence && cas.confluence.score) || 0;
    $('confluence-bar').style.width = score + '%';
    $('confluence-score').textContent = score + '/100';
    $('confluence-table').innerHTML = ((cas.confluence && cas.confluence.breakdown) || []).map((b) =>
      `<tr><td>${esc(b.factor.replace('_', ' '))}</td><td>${b.points}/${b.max}</td><td class="sub">${esc(b.note)}</td></tr>`).join('');

    // trade plan
    let html = '';
    if (!plan || plan.entry == null) {
      html = `<div class="note">${esc(plan ? plan.reason : 'No plan yet.')}</div>`;
    } else {
      html += `<div style="margin-bottom:6px"><span class="pill ${esc(plan.side)}">${esc((plan.side || '').toUpperCase())}</span> ` +
        `<b>${esc(plan.status.replace('_', ' '))}</b> <span class="sub">&middot; ${esc(plan.entry_type)} entry</span></div>`;
      html += '<div class="kv">';
      html += `<span class="k">Entry</span><span class="v">${fmt(plan.entry)}</span><span class="n">${esc(plan.entry_zone ? plan.entry_zone.timeframe + ' ' + plan.entry_zone.kind.replace('_', ' ') : '')}</span>`;
      html += `<span class="k">Stop</span><span class="v down">${fmt(plan.stop_loss)}</span><span class="n">inval ${fmt(plan.invalidation_level)}</span>`;
      for (const t of plan.targets) {
        html += `<span class="k">${esc(t.name)}${t.name === plan.primary_target ? ' *' : ''}</span><span class="v up">${fmt(t.price)}</span><span class="n">${t.rr.toFixed(2)}R &middot; ${esc(t.source)}</span>`;
      }
      html += '</div>';
      if (plan.position) {
        const p = plan.position;
        html += `<div class="note">Size: <b>${Number(p.units).toLocaleString()} ${esc(p.unit_name)}</b>${p.lots != null ? ` = ${p.lots} ${esc(p.lot_name)}` : ''}` +
          ` &middot; risk ${p.risk_amount} ${esc(p.account_currency)} (${p.risk_pct}%)</div>`;
        html += (p.notes || []).map((n) => `<div class="note">${esc(n)}</div>`).join('');
      }
      html += `<div class="note">${esc(plan.reason)}</div>` + (plan.notes || []).map((n) => `<div class="note">${esc(n)}</div>`).join('');
    }
    $('plan').innerHTML = html;
    $('btn-track').hidden = !(plan && plan.status === 'valid');

    const o = plan && plan.opposite;
    $('opposite').innerHTML = o && o.entry != null
      ? `<div><span class="pill ${esc(o.side)}">${esc(o.side.toUpperCase())}</span> if ${esc(o.trigger_rule)}</div>` +
        `<div class="kv" style="margin-top:6px"><span class="k">Entry</span><span class="v">${fmt(o.entry)}</span><span class="n"></span>` +
        `<span class="k">Stop</span><span class="v">${fmt(o.stop_loss)}</span><span class="n"></span>` +
        `<span class="k">Target</span><span class="v">${fmt(o.target)}</span><span class="n">${o.rr ?? '-'}R${o.valid ? '' : ' (info only)'}</span></div>` +
        `<div class="note">${esc(o.note)}</div>`
      : '<div class="note">Available once a trade plan exists.</div>';
    const h = plan && plan.hedge;
    $('hedge').innerHTML = !h ? '' : h.status === 'suggested'
      ? `<div style="margin-top:10px"><b>Hedge:</b> <span class="pill ${h.direction === 'long' ? 'long' : 'short'}">${esc(h.direction.toUpperCase())}</span> <b>${esc(h.instrument)}</b> ` +
        `&times;${h.ratio} notional${h.hedge_units != null ? ` (${Number(h.hedge_units).toLocaleString()} units)` : ''}</div><div class="note">${esc(h.reason)}</div>`
      : `<div class="note" style="margin-top:10px"><b>Hedge ${esc(h.status)}:</b> ${esc(h.reason)}</div>`;

    // stages
    $('stages').innerHTML = cas.stages.map((s) =>
      `<div class="stage ${esc(s.status)}"><div class="title"><span>${s.stage}. ${esc(s.name)}</span><span class="sub">${esc(s.timeframes.join(', '))} &middot; ${esc(s.status)}</span></div>` +
      `<ul>${s.rationale.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>`).join('');

    // alignment
    const rows = Object.entries(cas.alignment || {});
    $('alignment').innerHTML = '<tr><th>TF</th><th>Swing</th><th>Internal</th><th>Last break</th><th>P/D</th></tr>' +
      rows.map(([tf, v]) => `<tr class="${tf === S.tf ? 'current' : ''}"><td><b>${esc(tf)}</b></td>` +
        `<td><span class="dot ${esc(v.swing_trend)}"></span>${esc(v.swing_trend)}</td><td><span class="dot ${esc(v.internal_trend)}"></span>${esc(v.internal_trend)}</td>` +
        `<td>${esc(v.last_break || '-')}</td><td>${esc(v.pd_zone || '-')}</td></tr>`).join('');

    const m = S.analysis.market;
    const mb = $('market-badge');
    mb.textContent = m ? (m.state === 'closed' ? 'market closed' : m.state === 'open' ? 'market open' : 'hours unknown') : '-';
    mb.title = m && m.next_open ? `Next open ${m.next_open} UTC` : 'Market session';
  }

  async function loadJournal() {
    try {
      const j = await api('/api/journal');
      const st = j.stats;
      $('journal-stats').textContent = `${st.total} signals · ${st.open} open · ${st.wins}W/${st.losses}L · hit rate ` +
        `${st.hit_rate == null ? 'n/a' : Math.round(st.hit_rate * 100) + '%'} · avg ${st.avg_r == null ? 'n/a' : st.avg_r.toFixed(2) + 'R'} · total ${st.total_r.toFixed(2)}R`;
      $('journal').innerHTML = '<tr><th>Symbol</th><th>Side</th><th>Status</th><th>Entry</th><th>SL</th><th>R</th><th>Created</th></tr>' +
        (j.signals.length ? j.signals.map((s) => `<tr class="${s.symbol === j.symbol ? 'current' : ''}"><td>${esc(s.symbol)}</td>` +
          `<td><span class="pill ${esc(s.side)}">${esc(s.side)}</span></td><td>${esc(s.status.replace('_', ' '))}</td>` +
          `<td>${esc(s.entry)}</td><td>${esc(s.stop_loss)}</td><td>${s.r_multiple == null ? '-' : s.r_multiple.toFixed(2)}</td>` +
          `<td class="sub">${esc((s.created_at || '').slice(0, 16).replace('T', ' '))}</td></tr>`).join('')
          : '<tr><td colspan="7" class="sub">No tracked signals yet.</td></tr>');
    } catch (e) { $('journal-stats').textContent = 'Journal unavailable: ' + e.message; }
  }

  // ------------------------------------------------------------------ data loading
  async function loadCandles(fit) {
    const res = await api(`/api/candles?tf=${encodeURIComponent(S.tf)}`);
    S.candles = res.candles;
    const prec = res.precision;
    S.series.applyOptions({ priceFormat: { type: 'price', precision: prec, minMove: res.tick_size } });
    S.series.setData(S.candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    S.mapper.setTimes(S.candles.map((c) => c.time), TF_SECONDS[S.tf]);
    if (S.candles.length) {
      const last = S.candles[S.candles.length - 1];
      S.prevClose = S.candles.length > 1 ? S.candles[S.candles.length - 2].close : null;
      updatePrice(last.close);
    }
    $('data-note').textContent = res.data_note || '';
    if (fit) fitRecent();
  }

  async function loadAnalysis() {
    S.analysis = await api(`/api/analysis?tf=${encodeURIComponent(S.tf)}`);
    renderOverlays();
    renderPanel();
  }

  function fitRecent() {
    const n = S.candles.length;
    if (!n) return;
    S.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 18 });
  }

  function updatePrice(p) {
    S.lastPrice = p;
    $('price').textContent = fmt(p);
    const ch = $('change');
    if (S.prevClose) {
      const d = p - S.prevClose, pct = (d / S.prevClose) * 100;
      ch.textContent = `${d >= 0 ? '+' : ''}${fmt(d)} (${pct.toFixed(2)}%) vs prev ${S.tf} close`;
      ch.className = 'sub ' + (d >= 0 ? 'up' : 'down');
    }
    document.title = `${fmt(p)} · ${S.meta ? S.meta.symbol : ''} · SMC Analyst`;
  }

  async function setTF(tf, fit = true) {
    S.tf = tf;
    store.set('tf.' + S.meta.symbol, tf);
    document.querySelectorAll('#tf-switch button').forEach((b) => b.classList.toggle('active', b.dataset.tf === tf));
    try {
      await loadCandles(fit);
      await loadAnalysis();
    } catch (e) { showLoading(`Could not load ${tf}: ${e.message}`); return; }
    hideLoading();
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ subscribe: tf }));
  }

  // ------------------------------------------------------------------ live stream
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/stream?tf=${encodeURIComponent(S.tf)}`);
    S.ws = ws;
    ws.onopen = () => { S.wsDelay = 1000; setFeed(...feedLabel()); };
    ws.onclose = () => {
      setFeed('reconnecting', 'degraded');
      setTimeout(connectWS, S.wsDelay);
      S.wsDelay = Math.min(S.wsDelay * 2, 15000);
    };
    ws.onmessage = (m) => {
      let msg; try { msg = JSON.parse(m.data); } catch (e) { return; }
      if (msg.type === 'candle' && msg.tf === S.tf) onCandle(msg);
      else if (msg.type === 'status') onStatus(msg);
      else if (msg.type === 'analysis_updated') scheduleReload(msg.reload);
      else if (msg.type === 'journal') loadJournal();
      else if (msg.type === 'error') showLoading(msg.message);
    };
    clearInterval(S.ping);
    S.ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"type":"ping"}'); }, 25000);
  }

  function onCandle(c) {
    const bar = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
    const last = S.candles[S.candles.length - 1];
    if (last && c.time < last.time) return;
    if (last && c.time === last.time) S.candles[S.candles.length - 1] = { ...bar, volume: c.volume };
    else { if (last) S.prevClose = last.close; S.candles.push({ ...bar, volume: c.volume }); S.mapper.push(c.time); }
    S.series.update(bar);
    updatePrice(c.close);
    setFeed(...feedLabel());
  }

  function onStatus(s) {
    const map = { connected: ['live', 'live'], reconnecting: ['reconnecting', 'degraded'], fallback: ['polling (fallback)', 'degraded'],
      degraded: ['degraded', 'degraded'], market_closed: ['market closed', 'muted'], market_open: ['live', 'live'], loading: ['refreshing', 'muted'] };
    const [text, cls] = map[s.state] || [s.state, 'muted'];
    setFeed(text, cls, s.message);
  }

  function feedLabel() {
    const m = S.analysis && S.analysis.market;
    if (m && m.state === 'closed') return ['feed idle', 'muted', m.next_open ? `Market closed; next open ${m.next_open} UTC` : 'Market closed'];
    return S.meta && S.meta.instrument.realtime ? ['live', 'live', 'Real-time stream'] : ['polled', 'live', S.meta ? S.meta.instrument.data_note : ''];
  }

  function setFeed(text, cls, title) {
    const b = $('feed-badge');
    b.textContent = text; b.className = 'badge ' + (cls === 'degraded' ? 'degraded' : cls === 'live' ? 'live' : 'muted');
    if (title) b.title = title;
  }

  function scheduleReload(withCandles) {
    clearTimeout(S.reloadTimer);
    S.reloadTimer = setTimeout(async () => {
      try { if (withCandles) await loadCandles(false); await loadAnalysis(); hideLoading(); } catch (e) { /* next update retries */ }
    }, 400);
  }

  // ------------------------------------------------------------------ chrome
  function showLoading(text) { $('loading').hidden = false; $('loading-text').textContent = text; }
  function hideLoading() { $('loading').hidden = true; }

  function buildLayers() {
    const saved = store.get('layers', {});
    const c = palette();
    const box = $('layers');
    box.innerHTML = '<h2>Layers</h2>';
    for (const [key, label, color, def] of LAYERS) {
      S.layers[key] = key in saved ? saved[key] : def;
      const row = document.createElement('label');
      row.innerHTML = `<input type="checkbox" ${S.layers[key] ? 'checked' : ''}> <span class="swatch" style="background:${c[color] || c.muted}"></span> ${esc(label)}`;
      row.querySelector('input').addEventListener('change', (e) => toggleLayer(key, e.target.checked));
      row.dataset.key = key;
      box.appendChild(row);
    }
  }

  function toggleLayer(key, on) {
    S.layers[key] = on === undefined ? !S.layers[key] : on;
    store.set('layers', S.layers);
    const input = document.querySelector(`#layers label[data-key="${key}"] input`);
    if (input) input.checked = S.layers[key];
    renderOverlays();
  }

  function buildTFSwitch() {
    const nav = $('tf-switch');
    nav.innerHTML = '';
    S.meta.timeframes.forEach((tf, i) => {
      const b = document.createElement('button');
      b.textContent = tf; b.dataset.tf = tf; b.title = `${tf} (${i + 1})`;
      b.addEventListener('click', () => setTF(tf));
      nav.appendChild(b);
    });
  }

  function bindKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.target.matches('input, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      const n = parseInt(e.key, 10);
      if (n >= 1 && n <= S.meta.timeframes.length) setTF(S.meta.timeframes[n - 1]);
      else if (k === 'h') toggleLayer('htf');
      else if (k === 'p') toggleLayer('position');
      else if (k === 'l') $('layers').hidden = !$('layers').hidden;
      else if (k === 'f') fitRecent();
      else if (k === 'r') refreshAll();
      else if (k === 't') applyTheme(S.theme === 'dark' ? 'light' : 'dark');
      else if (e.key === '?') $('help').hidden = !$('help').hidden;
      else if (e.key === 'Escape') { $('help').hidden = true; $('layers').hidden = true; }
    });
    $('btn-layers').onclick = () => { $('layers').hidden = !$('layers').hidden; };
    $('btn-theme').onclick = () => applyTheme(S.theme === 'dark' ? 'light' : 'dark');
    $('btn-refresh').onclick = refreshAll;
    $('btn-help').onclick = () => { $('help').hidden = false; };
    $('help-close').onclick = () => { $('help').hidden = true; };
    $('btn-track').onclick = async () => {
      try { await api('/api/track', { method: 'POST' }); await loadJournal(); $('btn-track').textContent = 'Tracking'; }
      catch (e) { $('btn-track').textContent = e.message; }
    };
  }

  async function refreshAll() {
    showLoading('Refreshing every timeframe...');
    try { await api('/api/refresh', { method: 'POST' }); } catch (e) { hideLoading(); }
  }

  async function waitReady() {
    for (;;) {
      try {
        const meta = await api('/api/meta');
        S.meta = meta;
        $('symbol').textContent = meta.symbol;
        $('instrument').textContent = `${meta.instrument.asset_class} · ${meta.instrument.provider} · ${meta.instrument.realtime ? 'real-time' : 'polled'}`;
        if (meta.status === 'ready') return meta;
        if (meta.status === 'error') { showLoading('Analysis failed: ' + meta.error); }
        else showLoading(`Running the top-down analysis for ${meta.symbol} (weekly → 1m)...`);
      } catch (e) { showLoading('Waiting for the dashboard server...'); }
      await new Promise((r) => setTimeout(r, 1200));
    }
  }

  async function main() {
    // Deep links: ?tf=4H&theme=light override the remembered view.
    const params = new URLSearchParams(location.search);
    const urlTheme = params.get('theme');
    S.theme = urlTheme === 'light' || urlTheme === 'dark' ? urlTheme
      : store.get('theme', window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.dataset.theme = S.theme;
    buildLayers();
    bindKeys();
    createChart();
    const meta = await waitReady();
    buildTFSwitch();
    // Accept lowercase deep links (?tf=4h, 1d, 1w). Minutes stay lowercase: 1m = minute, 1M = month.
    const rawTf = params.get('tf');
    const linkTf = rawTf && /^\d+[hdw]$/i.test(rawTf) ? rawTf.toUpperCase() : rawTf;
    const wanted = linkTf || store.get('tf.' + meta.symbol, null);
    await setTF(meta.timeframes.includes(wanted) ? wanted : meta.default_tf);
    connectWS();
    loadJournal();
  }

  main();
})();
