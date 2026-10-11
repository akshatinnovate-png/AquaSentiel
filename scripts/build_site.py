#!/usr/bin/env python3
"""Build the static site that GitHub Pages serves.

GitHub Pages has no backend, so the dashboard cannot call a Python API. This
script makes the site work anyway, in two independent ways:

1. **Precomputed replay.** Every scenario is driven through the *real* engine
   here, at build time, and the resulting timeline is baked to JSON. The site
   loads instantly and replays it. This is the path that always works.

2. **The live engine.** `engine/` and `sim/` are zipped into the site so the
   browser can load them into Pyodide and run the actual Python. That is only
   possible because the engine has no third-party dependencies -- there are no
   wheels to find, so CPython-in-WebAssembly runs it unchanged.

Both paths produce the same situation-report shape, so the dashboard renders
them with one code path and the replay is a faithful preview of the live run,
not a mock-up.

    python scripts/build_site.py --out site
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import shutil
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import DEFAULT_SITE, SiteConfig
from engine.model import Severity
from engine.pipeline import Engine
from engine.state import DeviceState
from sim import scenarios

# Scenarios offered on the site, in the order the picker shows them. The demo
# leads because it is the one that tells the whole story.
SITE_SCENARIOS = [
    ("demo", "The full story", "A continuous 30 h day: a leak at midnight, no morning refill, a hot afternoon, then a downpour."),
    ("flood", "Flooding", "Heavy rain onto a blocked drain at the north gate."),
    ("leak", "Water loss", "A 0.6 L/min leak starting in the quiet hours, found by minimum night flow."),
    ("depletion", "Supply running out", "A 42% tank and a refill that never arrives."),
    ("heatwave", "Heat and demand", "A hot spell lifting afternoon demand against a middling tank."),
    ("outage", "Connectivity loss", "A 12-minute uplink outage mid-event; the node buffers and replays."),
    ("sensor_fault", "Sensor failure", "A sensor disconnected, then one frozen at a plausible value."),
    ("baseline_short", "A quiet day", "Nothing should happen. Any alert here is a false alarm."),
]

# Frame budget per scenario. A frame holds every device's full situation
# report, so this is the main driver of page weight; 420 frames is a smooth
# two-minute playback and keeps each scenario well under a megabyte gzipped.
MAX_FRAMES = 250
# Chart history carried in each frame, in samples. The dashboard only ever
# draws a trailing window, so shipping the whole run per frame would be waste.
SERIES_WINDOW = 240


def site_config() -> SiteConfig:
    cfg = SiteConfig.from_dict(DEFAULT_SITE.as_dict())
    cfg.tank.capacity_l = 5000.0
    cfg.tank.height_cm = 180.0
    cfg.calibrated = True
    return cfg


def _round_floats(obj, places: int = 2):
    """Shrink the payload without changing what anything means.

    Three decimals is far finer than any sensor here resolves, and it removes
    the long float tails that otherwise dominate the JSON.
    """
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        r = round(obj, places)
        return int(r) if r == int(r) and abs(r) < 1e15 else r
    if isinstance(obj, dict):
        return {k: _round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, places) for v in obj]
    return obj


def build_scenario(name: str, warm_state: dict[str, dict] | None,
                   max_frames: int = MAX_FRAMES) -> dict:
    """Drive one scenario through the real engine and capture its timeline."""
    scenarios.use_fixed_epoch()
    cfg = site_config()
    engine = Engine(cfg)
    scen = scenarios.get(name)
    nodes = scen.build()

    states: dict[str, DeviceState] = {}
    if warm_state:
        # Carry over only what a warm-up legitimately teaches: the learned
        # baselines. Carrying the rolling series too would interleave two
        # timelines in one buffer.
        for dev, raw in warm_state.items():
            old = DeviceState.from_dict(raw)
            fresh = DeviceState(dev)
            fresh.flow_baseline = old.flow_baseline
            fresh.demand_baseline = old.demand_baseline
            fresh.temp_baseline = old.temp_baseline
            fresh.demand_model = {"rows": (old.demand_model.get("rows") or [])[-720:]}
            states[dev] = fresh

    total_steps = int(scen.duration_s / scen.interval_s) + 1
    frame_every = max(1, total_steps // max_frames)

    frames: list[dict] = []
    alerts: dict[str, dict] = {}
    series: dict[str, dict[str, list]] = {}
    device_meta: dict[str, dict] = {}
    module_errors: list[dict] = []

    t = scen.start_epoch
    elapsed = 0.0
    step = 0
    while elapsed <= scen.duration_s:
        scen.apply(t, elapsed, nodes)
        for node in nodes.values():
            node.step_physics(t, scen.interval_s)

        situations: dict[str, dict] = {}
        for node in nodes.values():
            payload = node.sample(t)
            delivered: list[dict] = []
            # Honour the node's own buffering so the outage scenario replays
            # the real behaviour rather than a shortcut.
            node.publish(payload, lambda p: (delivered.append(dict(p)), True)[1])
            for rec in delivered:
                st = states.setdefault(rec["device_id"], DeviceState(rec["device_id"]))
                res = engine.ingest(rec, st, now=t)
                module_errors.extend(res.module_errors)
                situations[rec["device_id"]] = res.situation
                for a in res.alerts:
                    d = a.as_dict()
                    # An alert episode is an interval, not a per-frame fact.
                    # Recording when it opened and when it closed lets the
                    # client derive "what is open right now" and removes a
                    # copy of the whole alert list from all 450 frames.
                    if d["state"] in ("cleared", "superseded"):
                        existing = alerts.get(d["alert_id"])
                        if existing:
                            existing["closed_ts"] = d["ts"]
                            existing["close_state"] = d["state"]
                        continue
                    if d["alert_id"] in alerts:
                        continue        # same episode, already recorded
                    d["closed_ts"] = None
                    d["close_state"] = None
                    alerts[d["alert_id"]] = d
                # Accumulate the chart history for this device.
                dev_series = series.setdefault(rec["device_id"], {})
                for metric, value in (res.reading.metrics if res.reading else {}).items():
                    if int((res.reading.quality or {}).get(metric, 0) or 0) >= 3:
                        continue   # rejected as physically impossible
                    dev_series.setdefault(metric, []).append([round(t, 1), round(value, 3)])
                meta = (cfg.devices or {}).get(rec["device_id"], {})
                device_meta[rec["device_id"]] = {
                    "device_id": rec["device_id"],
                    "label": meta.get("label", rec["device_id"]),
                    "location": meta.get("location"),
                    "fw_version": rec.get("fw_version"),
                }

        if step % frame_every == 0 and situations:
            frames.append({
                "t": round(t, 1),
                "elapsed_s": round(elapsed, 1),
                "situations": situations,
            })

        t += scen.interval_s
        elapsed += scen.interval_s
        step += 1

    out = {
        "scenario": scen.name,
        "description": scen.description,
        "claim": scen.claim,
        "duration_h": round(scen.duration_s / 3600.0, 2),
        "interval_s": scen.interval_s,
        "start_epoch": scen.start_epoch,
        "tz_offset_h": cfg.timezone_offset_h,
        "site": {
            "site_id": cfg.site_id, "name": cfg.name, "calibrated": cfg.calibrated,
            "calibration_note": cfg.calibration_note,
            "sample_interval_s": cfg.sample_interval_s, "stale_after_s": cfg.stale_after_s,
        },
        "devices": device_meta,
        "series": series,
        "alerts": sorted(alerts.values(), key=lambda a: a.get("ts", 0)),
        "frames": frames,
        "module_errors": module_errors,
        "truth": [
            {"module": tr.module, "label": tr.label, "observed": tr.observed}
            for tr in scen.truth
        ],
    }
    return out, {d: s.as_dict() for d, s in states.items()}


def thin_series(data: dict, keep: int = 3000) -> None:
    """Cap each metric's history. Charts draw a window, not the whole run."""
    for dev, metrics in data["series"].items():
        for metric, points in metrics.items():
            if len(points) > keep:
                stride = len(points) // keep + 1
                metrics[metric] = points[::stride]
    n_frames = max(1, len(data["frames"]))
    for i, frame in enumerate(data["frames"]):
        frame["progress"] = round((i + 1) / n_frames, 6)


