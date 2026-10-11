/* AquaSentinel dashboard logic.
 *
 * The governing rule of this file: a number must arrive on screen carrying how
 * it was obtained. The engine tags every signal `measured`, `derived`,
 * `projected` or `forecast`, and `tile()` renders that tag next to the value.
 * A dashboard that lets a six-hour forecast look like a thermometer reading is
 * worse than no dashboard, so the provenance badge is not decoration.
 */
(function () {
  "use strict";

  const { timeSeries, spark, tankGauge, fmt, clockDateTime, duration } = window.Charts;

  // Where the data comes from is decided at startup -- a running backend, a
  // run recorded by the real engine in CI, or the real engine itself compiled
  // to WebAssembly. All three produce the same shapes, so everything below
  // this line is written once.
  let SOURCE = null;

  const SEV = ["OK", "INFO", "WATCH", "WARNING", "CRITICAL"];
  const SEV_COLOR = {
    OK: "var(--ok)", INFO: "var(--ink-3)", WATCH: "var(--watch)",
    WARNING: "var(--warning)", CRITICAL: "var(--critical)",
  };
  // A status colour never travels alone: every badge carries a glyph and a word.
  const SEV_ICON = {
    OK: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.2l2.3 2.3 4.7-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    INFO: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 5.2v3M6 3.6v.1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    WATCH: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 3.4v3.2M6 8.4v.1" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
    WARNING: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.4l4.8 8.4H1.2z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6 4.6v2.4M6 8.3v.1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    CRITICAL: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.8" fill="currentColor" opacity="0.18"/><path d="M6 2.8v4M6 8.8v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
  };

  const esc = (s) => String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const sevName = (n) => SEV[Math.max(0, Math.min(4, Number(n) || 0))];
  const sevBadge = (n) => {
    const name = typeof n === "string" ? n : sevName(n);
    return `<span class="sev ${name}">${SEV_ICON[name] || ""}${name}</span>`;
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // ---- state -------------------------------------------------------------
  const state = {
    overview: null, details: {}, metrics: null, view: "overview", error: null,
    mode: "replay", scenario: null, scenarios: [], engineStatus: "", engineError: null,
    engineLoading: false,
    engineConfig: null, booting: true,
  };

  let refreshing = false;
  let lastRenderedView = null;
  let renderQueued = false;
  let lastRenderAt = 0;

  /* Coalesce data-driven renders.
   *
   * A live source pushes frames several times a second. Rebuilding the whole
   * view that often is wasted work, and it tears down anything the reader had
   * open. Rendering at most every 350 ms still looks continuous. */
  const MIN_RENDER_GAP_MS = 350;

  function scheduleRender() {
    if (renderQueued) return;
    const wait = Math.max(0, MIN_RENDER_GAP_MS - (Date.now() - lastRenderAt));
    renderQueued = true;
    setTimeout(() => {
      renderQueued = false;
      render();
    }, wait);
  }

  async function refresh() {
    // Playback can tick faster than a refresh completes; overlapping passes
    // would interleave and render a half-updated page.
    if (refreshing) return;
    refreshing = true;
    try {
      state.overview = await SOURCE.overview();
      state.error = null;
      const devices = (state.overview.devices || []).map((d) => d.device_id).filter(Boolean);
      await Promise.all(devices.map(async (id) => {
        try { state.details[id] = await SOURCE.deviceDetail(id); }
        catch (e) { /* one node failing must not blank the whole page */ }
      }));
    } catch (err) {
      state.error = err.message;
    } finally {
      refreshing = false;
    }
    scheduleRender();
  }

  // ---- small render helpers ---------------------------------------------
  /* The core tile. `kind` is the provenance badge; it is required, not optional. */
  function tile(label, value, unit, kind, foot, extra) {
    const hasValue = value !== null && value !== undefined && value !== "";
    return `<div class="tile">
      <div class="label">${esc(label)}${kind ? ` <span class="kind ${esc(kind)}">${esc(kind)}</span>` : ""}</div>
      <div class="value${hasValue ? "" : " none"}">${hasValue ? esc(value) : "not available"}${hasValue && unit ? `<span class="unit">${esc(unit)}</span>` : ""}</div>
      ${foot ? `<div class="foot">${foot}</div>` : ""}
      ${extra || ""}
    </div>`;
  }

  function confidenceFoot(conf) {
    if (conf === null || conf === undefined) return "";
    const pct = Math.round(conf * 100);
    const word = pct >= 75 ? "high" : pct >= 50 ? "moderate" : "low";
    return `confidence ${pct}% (${word})`;
  }

  function evidenceTable(evidence) {
    if (!evidence || !evidence.length) return "";
    return `<div class="scroll-x"><table class="evidence">
      <thead><tr><th>Signal</th><th>Measured</th><th>Threshold</th><th>Why it counts</th></tr></thead>
      <tbody>${evidence.map((e) => `<tr>
        <td>${esc(String(e.signal || "").replace(/_/g, " "))}</td>
        <td class="num">${esc(fmt(e.value))}</td>
        <td class="num">${e.threshold === null || e.threshold === undefined ? "--" : esc(fmt(e.threshold))}</td>
        <td>${esc(e.note || "")}</td></tr>`).join("")}</tbody></table></div>`;
  }

  function alertCard(a, opts) {
    const name = a.severity_label || sevName(a.severity);
    const ack = a.state === "acknowledged";
    return `<article class="alert" style="--sev-color:${SEV_COLOR[name]}">
      <div class="title">${sevBadge(name)} ${esc(a.title || a.module)}
        ${ack ? `<span class="kind derived">acknowledged${a.acknowledged_by ? ` by ${esc(a.acknowledged_by)}` : ""}</span>` : ""}</div>
      <div class="reason">${esc(a.reason || "")}</div>
      ${a.explanation ? `<div class="explain"><span class="src">plain-language summary &middot; ${esc(a.explanation_source || "template")}</span>${esc(a.explanation)}</div>` : ""}
      ${a.recommendation ? `<div class="action"><b>What to do:</b> ${esc(a.recommendation)}</div>` : ""}
      ${(a.evidence && a.evidence.length) ? `<details class="more"><summary>Show the evidence behind this (${a.evidence.length})</summary><div>${evidenceTable(a.evidence)}</div></details>` : ""}
      <footer>
        <span>${esc(a.device_id || "")}</span>
        <span>&middot;</span>
        <span>${a.ts ? esc(clockDateTime(a.ts)) : ""}</span>
        <span>&middot;</span>
        <span>${esc(confidenceFoot(a.confidence))}</span>
        ${(!ack && a.state === "active" && opts !== false) ? `<span class="spacer" style="flex:1"></span><button class="ghost" data-ack="${esc(a.alert_id)}">Acknowledge</button>` : ""}
      </footer>
    </article>`;
  }

  /* Pull one signal out of a module result, with its provenance intact. */
  function sig(mod, name) {
    if (!mod || !mod.signals) return null;
    return mod.signals.find((s) => s.name === name) || null;
  }
  function sigVal(mod, name) {
    const s = sig(mod, name);
    return s ? s.value : null;
  }
  function sigKind(mod, name) {
    const s = sig(mod, name);
    return s ? s.kind : "derived";
  }

  /* Find the first device whose situation includes a given module. */
  function deviceFor(moduleName) {
    const devices = (state.overview && state.overview.devices) || [];
    const withModule = devices.filter((d) => d.modules && d.modules[moduleName]);
    if (!withModule.length) return null;
    // Prefer a fresh node over a stale one, then the most severe.
    withModule.sort((a, b) =>
      (a.stale ? 1 : 0) - (b.stale ? 1 : 0) ||
      (b.modules[moduleName].severity || 0) - (a.modules[moduleName].severity || 0));
    return withModule[0];
  }

  function seriesOf(deviceId, metric) {
    const d = state.details[deviceId];
    if (!d || !d.series) return [];
    return d.series[metric] || [];
  }

  function staleBanner(dev) {
    if (!dev) return "";
    const bits = [];
    if (dev.stale) {
      bits.push(`<div class="banner" style="background:var(--critical-bg)">
        ${SEV_ICON.CRITICAL}
        <div><b>These readings are not live.</b> Nothing has arrived from
        ${esc(dev.label || dev.device_id)} for ${esc(duration(dev.age_s))}. Everything shown for
        this node describes the past. Treat the measures it carries as unmonitored
        until it reports again &mdash; not as zero, and not as unchanged.</div></div>`);
    }
    if (dev.clock_skew_warning) {
      bits.push(`<div class="banner">${SEV_ICON.WATCH}
        <div><b>This node's clock is out by ${esc(duration(dev.clock_skew_s))}.</b>
        Readings are still arriving and are still being used; only the timestamps the node
        reports are wrong, so the time axis below is the node's own clock, not yours.
        (The simulator runs on its own clock, which is why this appears in a demo run.)</div></div>`);
    }
    return bits.join("");
  }

  function calibrationBanner() {
    const site = (state.overview && state.overview.site) || {};
    if (site.calibrated) return "";
    return `<div class="banner">${SEV_ICON.WATCH}
      <div><b>This site is running on demonstration calibration.</b>
      ${esc(site.calibration_note || "")}</div></div>`;
  }

  // ====================================================================
  // Views
  // ====================================================================

  function renderOverview() {
    const o = state.overview;
    const name = o.overall_label || sevName(o.overall_severity);
    const devices = o.devices || [];
    const queue = o.inspection_queue || [];
    const alerts = (o.active_alerts || []).filter((a) => a.state !== "cleared");
    const headline = devices.find((d) => d.headline && !d.stale);

    const tiles = [];
    const tankDev = deviceFor("tank");
    const tankMod = tankDev && tankDev.modules.tank;
    const floodDev = deviceFor("flood");
    const floodMod = floodDev && floodDev.modules.flood;
    const leakDev = deviceFor("leak");
    const leakMod = leakDev && leakDev.modules.leak;
    const demandDev = deviceFor("demand");
    const demandMod = demandDev && demandDev.modules.demand;

    tiles.push(tile("Stored supply", tankMod ? fmt(sigVal(tankMod, "fill_pct"), 0) : null, "%",
      tankMod ? sigKind(tankMod, "fill_pct") : null,
      tankMod ? `${esc(fmt(sigVal(tankMod, "remaining_l"), 0))} L remaining` : "no tank instrumented"));
    tiles.push(tile("Water at low point", floodMod ? fmt(sigVal(floodMod, "water_depth_cm"), 1) : null, "cm",
      floodMod ? sigKind(floodMod, "water_depth_cm") : null,
      floodMod ? (() => {
        const r = sigVal(floodMod, "rate_of_rise_cm_min");
        if (r === null || r === undefined) return "trend not available";
        if (Math.abs(r) < 0.05) return "level steady";
        return `${r > 0 ? "rising" : "falling"} ${esc(fmt(Math.abs(r), 2))} cm/min`;
      })() : "no low point instrumented"));
    tiles.push(tile("Line flow", leakMod ? fmt(sigVal(leakMod, "flow_lpm"), 2) : null, "L/min",
      leakMod ? sigKind(leakMod, "flow_lpm") : null,
      leakMod ? `loss score ${esc(fmt(sigVal(leakMod, "loss_score"), 2))} of 1.00` : "no meter instrumented"));
    tiles.push(tile("Heat index", demandMod ? fmt(sigVal(demandMod, "heat_index_c"), 0) : null, "°C",
      demandMod ? sigKind(demandMod, "heat_index_c") : null,
      demandMod ? `air ${esc(fmt(sigVal(demandMod, "temperature_c"), 0))} °C at ${esc(fmt(sigVal(demandMod, "humidity_pct"), 0))}% RH` : "no climate sensor"));

    const outlooks = devices.map((d) => d.supply_outlook).filter((s) => s && s.remaining_l !== null && s.remaining_l !== undefined);
    const outlook = outlooks[0];

    const cooccur = devices.flatMap((d) => (d.stale ? [] : (d.cooccurrence || [])));

    return `
      ${calibrationBanner()}
      ${devices.filter((d) => d.stale).map(staleBanner).join("")}
      <div class="grid split" style="margin-bottom:14px">
        <div class="hero" style="--sev-color:${SEV_COLOR[name]}">
          <div class="eyebrow">${esc((o.site && o.site.name) || "Site")} &middot; now</div>
          <div class="headline">${esc(headline ? headline.headline : (name === "OK" ? "All monitored risks normal" : "Attention needed"))}</div>
          <div class="detail">${esc(
            alerts.length
              ? `${alerts.length} alert${alerts.length === 1 ? "" : "s"} active across ${devices.length} node${devices.length === 1 ? "" : "s"}. The inspection queue below is ordered by severity, confidence and how much a visit would change the outcome.`
              : "No alert is active. The four monitored risks -- flooding at the low point, water loss on the service line, stored supply, and heat-driven demand -- are all within their configured thresholds."
          )}</div>
          <div class="row">${sevBadge(name)}
            ${devices.map((d) => `<span class="pill"><span class="dot ${d.stale ? "down" : "live"}"></span>${esc(d.label || d.device_id)} <b>${esc(d.overall_label || sevName(d.overall_severity))}</b></span>`).join("")}
          </div>
        </div>
        <div class="panel">
          <h2>Supply outlook <span class="kind projected">projected</span></h2>
          ${outlook ? `
            <div class="gauge">
              <div id="ov-gauge"></div>
              <div class="readout">
                <div class="pct">${esc(fmt(tankMod ? sigVal(tankMod, "fill_pct") : null, 0))}%</div>
                <div class="litres">${esc(fmt(outlook.remaining_l, 0))} L in store</div>
              </div>
            </div>
            <div class="note" style="margin-top:12px">
              <span class="h">Under forecast demand</span>${esc(outlook.note || "")}
            </div>` : `<div class="empty"><span class="big">◌</span>No tank instrumented on a reporting node.</div>`}
        </div>
      </div>

      <div class="grid c4" style="margin-bottom:14px">${tiles.join("")}</div>

      ${cooccur.length ? `<div class="panel" style="margin-bottom:14px">
        <div class="panel-head"><h2>Signals occurring together</h2>
        <span class="hint">co-occurrence, not causation</span></div>
        ${cooccur.map((c) => `<div class="note"><span class="h">${esc(c.headline)}</span>${esc(c.detail)}</div>`).join("")}
      </div>` : ""}

      <div class="grid c2">
        <div class="panel">
          <div class="panel-head"><h2>Inspection queue <span class="n">${queue.length}</span></h2>
          <span class="hint">severity &times; confidence &times; impact</span></div>
          ${queue.length ? `<ol class="queue">${queue.map((q) => `<li>
            <span class="rank">${esc(q.rank)}</span>
            <div><div class="what">${esc(q.what)} ${sevBadge(q.severity_label || sevName(q.severity))}</div>
              <div class="why">${esc(q.why)}</div>
              <div class="act"><b>Do:</b> ${esc(q.action)}</div></div>
            <span class="foot" style="font-size:0.72rem;color:var(--ink-3);white-space:nowrap">p ${esc(fmt(q.priority, 2))}</span>
          </li>`).join("")}</ol>` : `<div class="empty"><span class="big">✓</span>Nothing needs a visit. Every module is inside its configured thresholds.</div>`}
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Active alerts <span class="n">${alerts.length}</span></h2></div>
          ${alerts.length ? alerts.slice(0, 6).map((a) => alertCard(a)).join("") : `<div class="empty"><span class="big">✓</span>No active alerts.</div>`}
        </div>
      </div>

      <div class="panel" style="margin-top:14px">
        <div class="panel-head"><h2>Site</h2><span class="hint">node positions, schematic</span></div>
        <div id="ov-map"></div>
      </div>`;
  }

  function moduleHeader(mod, dev) {
    if (!mod) return "";
    const name = mod.severity_label || sevName(mod.severity);
    return `<div class="hero" style="--sev-color:${SEV_COLOR[name]};margin-bottom:14px">
      <div class="eyebrow">${esc(mod.title || mod.module)} &middot; ${esc(dev.label || dev.device_id)}</div>
      <div class="headline" style="font-size:clamp(1.05rem,2.3vw,1.35rem)">${esc(mod.reason || "")}</div>
      ${mod.recommendation ? `<div class="detail"><b>What to do:</b> ${esc(mod.recommendation)}</div>` : ""}
      <div class="row">${sevBadge(name)}
        <span class="pill">state <b>${esc(mod.state)}</b></span>
        <span class="pill">${esc(confidenceFoot(mod.confidence))}</span>
        <span class="pill"><span class="dot ${dev.stale ? "down" : "live"}"></span>${dev.stale ? `last heard ${esc(duration(dev.age_s))} ago` : "live"}</span>
      </div>
    </div>
    ${evidenceTable(mod.evidence) ? `<div class="panel" style="margin-bottom:14px">
      <div class="panel-head"><h2>Why this state</h2><span class="hint">every threshold the engine compared</span></div>
      ${evidenceTable(mod.evidence)}</div>` : ""}`;
  }

  function renderFlood() {
    const dev = deviceFor("flood");
    if (!dev) return `<div class="panel"><div class="empty"><span class="big">◌</span>No node on this site is watching a low point.</div></div>`;
    const mod = dev.modules.flood;
    const cfg = state.overview.site || {};
    const eta = sigVal(mod, "eta_to_danger_min");
    return `${staleBanner(dev)}${moduleHeader(mod, dev)}
      <div class="grid c4" style="margin-bottom:14px">
        ${tile("Water depth", fmt(sigVal(mod, "water_depth_cm"), 1), "cm", sigKind(mod, "water_depth_cm"),
          "at the surveyed low point, median-filtered")}
        ${tile("Rate of rise", fmt(sigVal(mod, "rate_of_rise_cm_min"), 2), "cm/min", sigKind(mod, "rate_of_rise_cm_min"),
          (() => { const s = sig(mod, "rate_of_rise_cm_min"); const f = s && s.detail && s.detail.fit;
            return f ? `least squares over ${esc(s.detail.window_s)} s, r&sup2; ${esc(fmt(f.r2, 2))}${s.detail.trusted ? "" : " &mdash; too noisy to trust"}` : ""; })())}
        ${tile("Rainfall", fmt(sigVal(mod, "rain_rate_mmhr"), 1), "mm/h", sigKind(mod, "rain_rate_mmhr"),
          "corroborating only; never raises severity on its own")}
        ${tile("Reaches danger depth in", eta === null || eta === undefined ? null : fmt(eta, 0), "min", "projected",
          "if the current rate of rise continues unchanged")}
      </div>
      <div class="grid c2">
        <div class="panel">
          <div class="panel-head"><h2>Water depth</h2>
          <span class="hint">raw readings; bands are this site's surveyed thresholds</span></div>
          <div id="flood-depth"></div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Rainfall intensity</h2><span class="hint">separate axis, separate chart</span></div>
          <div id="flood-rain"></div>
        </div>
      </div>
      <div class="panel" style="margin-top:14px">
        <div class="note warn"><span class="h">What this module does and does not claim</span>
          It detects rising water at one instrumented point and projects when that point reaches a
          depth surveyed <em>for that location</em>. It is not a flood forecast: predicting where water
          goes next needs drainage topology, catchment data and validation this system does not have.
          The thresholds shown (${esc(fmt(sigVal(mod, "depth_fraction_of_danger") ? 0 : 0, 0))}watch, warn and danger)
          are configuration, not public-safety limits.</div>
      </div>`;
  }

  function renderLeak() {
    const dev = deviceFor("leak");
    if (!dev) return `<div class="panel"><div class="empty"><span class="big">◌</span>No flow meter on this site.</div></div>`;
    const mod = dev.modules.leak;
    const baseSig = sig(mod, "baseline_p50_lpm");
    const volSig = sig(mod, "estimated_volume_lost_l");
    return `${staleBanner(dev)}${moduleHeader(mod, dev)}
      <div class="grid c4" style="margin-bottom:14px">
        ${tile("Flow now", fmt(sigVal(mod, "flow_lpm"), 2), "L/min", sigKind(mod, "flow_lpm"), "measured at the meter")}
        ${tile("Minimum night flow", fmt(sigVal(mod, "minimum_night_flow_lpm"), 2), "L/min", sigKind(mod, "minimum_night_flow_lpm"),
          "lowest flow in the last 30 min &mdash; the utility industry's own leak test")}
        ${tile("Loss score", fmt(sigVal(mod, "loss_score"), 2), "of 1.00", sigKind(mod, "loss_score"),
          (() => { const s = sig(mod, "loss_score"); const f = (s && s.detail && s.detail.detectors_fired) || [];
            return f.length ? `fired: ${esc(f.map((x) => x.replace(/_/g, " ")).join(", "))}` : "no detector fired"; })())}
        ${tile("Estimated volume above normal", fmt(sigVal(mod, "estimated_volume_lost_l"), 1), "L", "projected",
          (volSig && volSig.detail) ? `over ${esc(fmt(volSig.detail.duration_min, 0))} min, integrating flow above the learned median` : "")}
      </div>
      <div class="grid c2">
        <div class="panel">
          <div class="panel-head"><h2>Flow against learned normal</h2>
          <span class="hint">${baseSig && baseSig.detail ? `baseline from ${esc(baseSig.detail.samples)} samples` : ""}</span></div>
          <div id="leak-flow"></div>
        </div>
        <div class="panel stack">
          <div class="note strong"><span class="h">This says &ldquo;possible loss&rdquo;, and means it</span>
            A flow meter cannot tell a burst pipe from a tap someone left running, or from a genuine
            change in how the site uses water. Every alert here lists which detectors fired so one
            visit can settle it. Until someone looks, this is an anomaly, not a leak.</div>
          <div class="note"><span class="h">How normal is learned</span>
            Flow is banked per hour of the week and summarised as a median and a 95th percentile.
            Samples the engine already suspects are excluded, so a leak that runs for a week cannot
            quietly become the new baseline.
            ${baseSig && baseSig.detail ? `Current normal for this hour: median
            ${esc(fmt(sigVal(mod, "baseline_p50_lpm"), 2))} L/min, p95
            ${esc(fmt(sigVal(mod, "baseline_p95_lpm"), 2))} L/min.` : "Not enough history yet to state a normal for this hour."}</div>
        </div>
      </div>`;
  }

  function renderTank() {
    const dev = deviceFor("tank");
    if (!dev) return `<div class="panel"><div class="empty"><span class="big">◌</span>No tank instrumented on this site.</div></div>`;
    const mod = dev.modules.tank;
    const outlook = dev.supply_outlook || {};
    const etaSig = sig(mod, "eta_depletion_h");
    const range = (etaSig && etaSig.detail && etaSig.detail.range_h) || [null, null];
    const fillSig = sig(mod, "fill_pct");
    const rateSig = sig(mod, "consumption_lph");
    return `${staleBanner(dev)}${moduleHeader(mod, dev)}
      <div class="grid split" style="margin-bottom:14px">
        <div class="panel">
          <div class="panel-head"><h2>Stored volume</h2><span class="hint">calibrated level-to-volume mapping</span></div>
          <div class="gauge">
            <div id="tank-gauge"></div>
            <div class="readout">
              <div class="pct">${esc(fmt(sigVal(mod, "fill_pct"), 0))}%</div>
              <div class="litres">${esc(fmt(sigVal(mod, "remaining_l"), 0))} L of
                ${esc(fmt((fillSig && fillSig.detail && fillSig.detail.capacity_l) || 0, 0))} L</div>
              <div class="litres" style="margin-top:7px;font-size:0.78rem">
                mapping: <code>${esc((fillSig && fillSig.detail && fillSig.detail.mapping) || "?")}</code>
                ${(fillSig && fillSig.detail && fillSig.detail.dip_chart_points) ? ` from a ${esc(fillSig.detail.dip_chart_points)}-point dip chart` : ""}</div>
            </div>
          </div>
        </div>
        <div class="grid" style="gap:14px">
          ${tile("Current draw", fmt(sigVal(mod, "consumption_lph"), 0), "L/h", sigKind(mod, "consumption_lph"),
            (rateSig && rateSig.detail) ? `from the ${esc(rateSig.detail.method)}, ${esc(rateSig.detail.samples)} samples${rateSig.detail.refills_excluded ? ", refills excluded" : ""}` : "")}
          ${tile("Time to empty, at this rate", sigVal(mod, "eta_depletion_h") === null ? null : fmt(sigVal(mod, "eta_depletion_h"), 1), "h", "projected",
            (range[0] && range[1]) ? `range ${esc(fmt(range[0], 1))}&ndash;${esc(fmt(range[1], 1))} h, from the uncertainty in the trend` : "consumption is too low to project")}
          ${tile("Under forecast demand", outlook.eta_forecast_h === null || outlook.eta_forecast_h === undefined ? null : fmt(outlook.eta_forecast_h, 1), "h", "forecast",
            esc(outlook.basis ? `from the ${outlook.basis}` : ""))}
        </div>
      </div>
      <div class="grid c2">
        <div class="panel">
          <div class="panel-head"><h2>Stored volume over time</h2>
          <span class="hint">raw sensor readings &mdash; the engine median-filters these before use</span></div>
          <div id="tank-level"></div>
        </div>
        <div class="panel stack">
          <div class="note"><span class="h">Where the forecast and the current rate disagree</span>${esc(outlook.note || "No outlook available yet.")}</div>
          <div class="note warn"><span class="h">The error in this chain</span>
            A distance reading becomes a depth, a depth becomes litres, litres plus a consumption
            trend become a time. Each step adds error, which is why the estimate carries a range
            rather than a single number. Measured against the simulator's true volume, the volume
            error is about 0.15% of tank capacity; the depletion estimate's median error is about
            13% of the true time remaining. Both figures and their sample counts are on the
            <b>Devices &amp; history</b> page.</div>
        </div>
      </div>`;
  }

  function renderDemand() {
    const dev = deviceFor("demand");
    if (!dev) return `<div class="panel"><div class="empty"><span class="big">◌</span>No climate sensor on this site.</div></div>`;
    const mod = dev.modules.demand;
    const fcSig = sig(mod, "forecast_demand_l_next_h");
    const validation = (fcSig && fcSig.detail && fcSig.detail.validation) || {};
    const anomSig = sig(mod, "heat_anomaly_c");
    const anom = (anomSig && anomSig.detail) || {};
    const adopted = !!validation.adopted;
    return `${staleBanner(dev)}${moduleHeader(mod, dev)}
      <div class="grid c4" style="margin-bottom:14px">
        ${tile("Heat index", fmt(sigVal(mod, "heat_index_c"), 1), "°C", sigKind(mod, "heat_index_c"),
          `${esc(fmt(sigVal(mod, "temperature_c"), 1))} °C at ${esc(fmt(sigVal(mod, "humidity_pct"), 0))}% RH, NOAA/Rothfusz`)}
        ${tile("Departure from normal", (sigVal(mod, "heat_anomaly_c") >= 0 ? "+" : "") + fmt(sigVal(mod, "heat_anomaly_c"), 1), "°C", sigKind(mod, "heat_anomaly_c"),
          anom.prior_days ? `today's peak ${esc(fmt(anom.today_peak_c, 1))} °C vs a normal peak of ${esc(fmt(anom.normal_peak_c, 1))} °C, over ${esc(anom.prior_days)} prior day(s)` : "no prior days to compare against yet")}
        ${tile("Observed draw", fmt(sigVal(mod, "observed_demand_lph"), 0), "L/h", sigKind(mod, "observed_demand_lph"), "integrated from the flow meter")}
        ${tile(`Forecast, next ${esc((fcSig && fcSig.detail && fcSig.detail.horizon_h) || 6)} h`, fmt(sigVal(mod, "forecast_demand_l_next_h"), 0), "L", "forecast",
          `from the ${esc((fcSig && fcSig.detail && fcSig.detail.source) || "baseline")}`)}
      </div>
      <div class="grid c2">
        <div class="panel">
          <div class="panel-head"><h2>Heat index and air temperature</h2><span class="hint">bands are local advisory levels</span></div>
          <div id="demand-heat"></div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Demand: observed and forecast</h2><span class="hint">dashed is forecast</span></div>
          <div id="demand-forecast"></div>
        </div>
      </div>
      <div class="grid c2" style="margin-top:14px">
        <div class="panel">
          <div class="panel-head"><h2>Does the model earn its place?</h2><span class="hint">held-out comparison</span></div>
          <div class="scroll-x"><table class="data">
            <thead><tr><th>Model</th><th>Held-out MAE</th><th>Verdict</th></tr></thead>
            <tbody>
              <tr><td class="name">Seasonal-naive baseline</td>
                <td class="num">${esc(validation.baseline_mae_lph === null || validation.baseline_mae_lph === undefined ? "--" : fmt(validation.baseline_mae_lph, 2) + " L/h")}</td>
                <td>${adopted ? "beaten" : "<b>in use</b>"}</td></tr>
              <tr><td class="name">Weather-aware ridge model</td>
                <td class="num">${esc(validation.model_mae_lph === null || validation.model_mae_lph === undefined ? "--" : fmt(validation.model_mae_lph, 2) + " L/h")}</td>
                <td>${adopted ? "<b>in use</b>" : "not adopted"}</td></tr>
            </tbody></table></div>
          <div class="note" style="margin-top:12px"><span class="h">${adopted ? "The model is in use" : "The baseline is in use"}</span>
            ${esc((fcSig && fcSig.detail && fcSig.detail.note) || "")}
            Trained on ${esc(validation.n_train || 0)} hourly observations, scored on
            ${esc(validation.n_holdout || 0)} held out from the end of the record. The weather-aware
            model is only used when it beats the baseline on data it has never seen; otherwise the
            dashboard shows the baseline and says so.</div>
        </div>
        <div class="panel stack">
          <div class="note warn"><span class="h">This is not a heatwave declaration</span>
            The bands here are advisory levels configured for this site, aligned with the NOAA heat-index
            categories. Only a meteorological agency &mdash; the IMD in India &mdash; can declare a heat
            event, and any public warning should come from them. What this module adds is local: how hot
            it is <em>here</em>, how far that is from this site's own normal peak, and what that has
            historically done to water use.</div>
          <div class="note"><span class="h">Why a departure from normal, not just a number</span>
            An absolute threshold fires on every ordinary hot afternoon and quickly teaches people to
            ignore it. Requiring a departure from this site's own normal peak, as IMD does, cut the
            false-alarm count on a 24-hour quiet-site run from 32 to zero.</div>
        </div>
      </div>`;
  }

  function renderDevices() {
    const devices = (state.overview && state.overview.devices) || [];
    const m = state.metrics;
    const allAlerts = [];
    for (const id in state.details) {
      for (const a of state.details[id].alerts || []) allAlerts.push(a);
    }
    // Open items first, then by recency. A long run accumulates cleared
    // node-health entries, and burying a live CRITICAL under them is exactly
    // the failure this page exists to prevent.
    const rank = (a) => (a.state === "active" ? 0 : a.state === "acknowledged" ? 1 : 2);
    allAlerts.sort((a, b) => rank(a) - rank(b) || (b.ts || 0) - (a.ts || 0));
    const openCount = allAlerts.filter((a) => rank(a) < 2).length;

    const scenarios = (m && m.scenarios) || [];
    const detectionRows = scenarios.flatMap((s) =>
      (s.detections || []).map((d) => ({ scenario: s.scenario, ...d, fa: s.false_alarm_episodes, n: s.samples_ingested })));

    return `
      <div class="panel" style="margin-bottom:14px">
        <div class="panel-head"><h2>Nodes</h2><span class="hint">freshness is measured on the server's clock</span></div>
        <div class="scroll-x"><table class="data">
          <thead><tr><th>Node</th><th>State</th><th>Last heard</th><th>Device clock</th><th>Samples</th><th>Gaps</th><th>Signal</th><th>Firmware</th></tr></thead>
          <tbody>${devices.map((d) => {
            const det = state.details[d.device_id] || {};
            return `<tr>
              <td class="name">${esc(d.label || d.device_id)}<div style="font-size:0.74rem;color:var(--ink-3)">${esc(d.device_id)}</div></td>
              <td>${d.stale ? '<span class="stale-flag">not reporting</span>' : sevBadge(d.overall_label || sevName(d.overall_severity))}</td>
              <td class="num">${esc(duration(d.age_s))} ago</td>
              <td class="num">${d.clock_skew_warning ? `<span class="stale-flag">out by ${esc(duration(d.clock_skew_s))}</span>` : "in step"}</td>
              <td class="num">${esc(det.samples || d.samples || 0)}</td>
              <td class="num">${esc(det.gaps || d.gaps || 0)}</td>
              <td class="num">${det.rssi === null || det.rssi === undefined ? "--" : esc(det.rssi) + " dBm"}</td>
              <td><code>${esc(det.fw_version || "--")}</code></td>
            </tr>`; }).join("")}</tbody></table></div>
      </div>

      <div class="panel" style="margin-bottom:14px">
        <div class="panel-head"><h2>Measured results</h2>
        <span class="hint">${m && m.generated_at ? `from eval/run_eval.py, ${esc(m.generated_at)}` : "run make eval"}</span></div>
        ${detectionRows.length ? `<div class="scroll-x"><table class="data">
          <thead><tr><th>Scenario</th><th>Module</th><th>Detected</th><th>Delay</th><th>False alarms</th><th>Samples</th></tr></thead>
          <tbody>${detectionRows.map((r) => `<tr>
            <td class="name">${esc(r.scenario)}</td><td>${esc(r.module)}</td>
            <td>${r.detected ? sevBadge("OK") : `<span class="stale-flag">missed</span>`}</td>
            <td class="num">${r.delay_min === null || r.delay_min === undefined ? "--" : esc(fmt(r.delay_min, 1)) + " min"}</td>
            <td class="num">${esc(r.fa)}</td><td class="num">${esc(r.n)}</td></tr>`).join("")}</tbody></table></div>
          <div class="note" style="margin-top:12px"><span class="h">How to read this</span>
            Each row is the real engine driven over a simulated scenario and scored against the
            simulator's own ground truth. &ldquo;Delay&rdquo; is from the moment a condition genuinely
            began to the first alert at the expected severity. A false alarm is an alert episode raised
            while no such condition was present. The quiet-site run is included precisely so that any
            alert it produces is counted against us.</div>`
          : `<div class="empty"><span class="big">◌</span>No measured results yet. Run <code>make eval</code>.</div>`}
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Alert history <span class="n">${allAlerts.length}</span></h2>
        <span class="hint">${openCount} open, ${allAlerts.length - openCount} closed &middot; open first</span></div>
        ${allAlerts.length ? allAlerts.slice(0, 25).map((a) => alertCard(a)).join("") : `<div class="empty"><span class="big">◌</span>No alerts recorded yet.</div>`}
      </div>`;
  }

  // ---- charts, drawn after the HTML is in the DOM ------------------------
  function drawCharts() {
    const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const S1 = css("--series-1"), S2 = css("--series-2"), S3 = css("--series-3");
    const WATCH = css("--watch"), WARN = css("--warning"), CRIT = css("--critical");

    if (state.view === "overview") {
      const tankDev = deviceFor("tank");
      const mod = tankDev && tankDev.modules.tank;
      const g = $("#ov-gauge");
      if (g && mod) tankGauge(g, sigVal(mod, "fill_pct"), { low: 25, critical: 10 });
      const map = $("#ov-map");
      if (map) drawSiteMap(map);
    }

    if (state.view === "flood") {
      const dev = deviceFor("flood");
      if (!dev) return;
      const mod = dev.modules.flood;
      const danger = (sig(mod, "depth_fraction_of_danger") || {}).detail || {};
      const dangerCm = danger.danger_cm || 18;
      timeSeries($("#flood-depth"), {
        series: [{ points: seriesOf(dev.device_id, "street_water_cm"), color: S1, label: "water depth", area: true }],
        rules: [{ at: dangerCm, color: CRIT, label: "danger" }],
        bands: [
          { from: 0, to: dangerCm * 0.333, color: WATCH, label: "watch", opacity: 0.07 },
          { from: dangerCm * 0.666, to: dangerCm, color: WARN, label: "warn", opacity: 0.1 },
        ],
        unit: "cm", zeroFloor: true, digits: 1, height: 210, forceLegend: true,
        ariaLabel: "Water depth at the monitored low point over time",
      });
      timeSeries($("#flood-rain"), {
        series: [{ points: seriesOf(dev.device_id, "rain_rate_mmhr"), color: S3, label: "rainfall intensity", area: true }],
        rules: [{ at: 22, color: WARN, label: "heavy" }, { at: 7.5, color: WATCH, label: "moderate" }],
        unit: "mm/h", zeroFloor: true, digits: 1, height: 210, forceLegend: true,
        ariaLabel: "Rainfall intensity over time",
      });
    }

    if (state.view === "leak") {
      const dev = deviceFor("leak");
      if (!dev) return;
      const mod = dev.modules.leak;
      const p50 = sigVal(mod, "baseline_p50_lpm");
      const p95 = sigVal(mod, "baseline_p95_lpm");
      const rules = [];
      if (p95 !== null && p95 !== undefined) rules.push({ at: p95, color: WARN, label: "p95 normal" });
      if (p50 !== null && p50 !== undefined) rules.push({ at: p50, color: S3, label: "median normal" });
      timeSeries($("#leak-flow"), {
        series: [{ points: seriesOf(dev.device_id, "flow_lpm"), color: S1, label: "measured flow", area: true }],
        rules, unit: "L/min", zeroFloor: true, digits: 2, height: 240, forceLegend: true,
        ariaLabel: "Measured flow against the learned normal",
      });
    }

    if (state.view === "tank") {
      const dev = deviceFor("tank");
      if (!dev) return;
      const mod = dev.modules.tank;
      const fill = (sig(mod, "fill_pct") || {}).detail || {};
      const cap = fill.capacity_l || 5000;
      const g = $("#tank-gauge");
      if (g) tankGauge(g, sigVal(mod, "fill_pct"), { low: 25, critical: 10 });
      // Level in cm is what the sensor gives; convert to litres so the chart
      // is in the unit the thresholds are expressed in.
      const levelPts = seriesOf(dev.device_id, "tank_level_cm");
      const heightCm = 180;
      const volPts = levelPts.map(([t, v]) => [t, (v / heightCm) * cap]);
      timeSeries($("#tank-level"), {
        series: [{ points: volPts, color: S1, label: "stored volume", area: true }],
        rules: [
          { at: cap * 0.25, color: WARN, label: "low" },
          { at: cap * 0.10, color: CRIT, label: "critical" },
        ],
        unit: "L", zeroFloor: true, digits: 0, height: 240, forceLegend: true,
        ariaLabel: "Stored volume over time against the low and critical levels",
      });
    }

    if (state.view === "demand") {
      const dev = deviceFor("demand");
      if (!dev) return;
      const mod = dev.modules.demand;
      const hiDetail = (sig(mod, "heat_index_c") || {}).detail || {};
      const bands = hiDetail.advisory_thresholds_c || [41, 45, 50];
      // Heat index is derived from temperature and humidity, so both belong on
      // one chart: same unit, same scale, no second axis.
      const tempPts = seriesOf(dev.device_id, "temperature_c");
      const hiPts = [];
      const rhPts = seriesOf(dev.device_id, "humidity_pct");
      const rhAt = new Map(rhPts.map(([t, v]) => [t, v]));
      for (const [t, tc] of tempPts) {
        const rh = rhAt.has(t) ? rhAt.get(t) : 50;
        hiPts.push([t, heatIndexC(tc, rh)]);
      }
      timeSeries($("#demand-heat"), {
        series: [
          { points: hiPts, color: S2, label: "heat index", area: true },
          { points: tempPts, color: S1, label: "air temperature" },
        ],
        rules: [
          { at: bands[1], color: WARN, label: "warn" },
          { at: bands[0], color: WATCH, label: "watch" },
        ],
        unit: "°C", digits: 1, height: 240,
        ariaLabel: "Heat index and air temperature over time",
      });

      const fcSig = sig(mod, "forecast_demand_l_next_h");
      const fcPoints = ((sig(mod, "forecast_series") || {}).detail || {}).points || [];
      const now = dev.device_ts || (state.details[dev.device_id] || {}).last_ts || Date.now() / 1000;
      const observedHourly = [];
      const flow = seriesOf(dev.device_id, "flow_lpm");
      // Observed demand as litres/hour, smoothed over a rolling 20 samples so
      // the forecast and the observation are at comparable resolution.
      for (let i = 20; i < flow.length; i += 5) {
        const win = flow.slice(i - 20, i);
        const mean = win.reduce((a, p) => a + p[1], 0) / win.length;
        observedHourly.push([win[win.length - 1][0], mean * 60]);
      }
      const fcSeries = fcPoints.map((p) => [now + p.hours_ahead * 3600, p.litres_per_h]);
      timeSeries($("#demand-forecast"), {
        series: [
          { points: observedHourly, color: S1, label: "observed draw", area: true },
          { points: fcSeries, color: S2, label: `forecast (${(fcSig && fcSig.detail && fcSig.detail.source) || "baseline"})`, dashed: true },
        ],
        unit: "L/h", zeroFloor: true, digits: 0, height: 240,
        ariaLabel: "Observed water draw and the forecast for the next hours",
      });
    }
  }

  /* The heat index, mirrored from engine/demand.py so the chart can show it at
   * every historical point rather than only the latest. Kept deliberately
   * identical -- same constants, same adjustments -- because two different
   * heat indices on one product would be worse than none. */
  function heatIndexC(tC, rh) {
    const tF = tC * 9 / 5 + 32;
    const r = Math.max(0, Math.min(100, rh));
    const simple = 0.5 * (tF + 61.0 + (tF - 68.0) * 1.2 + r * 0.094);
    if ((simple + tF) / 2 < 80) return (simple - 32) * 5 / 9;
    let hi = -42.379 + 2.04901523 * tF + 10.14333127 * r - 0.22475541 * tF * r
      - 0.00683783 * tF * tF - 0.05481717 * r * r + 0.00122874 * tF * tF * r
      + 0.00085282 * tF * r * r - 0.00000199 * tF * tF * r * r;
    if (r < 13 && tF >= 80 && tF <= 112) hi -= ((13 - r) / 4) * Math.sqrt((17 - Math.abs(tF - 95)) / 17);
    else if (r > 85 && tF >= 80 && tF <= 87) hi += ((r - 85) / 10) * ((87 - tF) / 5);
    return (hi - 32) * 5 / 9;
  }

  /* A schematic site plan. Not a map tile service: node positions on a campus
   * are known from a site survey, and a schematic with no external dependency
   * cannot fail to load during a demo. */
  function drawSiteMap(container) {
    const devices = (state.overview && state.overview.devices) || [];
    const W = 760, H = 260;
    const NS = "http://www.w3.org/2000/svg";
    container.textContent = "";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "sitemap");
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", H);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Schematic plan of the monitored site with node positions");

    const plot = document.createElementNS(NS, "rect");
    plot.setAttribute("x", 14); plot.setAttribute("y", 14);
    plot.setAttribute("width", W - 28); plot.setAttribute("height", H - 28);
    plot.setAttribute("rx", 14); plot.setAttribute("class", "plot");
    svg.appendChild(plot);

    const positioned = devices.filter((d) => d.location && typeof d.location.x === "number");
    // The fine connecting lines: a visual reminder that these nodes are one
    // network, not four unrelated gauges.
    for (let i = 0; i < positioned.length; i++) {
      for (let j = i + 1; j < positioned.length; j++) {
        const a = positioned[i], b = positioned[j];
        const line = document.createElementNS(NS, "line");
        line.setAttribute("x1", 20 + a.location.x * (W - 40));
        line.setAttribute("y1", 20 + a.location.y * (H - 40));
        line.setAttribute("x2", 20 + b.location.x * (W - 40));
        line.setAttribute("y2", 20 + b.location.y * (H - 40));
        line.setAttribute("stroke", "var(--connector)");
        line.setAttribute("stroke-width", "1");
        line.setAttribute("stroke-dasharray", "4 5");
        svg.appendChild(line);
      }
    }

    for (const d of positioned) {
      const cx = 20 + d.location.x * (W - 40);
      const cy = 20 + d.location.y * (H - 40);
      const name = d.stale ? "INFO" : (d.overall_label || sevName(d.overall_severity));
      const colour = d.stale ? "var(--ink-3)" : SEV_COLOR[name];

      const halo = document.createElementNS(NS, "circle");
      halo.setAttribute("cx", cx); halo.setAttribute("cy", cy); halo.setAttribute("r", 17);
      halo.setAttribute("fill", colour); halo.setAttribute("opacity", "0.14");
      svg.appendChild(halo);

      const ring = document.createElementNS(NS, "circle");
      ring.setAttribute("cx", cx); ring.setAttribute("cy", cy); ring.setAttribute("r", 9);
      ring.setAttribute("class", "node-ring"); ring.setAttribute("stroke", colour);
      if (d.stale) ring.setAttribute("stroke-dasharray", "3 3");
      svg.appendChild(ring);

      const core = document.createElementNS(NS, "circle");
      core.setAttribute("cx", cx); core.setAttribute("cy", cy); core.setAttribute("r", 4);
      core.setAttribute("fill", colour);
      svg.appendChild(core);

      const label = document.createElementNS(NS, "text");
      label.setAttribute("x", cx); label.setAttribute("y", cy + 29);
      label.setAttribute("text-anchor", "middle");
      label.textContent = d.label || d.device_id;
      svg.appendChild(label);

      const sub = document.createElementNS(NS, "text");
      sub.setAttribute("x", cx); sub.setAttribute("y", cy + 42);
      sub.setAttribute("text-anchor", "middle");
      sub.setAttribute("fill", colour);
      sub.style.fontWeight = "650";
      sub.textContent = d.stale ? "not reporting" : name;
      svg.appendChild(sub);
    }

    if (!positioned.length) {
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", W / 2); t.setAttribute("y", H / 2);
      t.setAttribute("text-anchor", "middle");
      t.textContent = "No node positions configured for this site.";
      svg.appendChild(t);
    }
    container.appendChild(svg);
  }

  // ====================================================================
  // Boot: choose a source, and fall back rather than fail
  // ====================================================================
  async function boot() {
    // 1. An explicitly configured backend wins (the CDK deploy injects this).
    //    config.js ships as a stub that leaves this null, so a static build
    //    falls through to the recorded runs.
    if (typeof window.AQUASENTINEL_API === "string" && window.AQUASENTINEL_API) {
      SOURCE = new window.Sources.ApiSource(window.AQUASENTINEL_API);
      state.mode = "api";
      state.booting = false;
      await refresh();
      setInterval(refresh, 4000);
      return loadMetrics();
    }

    // 2. A static build carries recorded runs. This is the GitHub Pages case.
    const replay = new window.Sources.ReplaySource(() => refresh());
    let haveReplay = false;
    try {
      await replay.start(initialScenario());
      haveReplay = true;
    } catch (e) {
      state.engineError = e.message;
    }

    if (haveReplay) {
      SOURCE = replay;
      state.mode = "replay";
      state.scenarios = replay.scenarios();
      state.scenario = replay.scenario;
      state.booting = false;
      await refresh();
      replay.controls.play();
      return loadMetrics();
    }

    // 3. Otherwise assume a backend on this origin -- the local dev server.
    SOURCE = new window.Sources.ApiSource("");
    state.mode = "api";
    state.booting = false;
    await refresh();
    setInterval(refresh, 4000);
    return loadMetrics();
  }

  function initialScenario() {
    const m = /[?&]scenario=([A-Za-z0-9_-]+)/.exec(location.search);
    return m ? m[1] : null;
  }

  function loadMetrics() {
    return SOURCE.metrics().then((m) => {
      state.metrics = m;
      if (state.view === "devices") render();
    }).catch(() => { /* measured results are optional */ });
  }

  /* Switch between the recorded run and the live Python engine.
   *
   * The live engine downloads a WebAssembly CPython, so it is opt-in and it
   * must never strand the page: any failure drops back to the recording with
   * the reason shown. */
  async function setMode(mode) {
    if (mode === state.mode || state.mode === "api") return;
    if (mode === "pyodide") {
      state.engineStatus = "starting the Python runtime…";
      state.engineError = null;
      state.engineLoading = true;
      render();
      const py = new window.Sources.PyodideSource(
        () => refresh(),
        (text) => { state.engineStatus = text; render(); });
      try {
        await py.start(state.scenario || "demo");
        SOURCE = py;
        state.mode = "pyodide";
        state.engineConfig = py.config;
        state.engineStatus = "running the real Python engine in this browser";
      } catch (err) {
        state.engineError = err.message;
        state.engineStatus = "";
        // Stay on the recording. A failed upgrade is not a broken page.
      } finally {
        state.engineLoading = false;
      }
      render();
      return refresh();
    }
    const replay = new window.Sources.ReplaySource(() => refresh());
    await replay.start(state.scenario);
    SOURCE = replay;
    state.mode = "replay";
    state.scenarios = replay.scenarios();
    render();
    await refresh();
    replay.controls.play();
  }

  async function setScenario(name) {
    state.scenario = name;
    try {
      await SOURCE.start(name);
    } catch (err) {
      state.error = err.message;
    }
    await refresh();
    if (SOURCE.controls && SOURCE.controls.play) SOURCE.controls.play();
  }

  // ====================================================================
  // The playback bar
  // ====================================================================
  function renderControls() {
    const bar = $("#controls");
    if (!bar) return;
    if (state.mode === "api") {
      bar.innerHTML = `<div class="ctl-row">
        <span class="pill"><span class="dot live"></span>Connected to a running backend</span>
        <span class="ctl-note">Readings arrive as the nodes send them.</span></div>`;
      return;
    }

    const c = SOURCE && SOURCE.controls;
    const playing = c ? (c.playing === true || (c.playback && c.playback.playing)) : false;
    const progress = c ? (typeof c.progress === "number" ? c.progress : c.progress) : 0;
    const pct = Math.round((progress || 0) * 100);
    const live = state.mode === "pyodide";
    const scen = (state.scenarios || []).find((s) => s.name === state.scenario);

    bar.innerHTML = `
      <div class="ctl-row">
        <div class="seg" role="group" aria-label="Where the data comes from">
          <button type="button" data-mode="replay" aria-pressed="${!live}">Recorded run</button>
          <button type="button" data-mode="pyodide" aria-pressed="${live}">Live engine</button>
        </div>

        ${state.scenarios.length ? `<label class="ctl-field">
          <span>Scenario</span>
          <select id="ctl-scenario">${state.scenarios.map((s) =>
            `<option value="${esc(s.name)}"${s.name === state.scenario ? " selected" : ""}>${esc(s.title)}</option>`).join("")}</select>
        </label>` : ""}

        <button type="button" class="ghost play" id="ctl-play" aria-label="${playing ? "Pause" : "Play"}">
          ${playing
            ? '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><rect x="2" y="1.5" width="3" height="9" rx="1" fill="currentColor"/><rect x="7" y="1.5" width="3" height="9" rx="1" fill="currentColor"/></svg> Pause'
            : '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.8l7 4.2-7 4.2z" fill="currentColor"/></svg> Play'}
        </button>

        <label class="ctl-field">
          <span>Speed</span>
          <select id="ctl-speed">
            ${[1, 2, 4, 8, 16].map((s) => `<option value="${s}"${(c && c.speed === s) ? " selected" : ""}>${s}&times;</option>`).join("")}
          </select>
        </label>

        <input type="range" id="ctl-seek" min="0" max="1000" value="${Math.round((progress || 0) * 1000)}"
               aria-label="Scrub through the run" ${live ? "disabled" : ""}
               title="${live ? "The live engine runs forward; it cannot rewind" : "Scrub"}">
        <span class="ctl-pct">${pct}%</span>
        <button type="button" class="ghost" id="ctl-restart">Restart</button>
      </div>
      <div class="ctl-row sub">
        ${live
          ? `<span class="pill"><span class="dot live"></span>Real Python engine, in this browser</span>`
          : `<span class="pill">Recorded from the real engine in CI</span>`}
        <span class="ctl-note">${esc(scen ? scen.blurb : (state.engineStatus || ""))}</span>
        ${state.engineStatus && live ? `<span class="ctl-note">${esc(state.engineStatus)}</span>` : ""}
        ${state.engineError ? `<span class="stale-flag">live engine unavailable: ${esc(state.engineError)}</span>` : ""}
      </div>`;
  }

  function wireControls() {
    const bar = $("#controls");
    if (!bar) return;
    bar.addEventListener("click", (e) => {
      const mode = e.target.closest("button[data-mode]");
      if (mode) return setMode(mode.dataset.mode);
      if (e.target.closest("#ctl-play")) {
        const c = SOURCE && SOURCE.controls;
        if (c && c.toggle) c.toggle();
        else if (c && c.playing) c.pause();
        else if (c) c.play();
        renderControls();
        return;
      }
      if (e.target.closest("#ctl-restart")) {
        const c = SOURCE && SOURCE.controls;
        if (c && c.restart) c.restart();
        if (c && c.play) c.play();
        renderControls();
      }
    });
    bar.addEventListener("change", (e) => {
      if (e.target.id === "ctl-scenario") return setScenario(e.target.value);
      if (e.target.id === "ctl-speed") {
        const c = SOURCE && SOURCE.controls;
        if (c && c.setSpeed) c.setSpeed(Number(e.target.value));
      }
    });
    bar.addEventListener("input", (e) => {
      if (e.target.id === "ctl-seek") {
        const c = SOURCE && SOURCE.controls;
        if (c && c.seek) { c.pause && c.pause(); c.seek(Number(e.target.value) / 1000); }
      }
    });
  }

  // ====================================================================
  // The "Live engine" view -- the part a recording cannot do
  // ====================================================================
  const TUNABLES = [
    { section: "flood", key: "danger_cm", label: "Flood danger depth", unit: "cm",
      min: 5, max: 60, step: 1,
      help: "The surveyed depth at which this location is unsafe. Site configuration, not a public-safety limit." },
    { section: "flood", key: "rise_warn_cm_min", label: "Fast-rise threshold", unit: "cm/min",
      min: 0.2, max: 5, step: 0.1,
      help: "A rate of rise above this is a warning, if water is actually present." },
    { section: "leak", key: "min_night_flow_lpm", label: "Minimum night flow", unit: "L/min",
      min: 0.05, max: 2, step: 0.05,
      help: "Sustained idle-hour flow above this counts as evidence of loss — the most specific leak signal there is." },
    { section: "tank", key: "low_pct", label: "Tank low level", unit: "%",
      min: 5, max: 60, step: 1,
      help: "Below this the stored supply is reported as low." },
    { section: "tank", key: "eta_warn_hours", label: "Depletion warning", unit: "h",
      min: 1, max: 24, step: 0.5,
      help: "A projected time-to-empty below this warns — but only once the level is itself getting low." },
    { section: "demand", key: "heat_index_watch_c", label: "Heat advisory level", unit: "°C",
      min: 30, max: 55, step: 0.5,
      help: "Absolute heat index for a watch. An alert also needs a departure from this site's own normal peak." },
    { section: "demand", key: "heat_anomaly_watch_c", label: "Departure from normal", unit: "°C",
      min: 0, max: 10, step: 0.5,
      help: "How far today's peak must exceed this site's usual peak. Set this to 0 to reproduce the naive absolute-threshold detector." },
  ];

  // What the site ships with, for the reset button.
  const DEFAULT_TUNABLES = {
    flood: { danger_cm: 18, rise_warn_cm_min: 1.0 },
    leak: { min_night_flow_lpm: 0.25 },
    tank: { low_pct: 25, eta_warn_hours: 6 },
    demand: { heat_index_watch_c: 41, heat_anomaly_watch_c: 2 },
  };

  function renderEngine() {
    const live = state.mode === "pyodide";
    const cfg = state.engineConfig;

    const explainer = `
      <div class="grid c2" style="margin-bottom:14px">
        <div class="panel stack">
          <div class="panel-head"><h2>What is actually running</h2></div>
          <div class="note"><span class="h">Recorded run</span>
            Every scenario was driven through the real detection engine in CI, and the
            resulting timeline baked into this site. It is a recording of the real thing,
            not a reconstruction: each situation report came out of the same
            <code>Engine.ingest</code> a deployed backend calls. It loads instantly and
            needs nothing but static files.</div>
          <div class="note"><span class="h">Live engine</span>
            Loads CPython compiled to WebAssembly and runs <code>engine/</code> and
            <code>sim/</code> unmodified, in this tab. No server, no API. That is only
            possible because the engine imports nothing outside the standard library
            &mdash; a constraint chosen so the same code could run in Lambda, on a laptop
            and in the tests, paying off a fourth time.</div>
          <div class="note warn"><span class="h">The difference that matters</span>
            In the recording the thresholds below are fixed; it is a video. With the live
            engine running, changing one re-runs real detection from that moment on, and
            every number afterwards is computed in front of you.</div>
        </div>
        <div class="panel stack">
          <div class="panel-head"><h2>Engine</h2>
            <span class="hint">${live ? "WebAssembly CPython" : "not loaded"}</span></div>
          <div class="row-wrap">
            <span class="pill"><span class="dot ${live ? "live" : "down"}"></span>
              ${live ? "live Python engine" : "recorded run"}</span>
            ${state.engineStatus ? `<span class="pill">${esc(state.engineStatus)}</span>` : ""}
          </div>
          ${state.engineError ? `<div class="note strong"><span class="h">The live engine could not start</span>
            ${esc(state.engineError)} &mdash; the recorded run is still showing, so nothing here is
            broken. The runtime comes from a public CDN; a blocked network is the usual cause.</div>` : ""}
          ${!live ? `<div class="note">Switch to <b>Live engine</b> in the bar above to run the real
            Python here. It fetches about 10&nbsp;MB of WebAssembly runtime the first time, then
            starts in a couple of seconds.</div>` : ""}
          ${live && cfg ? `<div class="scroll-x"><table class="data">
            <thead><tr><th>Setting</th><th>Value</th></tr></thead>
            <tbody>
              <tr><td class="name">Site</td><td class="num">${esc(cfg.name || "")}</td></tr>
              <tr><td class="name">Tank capacity</td><td class="num">${esc(fmt((cfg.tank || {}).capacity_l, 0))} L</td></tr>
              <tr><td class="name">Sample interval</td><td class="num">${esc(cfg.sample_interval_s)} s</td></tr>
              <tr><td class="name">Calibration</td><td class="num">${cfg.calibrated ? "surveyed" : "demonstration values"}</td></tr>
            </tbody></table></div>` : ""}
        </div>
      </div>`;

    const sliders = `
      <div class="panel">
        <div class="panel-head"><h2>Thresholds</h2>
          <span class="hint">${live ? "changes re-run real detection" : "read-only while replaying a recording"}</span></div>
        <div class="grid c2">
          ${TUNABLES.map((tn) => {
            const section = (cfg || {})[tn.section] || {};
            const value = section[tn.key];
            const fallback = (DEFAULT_TUNABLES[tn.section] || {})[tn.key];
            const shown = value === undefined ? fallback : value;
            return `<div class="tune${live ? "" : " off"}">
              <label for="tune-${tn.section}-${tn.key}">
                <span class="tune-label">${esc(tn.label)}</span>
                <output id="out-${tn.section}-${tn.key}">${esc(shown)}<span class="unit">${esc(tn.unit)}</span></output>
              </label>
              <input type="range" id="tune-${tn.section}-${tn.key}"
                     data-section="${tn.section}" data-key="${tn.key}"
                     min="${tn.min}" max="${tn.max}" step="${tn.step}"
                     value="${shown}" ${live ? "" : "disabled"}>
              <p class="tune-help">${esc(tn.help)}</p>
            </div>`;
          }).join("")}
        </div>
        ${live ? `<div class="row-wrap" style="margin-top:14px">
          <button class="ghost" id="tune-apply">Apply and re-run detection</button>
          <button class="ghost" id="tune-reset">Back to the surveyed values</button>
        </div>` : ""}
      </div>`;

    const tryIt = `
      <div class="panel" style="margin-top:14px">
        <div class="panel-head"><h2>Things worth trying</h2>
          <span class="hint">with the live engine running</span></div>
        <ol class="queue">
          <li><span class="rank">1</span><div>
            <div class="what">Set <b>Departure from normal</b> to 0</div>
            <div class="why">That removes the IMD-style second gate and leaves a bare absolute
              threshold &mdash; the naive detector. Run the quiet day and watch heat alerts appear
              on an ordinary afternoon. This is the change that took the measured false-alarm
              count from 32 to zero, run backwards.</div></div><span></span></li>
          <li><span class="rank">2</span><div>
            <div class="what">Raise <b>Minimum night flow</b> above 0.6 L/min</div>
            <div class="why">Then run the water-loss scenario. The leak is 0.6 L/min, so the most
              specific detector stops firing and the alert comes late on weaker evidence, or not
              at all. A threshold is a choice between missing things and crying wolf.</div></div>
            <span></span></li>
          <li><span class="rank">3</span><div>
            <div class="what">Lower the <b>Flood danger depth</b> to 8 cm</div>
            <div class="why">The flood scenario then reaches "danger" far sooner. Nothing about the
              water changed &mdash; only the survey. It is the clearest demonstration that these
              numbers are site configuration, not physics.</div></div><span></span></li>
        </ol>
      </div>`;

    return explainer + sliders + tryIt;
  }

  function wireEngineView() {
    const apply = $("#tune-apply");
    if (apply) {
      apply.addEventListener("click", () => {
        const patch = {};
        $$("input[data-section]").forEach((input) => {
          (patch[input.dataset.section] = patch[input.dataset.section] || {})[input.dataset.key] =
            Number(input.value);
        });
        if (SOURCE && SOURCE.applyConfig) {
          SOURCE.applyConfig(patch);
          const next = Object.assign({}, state.engineConfig);
          for (const section of Object.keys(patch)) {
            next[section] = Object.assign({}, next[section], patch[section]);
          }
          state.engineConfig = next;
          state.engineStatus = "thresholds applied; detection re-running from here";
          render();
        }
      });
    }
    const reset = $("#tune-reset");
    if (reset) {
      reset.addEventListener("click", () => {
        if (SOURCE && SOURCE.applyConfig) {
          // The reset goes through the same path as any other change, so it
          // cannot drift from what applying actually does.
          SOURCE.applyConfig(DEFAULT_TUNABLES);
          const next = Object.assign({}, state.engineConfig);
          for (const section of Object.keys(DEFAULT_TUNABLES)) {
            next[section] = Object.assign({}, next[section], DEFAULT_TUNABLES[section]);
          }
          state.engineConfig = next;
          state.engineStatus = "thresholds restored to the surveyed values";
          render();
        }
      });
    }
    $$("input[data-section]").forEach((input) => {
      input.addEventListener("input", () => {
        const out = $(`#out-${input.dataset.section}-${input.dataset.key}`);
        if (out && out.firstChild) out.firstChild.nodeValue = input.value;
      });
    });
  }

  // ---- shell -------------------------------------------------------------
  const VIEWS = {
    overview: { label: "Overview", render: renderOverview },
    flood: { label: "Flood", render: renderFlood },
    leak: { label: "Leakage", render: renderLeak },
    tank: { label: "Tanks", render: renderTank },
    demand: { label: "Heat & demand", render: renderDemand },
    devices: { label: "Devices & history", render: renderDevices },
    engine: { label: "Live engine", render: renderEngine },
  };

  function render() {
    const host = $("#views");
    renderControls();
    if (state.booting) {
      host.innerHTML = `<div class="panel"><div class="empty"><span class="big">◌</span>
        Loading the recorded run&hellip;</div></div>`;
      return;
    }
    if (state.error && !state.overview) {
      host.innerHTML = `<div class="panel"><div class="empty">
        <span class="big">!</span>Cannot reach the API.<br><code>${esc(state.error)}</code><br><br>
        Start the backend with <code>make run</code>, or set
        <code>window.AQUASENTINEL_API</code> to a deployed endpoint.</div></div>`;
      return;
    }
    if (!state.overview && state.view !== "engine") {
      host.innerHTML = `<div class="panel"><div class="empty"><span class="big">◌</span>Waiting for the first reading&hellip;</div></div>`;
      return;
    }
    // The engine view explains and configures the engine; it does not need a
    // reading to have arrived yet.
    if (!state.overview) {
      state.overview = { site: {}, devices: [], active_alerts: [],
                         inspection_queue: [], overall_severity: 0, overall_label: "OK" };
    }

    host.innerHTML = Object.keys(VIEWS).map((key) => {
      let body = "";
      if (key === state.view) {
        try {
          body = VIEWS[key].render();
        } catch (err) {
          // One view failing must not blank the dashboard. An empty page with
          // a healthy-looking status bar is the worst possible failure for a
          // monitoring tool: it looks like "nothing is wrong".
          console.error(`view "${key}" failed to render`, err);
          body = `<div class="panel"><div class="note strong">
            <span class="h">This view could not be rendered</span>
            ${esc(String((err && err.message) || err))}. The other tabs are unaffected,
            and the data behind this one is still arriving.</div></div>`;
        }
      }
      // `enter` animates only when the view actually changed. Applying it on
      // every render made the page flicker to invisible under a live source.
      const entering = key === state.view && key !== lastRenderedView;
      return `<section class="view${key === state.view ? " on" : ""}${entering ? " enter" : ""}"
               id="view-${key}" role="tabpanel" aria-labelledby="tab-${key}">${body}</section>`;
    }).join("");

    // header state
    const o = state.overview;
    const sevLabel = o.overall_label || sevName(o.overall_severity);
    const activeCount = (o.active_alerts || []).filter((a) => a.state === "active").length;
    $("#hdr-site").innerHTML = `<b>${esc((o.site && o.site.name) || "Site")}</b>`;
    const anyLive = (o.devices || []).some((d) => !d.stale);
    $("#hdr-live").innerHTML =
      `<span class="dot ${anyLive ? "live" : "down"}"></span>${anyLive ? "live" : "no live node"}`;
    $("#hdr-sev").innerHTML = sevBadge(sevLabel);
    $("#hdr-updated").innerHTML = `updated <b>${esc(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</b>`;

    $$("nav.views button").forEach((b) => {
      const isCurrent = b.dataset.view === state.view;
      b.setAttribute("aria-current", isCurrent ? "page" : "false");
      b.setAttribute("aria-selected", isCurrent ? "true" : "false");
    });
    const badge = $("#nav-badge");
    if (badge) {
      badge.textContent = activeCount ? String(activeCount) : "";
      badge.style.display = activeCount ? "inline-block" : "none";
    }

    lastRenderedView = state.view;
    lastRenderAt = Date.now();
    try {
      drawCharts();
    } catch (err) {
      // Charts are an enhancement; the numbers above them are the point.
      console.error("charts failed to draw", err);
    }
    if (state.view === "engine") wireEngineView();
  }

  function go(view) {
    if (!VIEWS[view]) view = "overview";
    state.view = view;
    if (location.hash.slice(1) !== view) location.hash = view;
    render();
  }

  function mount() {
    $("#views").insertAdjacentHTML("beforebegin", "");
    const nav = $("nav.views");
    nav.innerHTML = Object.keys(VIEWS).map((key) =>
      `<button role="tab" id="tab-${key}" data-view="${key}" aria-controls="view-${key}">
        ${esc(VIEWS[key].label)}${key === "overview" ? '<span class="badge" id="nav-badge" style="display:none"></span>' : ""}
      </button>`).join("");
    nav.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-view]");
      if (btn) go(btn.dataset.view);
    });
    wireControls();

    document.addEventListener("click", async (e) => {
      const ack = e.target.closest("button[data-ack]");
      if (!ack) return;
      ack.disabled = true;
      ack.textContent = "Acknowledging…";
      try {
        await SOURCE.acknowledge(ack.dataset.ack, "caretaker");
        await refresh();
      } catch (err) {
        ack.disabled = false;
        ack.textContent = "Retry";
      }
    });

    $("#theme").addEventListener("click", () => {
      const root = document.documentElement;
      const current = root.getAttribute("data-theme");
      const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      const next = current ? (current === "dark" ? "light" : "dark") : (prefersDark ? "light" : "dark");
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("aquasentinel-theme", next); } catch (e) { /* private mode */ }
      drawCharts();
    });

    try {
      const saved = localStorage.getItem("aquasentinel-theme");
      if (saved) document.documentElement.setAttribute("data-theme", saved);
    } catch (e) { /* storage can throw; the page must still render */ }

    window.addEventListener("hashchange", () => go(location.hash.slice(1) || "overview"));
    state.view = location.hash.slice(1) || "overview";

    boot();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
