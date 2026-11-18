/* SMC Analyst - drawing primitives for TradingView Lightweight Charts v5.
 *
 * One generic series primitive draws four kinds of shapes:
 *   rect    {t1, t2|null, p1, p2, fill, stroke, dash, lineWidth, label, labelColor, labelAlign}
 *   segment {t1, t2|null, p, color, width, dash, label, labelColor, labelPos: 'start'|'mid'|'end'}
 *   glyph   {t, p, kind: 'x'|'diamond'|'dot', color, size, label, labelColor, above}
 *   text    {t, p, text, color, above}
 *   position {t1, tNow, tEnd, entry, stop, targets: [{price, tag}], nowPrice, entryTag, stopTag,
 *             profit, loss, line, tagText, dim}   TradingView-style long/short position tool
 * t2 === null means "extend to the right edge of the chart" (live / unmitigated).
 *
 * Times are UTC epoch seconds. TimeMapper converts any time into a fractional *logical*
 * bar index, so shapes that start before the loaded data (e.g. a weekly OB projected onto
 * a 1m chart) or between bars are still placed correctly.
 */
(function (global) {
  'use strict';

  class TimeMapper {
    constructor() { this.times = []; this.bar = 60; }
    setTimes(times, barSeconds) {
      this.times = times;
      this.bar = barSeconds || (times.length > 1 ? times[times.length - 1] - times[times.length - 2] : 60);
    }
    push(t) { if (!this.times.length || t > this.times[this.times.length - 1]) this.times.push(t); }
    logical(t) {
      const a = this.times, n = a.length;
      if (!n) return null;
      if (t <= a[0]) return (t - a[0]) / this.bar;
      if (t >= a[n - 1]) return (n - 1) + (t - a[n - 1]) / this.bar;
      let lo = 0, hi = n - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (a[mid] <= t) lo = mid; else hi = mid; }
      return lo + (t - a[lo]) / (a[hi] - a[lo]);
    }
  }

  function setDash(ctx, dash, r) {
    ctx.setLineDash(dash === 'dash' ? [6 * r, 4 * r] : dash === 'dot' ? [2 * r, 3 * r] : []);
  }

  function drawLabel(ctx, text, x, y, color, r, align, bg) {
    ctx.font = `${Math.round(10.5 * r)}px ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`;
    ctx.textAlign = align || 'left';
    ctx.textBaseline = 'middle';
    if (bg) {
      const w = ctx.measureText(text).width + 6 * r, h = 14 * r;
      const left = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
      ctx.fillStyle = bg;
      ctx.fillRect(left, y - h / 2, w, h);
      ctx.fillStyle = color;
      ctx.fillText(text, align === 'right' ? x - 3 * r : align === 'center' ? x : x + 3 * r, y);
      return;
    }
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  function pill(ctx, text, x, y, bg, fg, r, align) {
    ctx.font = `600 ${Math.round(10.5 * r)}px ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`;
    const w = ctx.measureText(text).width + 10 * r, h = 16 * r;
    const left = align === 'right' ? x - w : x;
    ctx.fillStyle = bg;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(left, y - h / 2, w, h, 3 * r); else ctx.rect(left, y - h / 2, w, h);
    ctx.fill();
    ctx.fillStyle = fg;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, left + 5 * r, y + 0.5 * r);
  }

  // Spread label y positions so no two pills overlap (keeps each as close to its line as possible).
  function spread(items, minGap) {
    const sorted = items.slice().sort((a, b) => a.y - b.y);
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i].y - sorted[i - 1].y < minGap) sorted[i].y = sorted[i - 1].y + minGap;
    }
    return items;
  }

  // TradingView-style position tool: profit zone (entry -> last target) and loss zone (entry -> stop),
  // darker over the elapsed bars (setup start -> now) and lighter into the future, a dashed path from
  // the entry to the current price, a line and price tag per target, and the entry/stop tags.
  function drawPosition(ctx, P, hr, vr) {
    const x1 = P.x1 * hr, xn = Math.max(P.x1, Math.min(P.xn, P.x2)) * hr, x2 = P.x2 * hr;
    if (x2 - x1 < 2) return;
    const yE = P.yEntry * vr, yS = P.yStop * vr, yF = P.yTargets[P.yTargets.length - 1] * vr;
    const k = P.dim ? 0.55 : 1;
    const zone = (yA, yB, strong, weak) => {
      const top = Math.min(yA, yB), h = Math.abs(yB - yA);
      ctx.fillStyle = strong; ctx.fillRect(x1, top, xn - x1, h);
      ctx.fillStyle = weak; ctx.fillRect(xn, top, x2 - xn, h);
    };
    zone(yE, yF, P.profit(0.30 * k), P.profit(0.15 * k));
    zone(yE, yS, P.loss(0.28 * k), P.loss(0.14 * k));

    // Outer edges (last target, stop) solid; intermediate targets dashed.
    ctx.lineWidth = Math.max(1, hr);
    P.yTargets.forEach((y, i) => {
      const last = i === P.yTargets.length - 1;
      ctx.strokeStyle = P.profit(last ? 0.85 * k : 0.7 * k);
      ctx.setLineDash(last ? [] : [4 * hr, 3 * hr]);
      ctx.beginPath(); ctx.moveTo(x1, Math.round(y * vr) + 0.5); ctx.lineTo(x2, Math.round(y * vr) + 0.5); ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.strokeStyle = P.loss(0.85 * k);
    ctx.beginPath(); ctx.moveTo(x1, Math.round(yS) + 0.5); ctx.lineTo(x2, Math.round(yS) + 0.5); ctx.stroke();

    // Entry line.
    ctx.strokeStyle = P.line; ctx.lineWidth = 1.5 * hr;
    ctx.beginPath(); ctx.moveTo(x1, Math.round(yE) + 0.5); ctx.lineTo(x2, Math.round(yE) + 0.5); ctx.stroke();

    // Path from the entry to the current price.
    if (P.yNow !== null && xn > x1 + 2) {
      ctx.strokeStyle = P.path; ctx.lineWidth = 1.2 * hr; ctx.setLineDash([5 * hr, 4 * hr]);
      ctx.beginPath(); ctx.moveTo(x1, yE); ctx.lineTo(xn, P.yNow * vr); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = P.path;
      ctx.beginPath(); ctx.arc(xn, P.yNow * vr, 2.5 * hr, 0, Math.PI * 2); ctx.fill();
    }

    // Tags: targets + stop on the right edge, entry on the left; never overlapping.
    if (x2 - x1 < 60 * hr) return;
    const right = spread([
      ...P.targets.map((t, i) => ({ y: P.yTargets[i] * vr, text: t.tag, bg: P.profit(0.92) })),
      { y: yS, text: P.stopTag, bg: P.loss(0.92) },
    ], 18 * vr);
    for (const l of right) pill(ctx, l.text, x2 - 4 * hr, l.y, l.bg, P.tagText, hr, 'right');
    pill(ctx, P.entryTag, x1 + 4 * hr, yE, P.line, P.entryText, hr, 'left');
  }

  class Renderer {
    constructor(items) { this.items = items; }
    draw(target) {
      const it = this.items;
      if (!it) return;
      target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
        ctx.save();
        for (const P of it.positions || []) drawPosition(ctx, P, hr, vr);
        for (const s of it.rects) {
          const x1 = Math.round(s.x1 * hr), x2 = Math.round(s.x2 * hr);
          const y1 = Math.round(Math.min(s.y1, s.y2) * vr), y2 = Math.round(Math.max(s.y1, s.y2) * vr);
          if (x2 <= x1) continue;
          if (s.fill) { ctx.fillStyle = s.fill; ctx.fillRect(x1, y1, x2 - x1, Math.max(1, y2 - y1)); }
          if (s.stroke) {
            ctx.strokeStyle = s.stroke; ctx.lineWidth = (s.lineWidth || 1) * hr; setDash(ctx, s.dash, hr);
            ctx.strokeRect(x1 + 0.5, y1 + 0.5, x2 - x1 - 1, Math.max(1, y2 - y1 - 1));
          }
          if (s.label) {
            const right = s.labelAlign === 'right';
            drawLabel(ctx, s.label, right ? x2 - 4 * hr : x1 + 4 * hr, y1 + 8 * vr, s.labelColor || s.stroke || '#999',
              hr, right ? 'right' : 'left', s.labelBg);
          }
        }
        for (const s of it.segments) {
          const x1 = s.x1 * hr, x2 = s.x2 * hr, y = Math.round(s.y * vr) + 0.5;
          ctx.strokeStyle = s.color; ctx.lineWidth = (s.width || 1) * hr; setDash(ctx, s.dash, hr);
          ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke();
          if (s.label) {
            const pos = s.labelPos || 'mid';
            const lx = pos === 'start' ? x1 + 3 * hr : pos === 'end' ? x2 - 3 * hr : (x1 + x2) / 2;
            drawLabel(ctx, s.label, lx, y - 7 * vr, s.labelColor || s.color, hr,
              pos === 'start' ? 'left' : pos === 'end' ? 'right' : 'center', s.labelBg);
          }
        }
        ctx.setLineDash([]);
        for (const g of it.glyphs) {
          const x = g.x * hr, y = g.y * vr, sz = (g.size || 5) * hr;
          ctx.strokeStyle = g.color; ctx.fillStyle = g.color; ctx.lineWidth = 2 * hr;
          ctx.beginPath();
          if (g.kind === 'x') {
            ctx.moveTo(x - sz, y - sz); ctx.lineTo(x + sz, y + sz); ctx.moveTo(x + sz, y - sz); ctx.lineTo(x - sz, y + sz);
            ctx.stroke();
          } else if (g.kind === 'diamond') {
            ctx.moveTo(x, y - sz); ctx.lineTo(x + sz, y); ctx.lineTo(x, y + sz); ctx.lineTo(x - sz, y); ctx.closePath();
            ctx.fill();
          } else {
            ctx.arc(x, y, sz * 0.7, 0, Math.PI * 2); ctx.fill();
          }
          if (g.label) drawLabel(ctx, g.label, x, y + (g.above ? -12 : 12) * vr, g.labelColor || g.color, hr, 'center');
        }
        for (const t of it.texts) {
          drawLabel(ctx, t.text, t.x * hr, t.y * vr + (t.above ? -9 : 9) * vr, t.color, hr, 'center');
        }
        ctx.restore();
      });
    }
  }

  class PaneView {
    constructor(src) { this.src = src; this.items = null; }
    update() {
      const { chart, series, mapper, shapes } = this.src;
      if (!chart || !series || !mapper) { this.items = null; return; }
      const ts = chart.timeScale();
      const right = ts.width();
      // logicalToCoordinate only handles whole bar indexes (it returns 0 for fractional ones),
      // so interpolate between the two neighbouring bars for times that fall between bars.
      const X = (t) => {
        if (t === null || t === undefined) return right;
        const l = mapper.logical(t);
        if (l === null || !Number.isFinite(l)) return null;
        const i = Math.floor(l), f = l - i;
        const x0 = ts.logicalToCoordinate(i);
        if (x0 === null) return null;
        if (f === 0) return x0;
        const x1 = ts.logicalToCoordinate(i + 1);
        return x1 === null ? x0 : x0 + f * (x1 - x0);
      };
      const Y = (p) => series.priceToCoordinate(p);
      const out = { rects: [], segments: [], glyphs: [], texts: [], positions: [] };
      for (const P of shapes.positions || []) {
        const x1 = X(P.t1), xn = X(P.tNow), x2 = X(P.tEnd);
        const yEntry = Y(P.entry), yStop = Y(P.stop), yTargets = P.targets.map((t) => Y(t.price));
        if ([x1, xn, x2, yEntry, yStop, ...yTargets].some((v) => v === null)) continue;
        const yNow = P.nowPrice == null ? null : Y(P.nowPrice);
        out.positions.push({ ...P, x1, xn, x2: Math.min(right + 10, x2), yEntry, yStop, yTargets, yNow });
      }
      for (const s of shapes.rects) {
        const x1 = X(s.t1), x2 = s.t2 == null ? right : X(s.t2), y1 = Y(s.p1), y2 = Y(s.p2);
        if (x1 === null || x2 === null || y1 === null || y2 === null) continue;
        out.rects.push({ ...s, x1: Math.max(-10, x1), x2: Math.min(right + 10, x2), y1, y2 });
      }
      for (const s of shapes.segments) {
        const x1 = X(s.t1), x2 = s.t2 == null ? right : X(s.t2), y = Y(s.p);
        if (x1 === null || x2 === null || y === null) continue;
        out.segments.push({ ...s, x1: Math.max(-10, x1), x2: Math.min(right + 10, x2), y });
      }
      for (const g of shapes.glyphs) {
        const x = X(g.t), y = Y(g.p);
        if (x !== null && y !== null) out.glyphs.push({ ...g, x, y });
      }
      for (const t of shapes.texts) {
        const x = X(t.t), y = Y(t.p);
        if (x !== null && y !== null) out.texts.push({ ...t, x, y });
      }
      this.items = out;
    }
    renderer() { return new Renderer(this.items); }
    zOrder() { return this.src.z; }
  }

  class OverlayPrimitive {
    constructor(zOrder) {
      this.z = zOrder || 'normal';
      this.shapes = { rects: [], segments: [], glyphs: [], texts: [], positions: [] };
      this.view = new PaneView(this);
      this.chart = null; this.series = null; this.request = null; this.mapper = null;
    }
    attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.request = requestUpdate; }
    detached() { this.chart = this.series = this.request = null; }
    setMapper(m) { this.mapper = m; }
    setShapes(shapes) {
      this.shapes = { rects: [], segments: [], glyphs: [], texts: [], positions: [], ...shapes };
      if (this.request) this.request();
    }
    // Price range that must stay visible (e.g. the trade plan's entry, stop and TP1); merged by the
    // chart with the candles' own range. Cached value: this is called on every scroll/zoom frame.
    setAutoscale(min, max) {
      this.autoscale = Number.isFinite(min) && Number.isFinite(max) ? { priceRange: { minValue: min, maxValue: max } } : null;
      if (this.request) this.request();
    }
    autoscaleInfo() { return this.autoscale || null; }
    updateAllViews() { this.view.update(); }
    paneViews() { return [this.view]; }
  }

  global.SMCOverlays = { TimeMapper, OverlayPrimitive };
})(window);
