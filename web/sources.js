/* Where the dashboard gets its data.
 *
 * Three interchangeable sources, all producing exactly the shape the render
 * code already consumes, so there is one rendering path and no "demo mode"
 * branch in the UI:
 *
 *   ApiSource      a running backend -- the local server, or a deployed stack.
 *   ReplaySource   a timeline baked by the real engine in CI. No backend, so
 *                  it works on GitHub Pages, and it loads instantly.
 *   PyodideSource  the actual Python engine, in this browser, via WebAssembly.
 *                  Possible only because engine/ has no dependencies.
 *
 * The replay is not a mock-up: every situation report in it came out of the
 * same `Engine.ingest` the live source runs. It is a recording, not a
 * reconstruction.
 */
(function (global) {
  "use strict";

  /* Undo the build's string interning. Repeated prose -- the same forecast
   * caveat in all 250 frames -- is stored once in a table and referenced by
   * index, which is most of the difference between a 7.6 MB payload and a
   * 2.1 MB one. */
  function rehydrate(node, table) {
    if (Array.isArray(node)) {
      const out = new Array(node.length);
      for (let i = 0; i < node.length; i++) out[i] = rehydrate(node[i], table);
      return out;
    }
    if (node && typeof node === "object") {
      const keys = Object.keys(node);
      if (keys.length === 1 && keys[0] === "$s") return table[node.$s];
      const out = {};
      for (const k of keys) out[k] = rehydrate(node[k], table);
      return out;
    }
    return node;
  }

  function unpack(payload) {
    return payload && payload.$strings
      ? rehydrate(payload.data, payload.$strings)
      : payload;
  }

  const SEV = ["OK", "INFO", "WATCH", "WARNING", "CRITICAL"];

  // ===================================================================
  // ApiSource -- a real backend
  // ===================================================================
  class ApiSource {
    constructor(base) {
      this.base = (base || "").replace(/\/$/, "");
      this.kind = "api";
      this.label = "Live backend";
      this.controls = null;
    }
    async _get(path) {
      const res = await fetch(`${this.base}${path}`, { headers: { accept: "application/json" } });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
      return res.json();
    }
    start() { return Promise.resolve(); }
    overview() { return this._get("/overview"); }
    deviceDetail(id) { return this._get(`/devices/${encodeURIComponent(id)}`); }
    metrics() { return this._get("/metrics").catch(() => null); }
    async acknowledge(id, who) {
      await fetch(`${this.base}/alerts/${encodeURIComponent(id)}/ack`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ who: who || "caretaker" }),
      });
    }
  }

  // ===================================================================
  // Shared playback: a clock over a fixed timeline
  // ===================================================================
  class Playback {
    constructor(onTick) {
      this.frames = [];
      this.index = 0;
      this.playing = false;
      this.speed = 1;          // frames advanced per tick
      this.onTick = onTick;
      this._timer = null;
      this.tickMs = 250;
    }
    load(frames) {
      this.frames = frames || [];
      this.index = 0;
    }
    get frame() { return this.frames[this.index] || null; }
    get progress() { return this.frames.length ? (this.index + 1) / this.frames.length : 0; }
    play() {
      if (this.playing || !this.frames.length) return;
      this.playing = true;
      this._timer = setInterval(() => {
        const next = this.index + Math.max(1, Math.round(this.speed));
        if (next >= this.frames.length) {
          this.index = this.frames.length - 1;
          this.pause();
        } else {
          this.index = next;
        }
        if (this.onTick) this.onTick();
      }, this.tickMs);
    }
    pause() {
      this.playing = false;
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
      if (this.onTick) this.onTick();
    }
    toggle() { this.playing ? this.pause() : this.play(); }
    seek(fraction) {
      if (!this.frames.length) return;
      this.index = Math.max(0, Math.min(this.frames.length - 1,
        Math.round(fraction * (this.frames.length - 1))));
      if (this.onTick) this.onTick();
    }
    setSpeed(s) { this.speed = s; }
    restart() { this.index = 0; if (this.onTick) this.onTick(); }
    atEnd() { return this.index >= this.frames.length - 1; }
  }

  /* Turn one captured frame into the overview the dashboard renders.
   *
   * Shared by the replay and the live engine, so both produce byte-identical
   * structures and a bug can only be in one place.
   */
  function buildOverview(ctx) {
    const { frame, data, now, ackState } = ctx;
    const site = data.site || {};
    const staleAfter = site.stale_after_s || 120;
    const devices = [];
    let worst = 0;

    for (const id of Object.keys(data.devices || {})) {
      const meta = data.devices[id];
      const live = frame && frame.situations ? frame.situations[id] : null;
      if (live) ctx.lastSeen[id] = { t: now, situation: live };
      const seen = ctx.lastSeen[id];
      if (!seen) continue;

      // A device missing from this frame has not delivered a reading. That is
      // exactly what happens during the outage scenario, and carrying its last
      // situation forward while marking it stale is the honest rendering --
      // the same thing the live backend does.
      const age = Math.max(0, now - seen.t);
      const stale = age > staleAfter;
      const sit = seen.situation;
      if (!stale) worst = Math.max(worst, sit.overall_severity || 0);
      devices.push(Object.assign({}, sit, {
        stale,
        age_s: Math.round(age * 10) / 10,
        device_ts: seen.t,
        clock_skew_s: 0,
        clock_skew_warning: false,
        label: meta.label || id,
        location: meta.location || null,
      }));
    }

    const open = (data.alerts || []).filter((a) =>
      a.ts <= now && (!a.closed_ts || a.closed_ts > now));
    const activeAlerts = open.map((a) => Object.assign({}, a, {
      state: ackState.has(a.alert_id) ? "acknowledged" : "active",
      acknowledged_by: ackState.has(a.alert_id) ? "caretaker" : null,
    }));

    const queue = [];
    for (const d of devices) {
      if (d.stale) continue;
      for (const q of d.inspection_queue || []) queue.push(q);
    }
    queue.sort((a, b) => (b.priority || 0) - (a.priority || 0) || (b.severity || 0) - (a.severity || 0));
    queue.forEach((q, i) => { q.rank = i + 1; });

    return {
      site,
      generated_at: now,
      overall_severity: worst,
      overall_label: SEV[worst] || "OK",
      devices,
      inspection_queue: queue.slice(0, 12),
      active_alerts: activeAlerts,
      stale_devices: devices.filter((d) => d.stale).map((d) => d.device_id),
    };
  }

  /* Build the per-device detail view, trimming history to the playhead so the
   * charts reveal themselves as the scenario runs rather than showing the
   * ending from the first frame. */
  function buildDetail(ctx, id) {
    const { data, now } = ctx;
    const meta = (data.devices || {})[id];
    if (!meta) return null;
    const seen = ctx.lastSeen[id];
    const all = (data.series || {})[id] || {};
    const series = {};
    for (const metric of Object.keys(all)) {
      const pts = all[metric].filter((p) => p[0] <= now);
      series[metric] = pts.length > 600 ? pts.slice(pts.length - 600) : pts;
    }
    const age = seen ? Math.max(0, now - seen.t) : Infinity;
    const sit = seen ? seen.situation : {};
    const alerts = (data.alerts || [])
      .filter((a) => a.device_id === id && a.ts <= now)
      .map((a) => Object.assign({}, a, {
        state: ctx.ackState.has(a.alert_id) ? "acknowledged"
          : (a.closed_ts && a.closed_ts <= now) ? (a.close_state || "cleared") : "active",
      }))
      .sort((a, b) => b.ts - a.ts);

    const deviceMod = (sit.modules || {}).device || {};
    const sig = (name) => ((deviceMod.signals || []).find((s) => s.name === name) || {}).value;

    return {
      device_id: id,
      label: meta.label || id,
      location: meta.location || null,
      last_ts: seen ? seen.t : null,
      last_seen_at: seen ? seen.t : null,
      clock_skew_s: 0,
      clock_skew_warning: false,
      age_s: Math.round(age * 10) / 10,
      stale: age > (data.site || {}).stale_after_s,
      fw_version: meta.fw_version || "",
      rssi: sig("rssi_dbm"),
      samples: sig("samples_received"),
      gaps: sig("transmission_gaps"),
      situation: sit,
      series,
      alerts,
    };
  }

  // ===================================================================
  // ReplaySource -- a timeline baked by the real engine in CI
  // ===================================================================
  class ReplaySource {
    constructor(onUpdate) {
      this.kind = "replay";
      this.label = "Recorded run";
      this.data = null;
      this.scenario = null;
      this.manifest = null;
      this.ackState = new Set();
      this.lastSeen = {};
      this.onUpdate = onUpdate;
      this.playback = new Playback(() => this.onUpdate && this.onUpdate());
      this.controls = this.playback;
      this.error = null;
    }

    async start(scenario) {
      if (!this.manifest) {
        const res = await fetch("data/manifest.json");
        if (!res.ok) throw new Error("no recorded runs on this site (data/manifest.json missing)");
        this.manifest = await res.json();
      }
      const want = scenario || (this.manifest.scenarios[0] || {}).name;
      if (want === this.scenario && this.data) return;
      const res = await fetch(`data/${want}.json`);
      if (!res.ok) throw new Error(`recorded run "${want}" is not on this site`);
      this.data = unpack(await res.json());
      this.scenario = want;
      this.ackState = new Set();
      this.lastSeen = {};
      this.playback.load(this.data.frames);
      return this.data;
    }

    get now() {
      const f = this.playback.frame;
      return f ? f.t : (this.data ? this.data.start_epoch : 0);
    }

    _ctx() {
      return { frame: this.playback.frame, data: this.data, now: this.now,
               lastSeen: this.lastSeen, ackState: this.ackState };
    }

    async overview() {
      if (!this.data) await this.start();
      return buildOverview(this._ctx());
    }
    async deviceDetail(id) {
      if (!this.data) await this.start();
      return buildDetail(this._ctx(), id) || {};
    }
    async metrics() {
      const res = await fetch("data/metrics.json").catch(() => null);
      return res && res.ok ? res.json() : null;
    }
    async acknowledge(id) {
      // Acknowledgement is real but local: a recording has no server to tell.
      this.ackState.add(id);
    }
    scenarios() { return (this.manifest && this.manifest.scenarios) || []; }
  }

  // ===================================================================
  // PyodideSource -- the actual Python engine, in this browser
  // ===================================================================
  class PyodideSource {
    constructor(onUpdate, onStatus) {
      this.kind = "pyodide";
      this.label = "Live Python engine";
      this.worker = null;
      this.ready = false;
      this.data = { devices: {}, series: {}, alerts: [], site: {}, frames: [] };
      this.lastSeen = {};
      this.ackState = new Set();
      this.onUpdate = onUpdate;
      this.onStatus = onStatus || (() => {});
      this.scenario = null;
      this.latest = null;
      this.config = null;
      this.error = null;
      this.playing = false;
      this.controls = {
        playing: false,
        progress: 0,
        play: () => this._send({ cmd: "play" }),
        pause: () => this._send({ cmd: "pause" }),
        toggle: () => (this.controls.playing ? this.controls.pause() : this.controls.play()),
        setSpeed: (s) => this._send({ cmd: "speed", speed: s }),
        restart: () => this._send({ cmd: "restart" }),
        seek: null,            // the live engine runs forward; it cannot rewind
        atEnd: () => this.controls.progress >= 1,
      };
    }

    _send(msg) { if (this.worker) this.worker.postMessage(msg); }

    start(scenario) {
      if (this.worker && scenario && scenario !== this.scenario) {
        this.scenario = scenario;
        this._reset();
        this._send({ cmd: "scenario", scenario });
        return Promise.resolve();
      }
      if (this.worker) return Promise.resolve();
      this.scenario = scenario || "demo";
      return new Promise((resolve, reject) => {
        let settled = false;
        // A slow or blocked CDN must not leave the page waiting forever; the
        // caller falls back to the recorded run.
        const timeout = setTimeout(() => {
          if (!settled) { settled = true; reject(new Error("the Python runtime did not load in time")); }
        }, 90000);

        this.worker = new Worker("engine-worker.js");
        this.worker.onerror = (e) => {
          if (!settled) { settled = true; clearTimeout(timeout); reject(new Error(e.message || "worker failed")); }
        };
        this.worker.onmessage = (e) => {
          const m = e.data || {};
          if (m.type === "status") { this.onStatus(m.text, m.detail); return; }
          if (m.type === "error") {
            this.error = m.error;
            if (!settled) { settled = true; clearTimeout(timeout); reject(new Error(m.error)); }
            this.onStatus(`engine error: ${m.error}`);
            return;
          }
          if (m.type === "ready") {
            this.ready = true;
            this.config = m.config;
            if (!settled) { settled = true; clearTimeout(timeout); resolve(); }
            this._send({ cmd: "scenario", scenario: this.scenario });
            return;
          }
          if (m.type === "meta") {
            this._reset();
            this.data.site = m.site;
            this.data.devices = m.devices;
            return;
          }
          if (m.type === "frame") { this._frame(m); return; }
          if (m.type === "state") {
            this.controls.playing = !!m.playing;
            this.controls.progress = m.progress || 0;
            if (this.onUpdate) this.onUpdate();
            return;
          }
          if (m.type === "config") { this.config = m.config; this.onStatus("thresholds applied"); }
        };
        this.worker.postMessage({ cmd: "init" });
      });
    }

    _reset() {
      this.data = { devices: this.data.devices || {}, series: {}, alerts: [],
                    site: this.data.site || {}, frames: [] };
      this.lastSeen = {};
      this.ackState = new Set();
      this.latest = null;
    }

    _frame(m) {
      this.latest = m;
      for (const id of Object.keys(m.situations || {})) {
        this.lastSeen[id] = { t: m.t, situation: m.situations[id] };
      }
      for (const id of Object.keys(m.readings || {})) {
        const devSeries = this.data.series[id] || (this.data.series[id] = {});
        for (const [metric, value] of Object.entries(m.readings[id])) {
          const arr = devSeries[metric] || (devSeries[metric] = []);
          arr.push([m.t, value]);
          if (arr.length > 900) arr.splice(0, arr.length - 900);
        }
      }
      for (const a of m.alerts || []) {
        const existing = this.data.alerts.find((x) => x.alert_id === a.alert_id);
        if (a.state === "cleared" || a.state === "superseded") {
          if (existing) { existing.closed_ts = a.ts; existing.close_state = a.state; }
        } else if (!existing) {
          this.data.alerts.push(Object.assign({}, a, { closed_ts: null, close_state: null }));
        }
      }
      this.controls.progress = m.progress || 0;
      if (this.onUpdate) this.onUpdate();
    }

    get now() { return this.latest ? this.latest.t : 0; }

    _ctx() {
      return { frame: this.latest ? { t: this.latest.t, situations: this.latest.situations } : null,
               data: this.data, now: this.now, lastSeen: this.lastSeen, ackState: this.ackState };
    }

    async overview() {
      if (!this.latest) {
        return { site: this.data.site || {}, generated_at: 0, overall_severity: 0,
                 overall_label: "OK", devices: [], inspection_queue: [],
                 active_alerts: [], stale_devices: [], warming_up: true };
      }
      return buildOverview(this._ctx());
    }
    async deviceDetail(id) { return buildDetail(this._ctx(), id) || {}; }
    async metrics() {
      const res = await fetch("data/metrics.json").catch(() => null);
      return res && res.ok ? res.json() : null;
    }
    async acknowledge(id) { this.ackState.add(id); }
    applyConfig(patch) { this._send({ cmd: "config", patch }); }
    scenarios() { return []; }
  }

  global.Sources = { ApiSource, ReplaySource, PyodideSource, unpack, rehydrate };
})(window);