# Strings at least this long are worth replacing with a table index. A
# scenario repeats the same explanatory prose in every one of its ~450
# frames -- "Not enough history to test a weather-aware model", and so on --
# which is most of the uncompressed weight.
INTERN_MIN_LEN = 16


def intern_strings(obj):
    """Replace repeated long strings with indices into a table.

    Lossless and generic: the client rehydrates with a three-line walk. This
    is what takes the demo scenario from 7.6 MB of JSON to parse down to
    something a phone does not notice, without changing a single value.

    Encoded as {"$s": n}, which cannot collide with real data because no
    payload key in this project begins with a dollar sign.
    """
    counts: dict[str, int] = {}

    def count(node):
        if isinstance(node, str):
            if len(node) >= INTERN_MIN_LEN:
                counts[node] = counts.get(node, 0) + 1
        elif isinstance(node, dict):
            for v in node.values():
                count(v)
        elif isinstance(node, list):
            for v in node:
                count(v)

    count(obj)
    # Only intern strings that actually repeat; a one-off is cheaper inline.
    table = [s for s, n in counts.items() if n > 1]
    table.sort(key=lambda s: -counts[s] * len(s))
    index = {s: i for i, s in enumerate(table)}

    def sub(node):
        if isinstance(node, str):
            i = index.get(node)
            return {"$s": i} if i is not None else node
        if isinstance(node, dict):
            return {k: sub(v) for k, v in node.items()}
        if isinstance(node, list):
            return [sub(v) for v in node]
        return node

    return {"$strings": table, "data": sub(obj)}


