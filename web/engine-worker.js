/* The real AquaSentinel engine, running in this browser.
 *
 * Pyodide is CPython compiled to WebAssembly. It can import `engine` and `sim`
 * straight out of a zip with no build step and no wheels, because those
 * packages import nothing outside the standard library. That constraint was
 * chosen so the same code could run in Lambda, on a laptop and in the tests;
 * running in a browser tab is the same property paying off again.
 *
 * This runs in a Worker so a Python step never blocks the dashboard's frame.
 */
/* eslint-env worker */

/* The runtime is self-hosted beside this file, not fetched from a CDN.
 *
 * A CDN is one more thing that can be blocked, throttled or simply down on
 * the morning someone is looking at this, and the live engine is the point of
 * the page. Self-hosting also makes the site work on a locked-down network,
 * and offline once cached. The CDN remains only as a fallback for a build
 * where vendoring was skipped or failed. */
const PYODIDE_VERSION = "0.26.4";
const PYODIDE_LOCAL = "pyodide/";
const PYODIDE_CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

async function pyodideBase() {
  try {
    const probe = await fetch(`${PYODIDE_LOCAL}pyodide-lock.json`, { cache: "force-cache" });
    if (probe.ok) return PYODIDE_LOCAL;
  } catch (e) { /* fall through to the CDN */ }
  return PYODIDE_CDN;
}

let pyodide = null;
let runner = null;          // the Python driver object
let timer = null;
let playing = false;
let speed = 4;              // engine steps per tick
const TICK_MS = 200;

function status(text, detail) {
  self.postMessage({ type: "status", text, detail });
}

/* The Python side: a small driver that owns the simulator and the engine and
 * hands back one JSON frame per step. Kept here rather than in the repo's
 * packages because it exists only for this transport -- the detection logic
 * it calls is the committed code, unmodified. */
const DRIVER = String.raw`
import json, sys

from engine.config import DEFAULT_SITE, SiteConfig
from engine.pipeline import Engine
from engine.state import DeviceState
from sim import scenarios


def site_config():
    cfg = SiteConfig.from_dict(DEFAULT_SITE.as_dict())
    cfg.tank.capacity_l = 5000.0
    cfg.tank.height_cm = 180.0
    cfg.calibrated = True
    return cfg


class Runner:
    """Drives one scenario through the real engine, one step at a time."""

    def __init__(self):
        self.cfg = site_config()
        self.engine = Engine(self.cfg)
        self.scenario = None
        self.warm = None

    # -- warm-up ------------------------------------------------------
    def warm_up(self):
        """Learn what normal looks like before judging anything abnormal.

        Without this the leak and demand modules have no baseline, and the
        browser would show a less capable system than the recorded run.
        """
        scenarios.use_fixed_epoch()
        scen = scenarios.get("baseline_short")
        nodes = scen.build()
        states = {}
        t, elapsed = scen.start_epoch, 0.0
        while elapsed <= scen.duration_s:
            scen.apply(t, elapsed, nodes)
            for node in nodes.values():
                node.step_physics(t, scen.interval_s)
                payload = node.sample(t)
                st = states.setdefault(payload["device_id"], DeviceState(payload["device_id"]))
                self.engine.ingest(payload, st, now=t)
            t += scen.interval_s
            elapsed += scen.interval_s
        self.warm = {d: s.as_dict() for d, s in states.items()}
        return len(states)

    # -- scenario -----------------------------------------------------
    def load(self, name):
        scenarios.use_fixed_epoch()
        self.scenario = scenarios.get(name)
        self.nodes = self.scenario.build()
        self.states = {}
        if self.warm:
            # Carry the learned baselines, not the timeline they were learned
            # on: mixing two timelines in one buffer corrupts the integrals.
            for dev, raw in self.warm.items():
                old = DeviceState.from_dict(raw)
                fresh = DeviceState(dev)
                fresh.flow_baseline = old.flow_baseline
                fresh.demand_baseline = old.demand_baseline
                fresh.temp_baseline = old.temp_baseline
                fresh.demand_model = {"rows": (old.demand_model.get("rows") or [])[-720:]}
                self.states[dev] = fresh
        self.t = self.scenario.start_epoch
        self.elapsed = 0.0
        self.total = max(1.0, self.scenario.duration_s)
        meta = {}
        for dev_id, info in (self.cfg.devices or {}).items():
            meta[dev_id] = {"device_id": dev_id, "label": info.get("label", dev_id),
                            "location": info.get("location"), "fw_version": ""}
        return json.dumps({
            "site": {"site_id": self.cfg.site_id, "name": self.cfg.name,
                     "calibrated": self.cfg.calibrated,
                     "calibration_note": self.cfg.calibration_note,
                     "sample_interval_s": self.cfg.sample_interval_s,
                     "stale_after_s": self.cfg.stale_after_s},
            "devices": meta,
            "description": self.scenario.description,
            "duration_h": round(self.scenario.duration_s / 3600.0, 2),
        })

    # -- one step -----------------------------------------------------
    def step(self, n=1):
        situations, readings, alerts = {}, {}, []
        for _ in range(int(n)):
            if self.elapsed > self.scenario.duration_s:
                break
            self.scenario.apply(self.t, self.elapsed, self.nodes)
            for node in self.nodes.values():
                node.step_physics(self.t, self.scenario.interval_s)
                payload = node.sample(self.t)
                delivered = []
                node.publish(payload, lambda p: (delivered.append(dict(p)), True)[1])
                for rec in delivered:
                    st = self.states.setdefault(rec["device_id"], DeviceState(rec["device_id"]))
                    res = self.engine.ingest(rec, st, now=self.t)
                    situations[rec["device_id"]] = res.situation
                    good = {}
                    for k, v in (res.reading.metrics if res.reading else {}).items():
                        if int((res.reading.quality or {}).get(k, 0) or 0) < 3:
                            good[k] = round(float(v), 3)
                    readings[rec["device_id"]] = good
                    alerts.extend(a.as_dict() for a in res.alerts)
            self.t += self.scenario.interval_s
            self.elapsed += self.scenario.interval_s
        return json.dumps({
            "t": round(self.t, 1),
            "elapsed_s": round(self.elapsed, 1),
            "progress": min(1.0, self.elapsed / self.total),
            "situations": situations,
            "readings": readings,
            "alerts": alerts,
        })

    # -- live threshold editing ---------------------------------------
    def config(self):
        return json.dumps(self.cfg.as_dict())

    def set_config(self, patch_json):
        """Apply a threshold change and re-evaluate from here on.

        This is the thing a recording cannot do: the numbers on screen after
        this call come from the real detector running with the new settings.
        """
        patch = json.loads(patch_json)
        current = self.cfg.as_dict()
        for section, values in patch.items():
            if isinstance(values, dict) and section in current:
                current[section].update(values)
            else:
                current[section] = values
        self.cfg = SiteConfig.from_dict(current)
        self.engine = Engine(self.cfg)
        return json.dumps(self.cfg.as_dict())


runner = Runner()
`;

