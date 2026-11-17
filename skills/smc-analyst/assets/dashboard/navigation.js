/* SMC Analyst - TradingView-style chart navigation for Lightweight Charts v5.
 *
 * Lightweight Charts' built-in wheel handling mixes the two axes: a trackpad swipe to the
 * right also zooms (deltaY noise), and a plain mouse wheel always zooms. This module replaces
 * it with explicit, per-region behaviour:
 *
 *   chart pane   wheel / trackpad swipe     -> pan (left/right = time, up/down = price)
 *                Shift + wheel              -> pan time with a vertical-only mouse wheel
 *                Ctrl/Cmd + wheel, pinch    -> zoom time around the cursor
 *                drag                       -> pan time and price (grab cursor)
 *   time axis    wheel or drag              -> zoom time      (ew-resize cursor)
 *   price axis   wheel or drag              -> zoom price     (ns-resize cursor)
 *   double-click an axis                    -> reset that axis (auto-scale / default spacing)
 *
 * Each wheel event moves along ONE axis only (the dominant one), so a slightly diagonal
 * swipe never pans and zooms at the same time.
 *
 * Horizontal navigation never changes the vertical scale. Lightweight Charts' price auto-scale
 * re-fits the axis to whatever candles are on screen, so panning sideways visibly zoomed the
 * chart in and out (measured: the price span swung ~85% over a few hundred pixels). Any time
 * navigation therefore first locks the price axis at its current range (freezePrice). Auto-scale
 * returns only when the user asks: the "auto" corner toggle, key A, F (fit), a timeframe switch or
 * a double-click on the price axis. While locked, live ticks shift (never rescale) the price axis
 * just enough to keep the latest price visible (keepInView). onAutoScaleChange drives the toggle.
 */