def _rehydrate(node, table):
    """The client's rehydration, mirrored here so the build can verify it."""
    if isinstance(node, dict):
        if set(node.keys()) == {"$s"}:
            return table[node["$s"]]
        return {k: _rehydrate(v, table) for k, v in node.items()}
    if isinstance(node, list):
        return [_rehydrate(v, table) for v in node]
    return node


def write_json(path: pathlib.Path, obj, *, pretty: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_round_floats(obj), separators=(",", ":") if not pretty else None,
                      indent=2 if pretty else None, default=str)
    path.write_text(text)
    return len(text.encode())


# The Pyodide runtime is vendored into the site rather than loaded from a
# public CDN at view time. A CDN is one more thing that can be blocked,
# throttled or simply down on the morning someone is judging this, and the
# runtime is the headline feature. Self-hosting also makes the whole site work
# offline and removes a cross-origin dependency.
PYODIDE_VERSION = "0.26.4"
PYODIDE_URL = (f"https://github.com/pyodide/pyodide/releases/download/"
               f"{PYODIDE_VERSION}/pyodide-core-{PYODIDE_VERSION}.tar.bz2")
# Only these are needed to boot a bare interpreter with no third-party
# packages, which is all the engine requires.
PYODIDE_FILES = ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm",
                 "python_stdlib.zip", "pyodide-lock.json")


# A stalled download must not hold a deploy open indefinitely. The first
# attempt at this hung a Pages build on a step that takes under a minute when
# it works, so the whole fetch is bounded in wall-clock time and the build
# continues without the runtime rather than waiting on it.
PYODIDE_DOWNLOAD_BUDGET_S = 150.0
PYODIDE_ATTEMPTS = 2


