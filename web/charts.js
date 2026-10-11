/* Hand-rolled SVG charts. No charting library, no build step.
 *
 * Every chart here is a single-measure time series, which is deliberate: the
 * one thing this dashboard must never do is put two differently-scaled
 * measures on two y-axes. Water depth and rainfall are both "flood" data and
 * belong on the same screen, but not on the same axis -- so they are separate
 * charts, stacked.
 *
 * Interaction is standard, not optional: a crosshair and a tooltip on every
 * time series, because an HTML chart that cannot be interrogated is a picture.
 */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const el = (name, attrs) => {
    const node = document.createElementNS(NS, name);
    for (const k in attrs || {}) {
      if (attrs[k] === null || attrs[k] === undefined) continue;
      node.setAttribute(k, String(attrs[k]));
    }
    return node;
  };

  // ---- number and time formatting ---------------------------------------
  function fmt(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return "--";
    const d = digits === undefined ? (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2) : digits;
    return Number(v).toFixed(d).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
  }

  function clockTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function clockDateTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  // "4.3 h", "25 min", "40 s" -- a duration a person reads, not seconds.
  function duration(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--";
    const s = Math.abs(seconds);
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 5400) return `${fmt(s / 60, s < 600 ? 1 : 0)} min`;
    if (s < 172800) return `${fmt(s / 3600, 1)} h`;
    return `${fmt(s / 86400, 1)} days`;
  }

  // ---- shared tooltip ----------------------------------------------------
  let tip = null;
  function tooltip() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tooltip";
      tip.setAttribute("role", "status");
      document.body.appendChild(tip);
    }
    return tip;
  }
  function showTip(html, x, y) {
    const t = tooltip();
    t.innerHTML = html;
    t.style.left = `${x}px`;
    t.style.top = `${y}px`;
    t.classList.add("on");
  }
  function hideTip() { if (tip) tip.classList.remove("on"); }

  // ---- scales ------------------------------------------------------------
  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) return [lo];
    const span = hi - lo;
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 0.001; v += step) {
      out.push(Math.round(v / step) * step);
    }
    return out.length ? out : [lo, hi];
  }

  /* timeSeries(container, options)
   *
   * options:
   *   series: [{ points: [[ts, value], ...], color, label, dashed, area }]
   *   bands:  [{ from, to, color, label }]   horizontal threshold bands
   *   rules:  [{ at, color, label }]         horizontal threshold lines
   *   unit, height, yMin, yMax, zeroFloor, digits
   */
  function timeSeries(container, options) {
    const opts = options || {};
    const series = (opts.series || []).filter((s) => s && s.points && s.points.length);
    container.textContent = "";

    if (!series.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML = '<span class="big">◌</span>No readings yet for this measure.';
      container.appendChild(empty);
      return;
    }

    const H = opts.height || 190;
    const PAD = { t: 12, r: 52, b: 22, l: 44 };
    // A viewBox plus width:100% means one render adapts to any column width,
    // so there is no resize handler to get wrong.
    const W = 760;

    let tLo = Infinity, tHi = -Infinity, vLo = Infinity, vHi = -Infinity;
    for (const s of series) {
      for (const [t, v] of s.points) {
        if (t < tLo) tLo = t;
        if (t > tHi) tHi = t;
        if (v < vLo) vLo = v;
        if (v > vHi) vHi = v;
      }
    }
    for (const b of opts.bands || []) {
      if (b.from !== undefined && b.from < vLo) vLo = b.from;
      if (b.to !== undefined && b.to > vHi) vHi = b.to;
    }
    for (const r of opts.rules || []) {
      if (r.at < vLo) vLo = r.at;
      if (r.at > vHi) vHi = r.at;
    }
    if (tHi === tLo) { tHi = tLo + 1; }

    if (opts.yMin !== undefined) vLo = opts.yMin;
    if (opts.yMax !== undefined) vHi = opts.yMax;
    if (opts.zeroFloor && vLo > 0) vLo = 0;
    if (vHi === vLo) { vHi = vLo + 1; }
    const headroom = (vHi - vLo) * 0.1;
    vHi += headroom;
    if (!opts.zeroFloor) vLo -= headroom;

    const x = (t) => PAD.l + ((t - tLo) / (tHi - tLo)) * (W - PAD.l - PAD.r);
    const y = (v) => H - PAD.b - ((v - vLo) / (vHi - vLo)) * (H - PAD.t - PAD.b);

    const svg = el("svg", {
      class: "chart", viewBox: `0 0 ${W} ${H}`, width: "100%", height: H,
      preserveAspectRatio: "none", role: "img",
      "aria-label": opts.ariaLabel || `${opts.unit || "value"} over time`,
    });

    // -- threshold bands, behind everything -------------------------------
    for (const band of opts.bands || []) {
      const top = y(Math.min(band.to, vHi));
      const bottom = y(Math.max(band.from, vLo));
      if (bottom - top <= 0.5) continue;
      svg.appendChild(el("rect", {
        x: PAD.l, y: top, width: W - PAD.l - PAD.r, height: bottom - top,
        fill: band.color, opacity: band.opacity || 0.1,
      }));
      if (band.label) {
        const label = el("text", { x: W - PAD.r + 5, y: top + 10, class: "band-label" });
        label.textContent = band.label;
        svg.appendChild(label);
      }
    }

    // -- y grid and labels -------------------------------------------------
    for (const tick of niceTicks(vLo, vHi, 4)) {
      const yy = y(tick);
      if (yy < PAD.t - 2 || yy > H - PAD.b + 2) continue;
      svg.appendChild(el("line", { x1: PAD.l, x2: W - PAD.r, y1: yy, y2: yy, class: "grid-line" }));
      const label = el("text", { x: PAD.l - 7, y: yy + 3.5, "text-anchor": "end" });
      label.textContent = fmt(tick, Math.abs(tick) >= 10 ? 0 : 1);
      svg.appendChild(label);
    }

    // -- x labels: first, middle, last ------------------------------------
    for (const frac of [0, 0.5, 1]) {
      const t = tLo + (tHi - tLo) * frac;
      const label = el("text", {
        x: x(t), y: H - PAD.b + 14,
        "text-anchor": frac === 0 ? "start" : frac === 1 ? "end" : "middle",
      });
      label.textContent = clockTime(t);
      svg.appendChild(label);
    }
    svg.appendChild(el("line", {
      x1: PAD.l, x2: W - PAD.r, y1: H - PAD.b, y2: H - PAD.b, class: "axis-line",
    }));

    // -- threshold rules ---------------------------------------------------
    for (const rule of opts.rules || []) {
      const yy = y(rule.at);
      if (yy < PAD.t - 2 || yy > H - PAD.b + 2) continue;
      svg.appendChild(el("line", {
        x1: PAD.l, x2: W - PAD.r, y1: yy, y2: yy,
        stroke: rule.color, "stroke-width": 1.5, "stroke-dasharray": "4 4", opacity: 0.85,
      }));
      if (rule.label) {
        const label = el("text", { x: W - PAD.r + 5, y: yy + 3.5, fill: rule.color });
        label.textContent = rule.label;
        label.style.fontWeight = "620";
        svg.appendChild(label);
      }
    }

    // -- the data ----------------------------------------------------------
    const placedLabels = [];
    series.forEach((s, i) => {
      const pts = s.points.slice().sort((a, b) => a[0] - b[0]);
      const d = pts.map((p, n) => `${n ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`).join(" ");
      if (s.area) {
        const gradId = `grad-${Math.random().toString(36).slice(2, 9)}`;
        const defs = el("defs");
        const grad = el("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
        grad.appendChild(el("stop", { offset: "0%", "stop-color": s.color, "stop-opacity": 0.26 }));
        grad.appendChild(el("stop", { offset: "100%", "stop-color": s.color, "stop-opacity": 0.02 }));
        defs.appendChild(grad);
        svg.appendChild(defs);
        svg.appendChild(el("path", {
          class: "series-area",
          d: `${d} L${x(pts[pts.length - 1][0]).toFixed(1)} ${y(vLo).toFixed(1)} L${x(pts[0][0]).toFixed(1)} ${y(vLo).toFixed(1)} Z`,
          fill: `url(#${gradId})`,
        }));
      }
      svg.appendChild(el("path", {
        class: `series-line${s.dashed ? " forecast-line" : ""}`, d, stroke: s.color,
      }));

      // Direct-label the latest value: one number on the chart, not every point.
      const last = pts[pts.length - 1];
      svg.appendChild(el("circle", {
        cx: x(last[0]), cy: y(last[1]), r: 3.5, fill: s.color,
        stroke: "var(--glass-solid)", "stroke-width": 1.5,
      }));
      if (i === 0 || series.length <= 2) {
        // Direct labels collide when two series end at similar values -- the
        // heat index at 36.4 sat on top of the air temperature at 34.5. Keep a
        // minimum vertical gap, pushing each new label clear of the ones
        // already placed.
        let ly = y(last[1]) + 3.5;
        for (const taken of placedLabels) {
          if (Math.abs(ly - taken) < 13) ly = taken + (ly >= taken ? 13 : -13);
        }
        ly = Math.max(PAD.t + 8, Math.min(H - PAD.b - 2, ly));
        placedLabels.push(ly);
        const label = el("text", {
          x: Math.min(x(last[0]) + 7, W - PAD.r + 48), y: ly, class: "val",
          "text-anchor": x(last[0]) > W - PAD.r - 30 ? "end" : "start",
        });
        label.textContent = fmt(last[1], opts.digits);
        svg.appendChild(label);
      }
    });

    // -- crosshair ---------------------------------------------------------
    const cursor = el("g", { opacity: 0 });
    const vline = el("line", { y1: PAD.t, y2: H - PAD.b, class: "cursor-line" });
    cursor.appendChild(vline);
    const dots = series.map((s) =>
      el("circle", { r: 4.5, fill: s.color, class: "cursor-dot" })
    );
    dots.forEach((d) => cursor.appendChild(d));
    svg.appendChild(cursor);

    // A transparent hit area spanning the plot: the target is the whole
    // column, not the 2px line, so a finger or a fast mouse still lands.
    const hit = el("rect", {
      x: PAD.l, y: PAD.t, width: W - PAD.l - PAD.r, height: H - PAD.t - PAD.b,
      fill: "transparent", style: "cursor:crosshair",
    });

    function nearest(pts, t) {
      let best = pts[0], bestD = Infinity;
      for (const p of pts) {
        const d = Math.abs(p[0] - t);
        if (d < bestD) { bestD = d; best = p; }
      }
      return best;
    }

    function move(evt) {
      const rect = svg.getBoundingClientRect();
      const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
      const clientY = evt.touches ? evt.touches[0].clientY : evt.clientY;
      const px = ((clientX - rect.left) / rect.width) * W;
      const t = tLo + ((px - PAD.l) / (W - PAD.l - PAD.r)) * (tHi - tLo);
      let shownTime = t;
      const rows = [];
      series.forEach((s, i) => {
        const p = nearest(s.points, t);
        if (i === 0) shownTime = p[0];
        dots[i].setAttribute("cx", x(p[0]));
        dots[i].setAttribute("cy", y(p[1]));
        rows.push(
          `<div class="t-row"><span class="k"><span class="swatch" style="display:inline-block;width:10px;height:3px;border-radius:2px;background:${s.color}"></span>${s.label || ""}</span>` +
          `<span class="v">${fmt(p[1], opts.digits)}${opts.unit ? ` ${opts.unit}` : ""}</span></div>`
        );
      });
      vline.setAttribute("x1", x(shownTime));
      vline.setAttribute("x2", x(shownTime));
      cursor.setAttribute("opacity", 1);
      showTip(`<div class="t-time">${clockDateTime(shownTime)}</div>${rows.join("")}`, clientX, clientY);
    }

    hit.addEventListener("mousemove", move);
    hit.addEventListener("touchmove", move, { passive: true });
    hit.addEventListener("touchstart", move, { passive: true });
    const leave = () => { cursor.setAttribute("opacity", 0); hideTip(); };
    hit.addEventListener("mouseleave", leave);
    hit.addEventListener("touchend", leave);
    svg.appendChild(hit);

    container.appendChild(svg);

    if (series.length >= 2 || opts.forceLegend) {
      const legend = document.createElement("div");
      legend.className = "legend";
      legend.innerHTML = series.map((s) =>
        `<span class="item"><span class="swatch${s.dashed ? " dash" : ""}" style="background:${s.color};color:${s.color}"></span>${s.label || ""}</span>`
      ).join("");
      container.appendChild(legend);
    }
  }

  /* A sparkline for a stat tile: shape only, no axes, no interaction. */
  function spark(container, points, color, height) {
    container.textContent = "";
    if (!points || points.length < 2) return;
    const H = height || 30, W = 150, PAD = 3;
    let lo = Infinity, hi = -Infinity;
    for (const [, v] of points) { if (v < lo) lo = v; if (v > hi) hi = v; }
    if (hi === lo) { hi = lo + 1; }
    const t0 = points[0][0], t1 = points[points.length - 1][0] || t0 + 1;
    const x = (t) => PAD + ((t - t0) / Math.max(1e-9, t1 - t0)) * (W - 2 * PAD);
    const y = (v) => H - PAD - ((v - lo) / (hi - lo)) * (H - 2 * PAD);
    const svg = el("svg", {
      viewBox: `0 0 ${W} ${H}`, width: "100%", height: H,
      preserveAspectRatio: "none", "aria-hidden": "true",
    });
    svg.appendChild(el("path", {
      d: points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`).join(" "),
      fill: "none", stroke: color, "stroke-width": 1.75, "stroke-linejoin": "round",
    }));
    const last = points[points.length - 1];
    svg.appendChild(el("circle", { cx: x(last[0]), cy: y(last[1]), r: 2.2, fill: color }));
    container.appendChild(svg);
  }

  /* A vertical tank gauge. Magnitude of one value against a known maximum,
   * which is the one case where a filled shape beats a number alone -- a tank
   * is a thing people picture. */
  function tankGauge(container, pct, thresholds) {
    container.textContent = "";
    const W = 76, H = 150, lipH = 7;
    const pctClamped = Math.max(0, Math.min(100, pct || 0));
    const bodyTop = lipH + 3;
    const bodyH = H - bodyTop - 3;
    const svg = el("svg", {
      viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": `Tank ${fmt(pctClamped, 0)} percent full`,
    });
    svg.appendChild(el("rect", {
      x: 8, y: bodyTop, width: W - 16, height: bodyH, rx: 9,
      fill: "var(--glass-solid)", stroke: "var(--axis)", "stroke-width": 1.5,
    }));
    const fillH = (bodyH - 4) * (pctClamped / 100);
    const colour = pctClamped <= (thresholds && thresholds.critical || 10) ? "var(--critical)"
      : pctClamped <= (thresholds && thresholds.low || 25) ? "var(--warning)"
      : "var(--series-1)";
    svg.appendChild(el("rect", {
      x: 10, y: bodyTop + 2 + (bodyH - 4 - fillH), width: W - 20, height: Math.max(0, fillH),
      rx: 7, fill: colour, opacity: 0.8,
    }));
    for (const [value, stroke] of [
      [(thresholds && thresholds.low) || 25, "var(--warning)"],
      [(thresholds && thresholds.critical) || 10, "var(--critical)"],
    ]) {
      const yy = bodyTop + 2 + (bodyH - 4) * (1 - value / 100);
      svg.appendChild(el("line", {
        x1: 6, x2: W - 6, y1: yy, y2: yy, stroke, "stroke-width": 1.5, "stroke-dasharray": "3 3",
      }));
    }
    svg.appendChild(el("rect", {
      x: 3, y: 0, width: W - 6, height: lipH, rx: 3,
      fill: "var(--axis)", opacity: 0.5,
    }));
    container.appendChild(svg);
  }

  global.Charts = { timeSeries, spark, tankGauge, fmt, clockTime, clockDateTime, duration };
})(window);