async function init() {
  if (pyodide) return;
  const base = await pyodideBase();
  status(base === PYODIDE_LOCAL
    ? "loading the Python runtime…"
    : "fetching the Python runtime from the CDN…");
  importScripts(`${base}pyodide.js`);
  // eslint-disable-next-line no-undef
  pyodide = await loadPyodide({ indexURL: base });

  status("unpacking the engine…");
  const res = await fetch("data/engine-bundle.zip");
  if (!res.ok) throw new Error("engine bundle missing from this site");
  const buf = await res.arrayBuffer();
  pyodide.unpackArchive(buf, "zip");
  // unpackArchive extracts into the current directory, which is not on the
  // path by default inside a worker.
  pyodide.runPython("import sys\nif '.' not in sys.path: sys.path.insert(0, '.')");

  status("starting the engine…");
  pyodide.runPython(DRIVER);
  runner = pyodide.globals.get("runner");

  status("learning what normal looks like…");
  runner.warm_up();

  const cfg = JSON.parse(runner.config());
  self.postMessage({ type: "ready", config: cfg });
  status("live engine ready");
}

function pushState() {
  self.postMessage({
    type: "state",
    playing,
    progress: lastProgress,
  });
}

let lastProgress = 0;

function stepOnce() {
  if (!runner) return;
  let frame;
  try {
    frame = JSON.parse(runner.step(speed));
  } catch (err) {
    self.postMessage({ type: "error", error: String(err) });
    stop();
    return;
  }
  lastProgress = frame.progress;
  self.postMessage(Object.assign({ type: "frame" }, frame));
  if (frame.progress >= 1) {
    stop();
  }
}

function start() {
  if (timer || !runner) return;
  playing = true;
  timer = setInterval(stepOnce, TICK_MS);
  pushState();
}

function stop() {
  playing = false;
  if (timer) clearInterval(timer);
  timer = null;
  pushState();
}

self.onmessage = async (e) => {
  const m = e.data || {};
  try {
    if (m.cmd === "init") {
      await init();
      return;
    }
    if (!runner) return;
    if (m.cmd === "scenario") {
      stop();
      const meta = JSON.parse(runner.load(m.scenario));
      self.postMessage(Object.assign({ type: "meta" }, meta));
      lastProgress = 0;
      stepOnce();            // show something immediately
      start();
      return;
    }
    if (m.cmd === "play") { start(); return; }
    if (m.cmd === "pause") { stop(); return; }
    if (m.cmd === "speed") { speed = Math.max(1, Math.round(m.speed || 1)); return; }
    if (m.cmd === "restart") {
      stop();
      JSON.parse(runner.load(m.scenario || "demo"));
      lastProgress = 0;
      start();
      return;
    }
    if (m.cmd === "config") {
      const cfg = JSON.parse(runner.set_config(JSON.stringify(m.patch || {})));
      self.postMessage({ type: "config", config: cfg });
      return;
    }
  } catch (err) {
    self.postMessage({ type: "error", error: String((err && err.message) || err) });
  }
};