def _download(url: str) -> bytes:
    """Fetch a URL with a hard overall deadline and a couple of retries."""
    import socket
    import time as _time
    import urllib.error
    import urllib.request

    deadline = _time.monotonic() + PYODIDE_DOWNLOAD_BUDGET_S
    last = None
    for attempt in range(1, PYODIDE_ATTEMPTS + 1):
        remaining = deadline - _time.monotonic()
        if remaining <= 5:
            break
        # An explicit User-Agent: GitHub throttles unidentified clients, which
        # is a plausible cause of a download that crawls rather than fails.
        req = urllib.request.Request(url, headers={
            "User-Agent": "AquaSentinel-site-build/1.0",
            "Accept": "application/octet-stream",
        })
        try:
            chunks, total = [], 0
            with urllib.request.urlopen(req, timeout=min(60.0, remaining)) as resp:
                while True:
                    if _time.monotonic() > deadline:
                        raise TimeoutError(
                            f"exceeded the {PYODIDE_DOWNLOAD_BUDGET_S:.0f}s budget "
                            f"after {total / 1024 / 1024:.1f} MB")
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            if total < 1_000_000:
                raise OSError(f"download was only {total} bytes; expected several MB")
            return b"".join(chunks)
        except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as exc:
            last = exc
            print(f"[build] pyodide: attempt {attempt} failed "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
    raise last or TimeoutError("download budget exhausted")


def vendor_pyodide(out_dir: pathlib.Path, cache: pathlib.Path | None = None) -> int:
    """Download the Pyodide core distribution into the site.

    Returns the total bytes written, or 0 if it could not be fetched -- in
    which case the site still works: the dashboard falls back to the recorded
    runs and says why the live engine is unavailable.
    """
    import io
    import tarfile
    import urllib.request

    target = out_dir / "pyodide"
    target.mkdir(parents=True, exist_ok=True)

    cached = cache / f"pyodide-core-{PYODIDE_VERSION}.tar.bz2" if cache else None
    blob = None
    if cached and cached.exists() and cached.stat().st_size > 1_000_000:
        blob = cached.read_bytes()
        print(f"[build] pyodide: using cached {cached.name}", file=sys.stderr)
    else:
        try:
            print(f"[build] pyodide: downloading {PYODIDE_VERSION}…", file=sys.stderr)
            blob = _download(PYODIDE_URL)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(blob)
        except Exception as exc:
            print(f"[build] pyodide: NOT vendored ({type(exc).__name__}: {exc}). "
                  f"The site still works; the live engine falls back to the CDN, and "
                  f"to the recorded runs if that is unreachable too.",
                  file=sys.stderr)
            return 0

    total = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:bz2") as tar:
        for name in PYODIDE_FILES:
            member = tar.getmember(f"pyodide/{name}")
            src = tar.extractfile(member)
            if src is None:
                continue
            data = src.read()
            (target / name).write_bytes(data)
            total += len(data)
    return total


def bundle_engine(out_dir: pathlib.Path) -> int:
    """Zip the engine and simulator for Pyodide to unpack in the browser.

    No wheels, no compiled extensions, no `micropip` -- the packages are pure
    standard-library Python, which is the whole reason this is possible at all.
    """
    target = out_dir / "engine-bundle.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for package in ("engine", "sim"):
            for path in sorted((ROOT / package).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                z.write(path, path.relative_to(ROOT).as_posix())
    return target.stat().st_size


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the AquaSentinel static site")
    ap.add_argument("--out", default="site")
    ap.add_argument("--scenarios", nargs="*", default=[s[0] for s in SITE_SCENARIOS])
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES)
    ap.add_argument("--no-pyodide", dest="pyodide", action="store_false",
                    help="skip vendoring the Python runtime (faster local builds)")
    ap.add_argument("--pyodide-cache", default=".cache",
                    help="directory to cache the downloaded runtime in")
    args = ap.parse_args(argv)

    out = pathlib.Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # -- static assets --------------------------------------------------
    for item in sorted((ROOT / "web").iterdir()):
        if item.is_file():
            shutil.copy2(item, out / item.name)
    (out / ".nojekyll").write_text("")   # or Jekyll eats files beginning with _

    # The CDK deploy writes a real config.js pointing at its API endpoint. The
    # static site has no backend, so ship a stub: without it the page requests
    # a file that is not there and logs a 404 on every load, which buries real
    # errors in the console.
    (out / "config.js").write_text(
        "/* No backend on this build: the dashboard falls back to the recorded\n"
        " * runs in data/, or to the Python engine running in your browser.\n"
        " * A CDK deploy overwrites this file with its API endpoint. */\n"
        "window.AQUASENTINEL_API = window.AQUASENTINEL_API || null;\n"
    )

    # -- warm up once, and reuse it for every scenario -------------------
    print("[build] warm-up run (learning what normal looks like)...", file=sys.stderr)
    _, warm = build_scenario("baseline_short", None, args.max_frames)

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine_version": __import__("engine").__version__,
        "scenarios": [],
    }
    total_bytes = 0
    titles = {s[0]: (s[1], s[2]) for s in SITE_SCENARIOS}

    for name in args.scenarios:
        t0 = time.time()
        data, _ = build_scenario(name, warm if name != "baseline_short" else None,
                                 args.max_frames)
        thin_series(data)
        rounded = _round_floats(data)
        packed = intern_strings(rounded)
        # Prove the compression is lossless before shipping it. A silent
        # corruption here would show up as a wrong number on the dashboard
        # with nothing to trace it to.
        assert _rehydrate(packed["data"], packed["$strings"]) == rounded, \
            f"string interning changed the contents of {name}"
        size = write_json(out / "data" / f"{name}.json", packed)
        total_bytes += size
        gz = len(gzip.compress((out / "data" / f"{name}.json").read_bytes(), 9))
        title, blurb = titles.get(name, (name, data["description"]))
        manifest["scenarios"].append({
            "name": name, "title": title, "blurb": blurb,
            "description": data["description"], "claim": data["claim"],
            "duration_h": data["duration_h"], "interval_s": data["interval_s"],
            "frames": len(data["frames"]),
            "bytes": size, "gzip_bytes": gz,
        })
        errs = len(data["module_errors"])
        print(f"[build] {name:16s} {len(data['frames']):4d} frames  "
              f"{size/1024:7.0f} KB ({gz/1024:6.0f} KB gzipped)  "
              f"{time.time()-t0:5.1f}s" + (f"  !! {errs} MODULE ERRORS" if errs else ""),
              file=sys.stderr)
        if errs:
            print(f"[build] ERROR: {name} had module errors: {data['module_errors'][:2]}",
                  file=sys.stderr)
            return 1

    # -- measured results, so the site can show them ---------------------
    metrics_src = ROOT / "eval" / "results" / "metrics.json"
    if metrics_src.exists():
        shutil.copy2(metrics_src, out / "data" / "metrics.json")
        manifest["has_metrics"] = True
    report_src = ROOT / "eval" / "results" / "REPORT.md"
    if report_src.exists():
        shutil.copy2(report_src, out / "data" / "REPORT.md")

    # -- the live engine bundle ------------------------------------------
    bundle = bundle_engine(out / "data")
    manifest["engine_bundle_bytes"] = bundle
    print(f"[build] engine bundle  {bundle/1024:7.0f} KB", file=sys.stderr)

    if args.pyodide:
        vendored = vendor_pyodide(out, pathlib.Path(args.pyodide_cache) if args.pyodide_cache else None)
        manifest["pyodide"] = {
            "vendored": bool(vendored),
            "version": PYODIDE_VERSION,
            "bytes": vendored,
        }
        if vendored:
            print(f"[build] pyodide        {vendored/1024/1024:7.1f} MB (self-hosted)",
                  file=sys.stderr)
    else:
        manifest["pyodide"] = {"vendored": False, "version": PYODIDE_VERSION, "bytes": 0}

    write_json(out / "data" / "manifest.json", manifest, pretty=True)

    print(f"[build] wrote {out}/  ({total_bytes/1024/1024:.2f} MB of scenario data)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