(function (global) {
  'use strict';

  const ZOOM_SPEED = 0.0025;     // per wheel pixel; 100px (one mouse notch) ~ 28%
  const MIN_BARS = 8;            // most zoomed-in time view
  const MIN_VISIBLE = 5;         // bars that must stay on screen when panning away
  const DRAG_SLOP = 3;           // px of pointer travel before a press counts as a drag
  const VERT_SLOP = 6;           // px of vertical travel before a pane drag also pans price
  const VIEW_MARGIN = 0.08;      // keepInView leaves this fraction of the pane free above/below

  function wheelPixels(e, pageSize) {
    const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? pageSize : 1;
    return { dx: e.deltaX * k, dy: e.deltaY * k };
  }

  class ChartNavigation {
    constructor(chart, series, container, opts = {}) {
      this.chart = chart;
      this.series = series;
      this.el = container;
      this.barCount = opts.barCount || (() => 0);
      this.onAutoScaleChange = opts.onAutoScaleChange || (() => {});
      this.drag = null;

      // Hand the wheel to us; keep LWC's own drag-to-pan, axis drag-to-scale and pinch.
      chart.applyOptions({
        handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
        handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: { time: true, price: true },
          axisDoubleClickReset: { time: true, price: true } },
      });

      container.addEventListener('wheel', (e) => this.onWheel(e), { passive: false, capture: true });
      container.addEventListener('pointerdown', (e) => this.onPointerDown(e), true);
      container.addEventListener('pointermove', (e) => this.onPointerMove(e), true);
      global.addEventListener('pointerup', () => this.onPointerUp(), true);
      container.addEventListener('dblclick', () => setTimeout(() => this.syncAutoScale(), 0), true);
    }

    // ------------------------------------------------------------- geometry
    regions() {
      const rect = this.el.getBoundingClientRect();
      const axisW = this.chart.priceScale('right').width();
      const axisH = this.chart.timeScale().height();
      return { rect, paneW: rect.width - axisW, paneH: rect.height - axisH };
    }

    regionAt(e) {
      const { rect, paneW, paneH } = this.regions();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      if (x >= paneW) return { area: y < paneH ? 'price' : 'corner', x, y, paneW, paneH };
      if (y >= paneH) return { area: 'time', x, y, paneW, paneH };
      return { area: 'pane', x, y, paneW, paneH };
    }

    isAutoScale() { return this.series.priceScale().options().autoScale !== false; }

    syncAutoScale() { this.onAutoScaleChange(this.isAutoScale()); }

    // Lock the price axis exactly where it is (setVisibleRange(getVisibleRange()) is lossless),
    // so the time navigation that follows cannot re-fit, i.e. visually zoom, the price axis.
    freezePrice() {
      if (!this.isAutoScale()) return;
      const ps = this.series.priceScale();
      const r = ps.getVisibleRange();
      if (!r) return;
      ps.setVisibleRange({ from: r.from, to: r.to });
      this.syncAutoScale();
    }

    // ------------------------------------------------------------- time axis
    panTime(px, paneW) {
      const ts = this.chart.timeScale();
      const r = ts.getVisibleLogicalRange();
      if (!r || !paneW) return;
      this.freezePrice();
      const span = r.to - r.from;
      let shift = px * span / paneW;
      const n = this.barCount();
      // Keep a few bars visible at either end instead of scrolling into emptiness.
      if (n) shift = Math.max(MIN_VISIBLE - r.to, Math.min(n - MIN_VISIBLE - r.from, shift));
      ts.setVisibleLogicalRange({ from: r.from + shift, to: r.to + shift });
    }

    zoomTime(delta, x, paneW) {
      const ts = this.chart.timeScale();
      const r = ts.getVisibleLogicalRange();
      if (!r || !paneW) return;
      const span = r.to - r.from;
      const maxSpan = Math.max(MIN_BARS * 2, paneW / 0.5);   // LWC's minimum bar spacing is 0.5px
      const next = Math.min(maxSpan, Math.max(MIN_BARS, span * Math.exp(delta * ZOOM_SPEED)));
      const f = next / span;
      if (f === 1) return;
      this.freezePrice();
      const anchor = r.from + span * Math.min(1, Math.max(0, x / paneW));   // logical bar under the cursor
      ts.setVisibleLogicalRange({ from: anchor - (anchor - r.from) * f, to: anchor + (r.to - anchor) * f });
    }

    // ------------------------------------------------------------- price axis
    priceRange(paneH) {
      const top = this.series.coordinateToPrice(0), bottom = this.series.coordinateToPrice(paneH);
      return top == null || bottom == null ? null : { top, bottom };
    }

    panPrice(px, paneH) {
      const ps = this.series.priceScale();
      const r = ps.getVisibleRange(), view = this.priceRange(paneH);
      if (!r || !view || !paneH) return;
      const d = px * (view.top - view.bottom) / paneH;   // scroll down -> see lower prices
      ps.setVisibleRange({ from: r.from - d, to: r.to - d });
      this.syncAutoScale();
    }

    zoomPrice(delta, y, paneH) {
      const ps = this.series.priceScale();
      const r = ps.getVisibleRange();
      const anchor = this.series.coordinateToPrice(Math.min(paneH, Math.max(0, y)));
      if (!r || anchor == null) return;
      const f = Math.exp(delta * ZOOM_SPEED);
      const from = anchor - (anchor - r.from) * f, to = anchor + (r.to - anchor) * f;
      if (!(to - from > 0) || !Number.isFinite(from) || !Number.isFinite(to)) return;
      ps.setVisibleRange({ from, to });
      this.syncAutoScale();
    }

    // ------------------------------------------------------------- events
    onWheel(e) {
      const g = this.regionAt(e);
      if (g.area === 'corner') return;
      const { dx, dy } = wheelPixels(e, g.paneH);
      if (!dx && !dy) return;
      e.preventDefault();
      e.stopPropagation();
      const main = Math.abs(dy) >= Math.abs(dx) ? dy : dx;

      if (g.area === 'time') return this.zoomTime(main, g.x, g.paneW);
      if (g.area === 'price') return this.zoomPrice(main, g.y, g.paneH);
      // Chart pane. Trackpad pinch arrives as ctrlKey + deltaY.
      if (e.ctrlKey || e.metaKey) return this.zoomTime(main, g.x, g.paneW);
      if (e.shiftKey) return this.panTime(main, g.paneW);
      if (Math.abs(dx) > Math.abs(dy)) this.panTime(dx, g.paneW);
      else this.panPrice(dy, g.paneH);
    }

    onPointerDown(e) {
      if (e.button !== 0) return;
      const g = this.regionAt(e);
      if (g.area !== 'pane' && g.area !== 'time') return;
      // LWC moves the time axis itself (pane drag = pan, time-axis drag = zoom). Pointer events
      // fire before the mouse events LWC listens to, so freezing on the first real movement
      // happens before LWC applies it. Vertical pane drag is ours.
      this.drag = { area: g.area, x0: e.clientX, y0: e.clientY, y: e.clientY, paneH: g.paneH,
        moved: false, vertical: false };
      if (g.area === 'pane') this.el.classList.add('nav-grabbing');
    }

    onPointerMove(e) {
      if (this.drag && e.buttons & 1) {
        const d = this.drag;
        if (!d.moved && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) >= DRAG_SLOP) {
          d.moved = true;
          this.freezePrice();
        }
        if (d.area !== 'pane') return;
        if (!d.vertical && Math.abs(e.clientY - d.y0) < VERT_SLOP) return;
        if (!d.vertical) { d.vertical = true; d.y = d.y0; }
        const dy = e.clientY - d.y;
        d.y = e.clientY;
        if (dy) this.panPrice(-dy, d.paneH);
        return;
      }
      if (this.drag) this.onPointerUp();
      const area = this.regionAt(e).area;
      this.el.dataset.navArea = area;
    }

    onPointerUp() {
      if (!this.drag) return;
      this.drag = null;
      this.el.classList.remove('nav-grabbing');
      this.syncAutoScale();
    }

    // ------------------------------------------------------------- public
    // Live tick while the price axis is locked: if the newest bar is on screen and its price has
    // left the view, slide the axis (same scale) so the price sits VIEW_MARGIN inside the edge.
    keepInView(price) {
      if (this.isAutoScale() || this.drag || !Number.isFinite(price)) return;
      const n = this.barCount();
      const r = this.chart.timeScale().getVisibleLogicalRange();
      if (!r || n - 1 < r.from || n - 1 > r.to) return;
      const { paneH } = this.regions();
      const view = this.priceRange(paneH);
      if (!view) return;
      const span = view.top - view.bottom, pad = span * VIEW_MARGIN;
      let d = 0;
      if (price > view.top - pad) d = price - (view.top - pad);
      else if (price < view.bottom + pad) d = price - (view.bottom + pad);
      if (!d) return;
      const ps = this.series.priceScale(), pr = ps.getVisibleRange();
      if (pr) ps.setVisibleRange({ from: pr.from + d, to: pr.to + d });
    }

    setAutoScale(on) {
      this.series.priceScale().setAutoScale(on);
      this.syncAutoScale();
    }
  }

  global.SMCNavigation = { ChartNavigation };
})(window);
