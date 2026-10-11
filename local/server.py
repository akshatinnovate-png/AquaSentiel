#!/usr/bin/env python3
"""The whole product, offline, in one command.

    python -m local.server            # http://127.0.0.1:8080

Serves the same routes as the deployed API Gateway and the dashboard as static
files, backed by the in-memory store. Standard library only -- no pip install,
no AWS account, nothing to go wrong five minutes before a demo. That is the
point of it: the cloud deployment is the real architecture, and this is the
copy that cannot fail on conference Wi-Fi.

With `--simulate` it also runs the virtual rig in a background thread, so a
single command gives you live data in a browser.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import DEFAULT_SITE
from services.common.app import Service
from services.common.repo import MemoryRepo

WEB_DIR = ROOT / "web"

# One service shared by every request thread. The in-memory store is mutated
# from both the HTTP threads and the simulator thread, so it needs a lock.
# The simulated clock, advanced by the simulator thread. The service reads it
# so that freshness is judged on the same clock the readings carry.
SIM_CLOCK: dict = {"t": None}


def service_clock() -> float:
    t = SIM_CLOCK["t"]
    return t if t is not None else time.time()


SERVICE = Service(repo=MemoryRepo(), cfg=DEFAULT_SITE, clock=service_clock)
LOCK = threading.Lock()
SIM_STATUS: dict = {"running": False, "scenario": None, "sent": 0, "elapsed_s": 0.0,
                    "speed": 0, "nodes": {}}


class Handler(BaseHTTPRequestHandler):
    server_version = "AquaSentinel/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("AQUASENTINEL_VERBOSE"):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type,x-api-key")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            with LOCK:
                if path in ("/health", "/api/health"):
                    return self._json(200, {"ok": True, "service": "aquasentinel-local",
                                            "engine": __import__("engine").__version__,
                                            "store": "in-memory"})
                if path in ("/overview", "/api/overview"):
                    return self._json(200, SERVICE.overview())
                if path in ("/devices", "/api/devices"):
                    return self._json(200, {"devices": SERVICE.overview()["devices"]})
                if "/devices/" in path:
                    device_id = path.split("/devices/", 1)[1]
                    detail = SERVICE.device_detail(device_id, window_s=float(qs.get("window_s") or 7200))
                    return self._json(200 if detail else 404, detail or {"error": "unknown device"})
                if path in ("/alerts", "/api/alerts"):
                    return self._json(200, SERVICE.alerts(state=qs.get("state"),
                                                          device_id=qs.get("device"),
                                                          limit=int(qs.get("limit") or 200)))
                if path in ("/config", "/api/config"):
                    return self._json(200, SERVICE.config().as_dict())
                if path in ("/metrics", "/api/metrics"):
                    return self._json(200, SERVICE.metrics())
                if path in ("/sim/status", "/api/sim/status"):
                    return self._json(200, dict(SIM_STATUS))
            return self._static(path)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "body is not valid JSON"})

        try:
            with LOCK:
                if path in ("/ingest", "/api/ingest"):
                    records = body if isinstance(body, list) else [body]
                    out = [SERVICE.ingest(r) for r in records]
                    return self._json(200, {"ok": True, "ingested": len(out), "results": out})
                if "/alerts/" in path and path.endswith("/ack"):
                    alert_id = path.split("/alerts/", 1)[1][: -len("/ack")]
                    out = SERVICE.acknowledge(alert_id, body.get("who", "caretaker"))
                    return self._json(200 if out["ok"] else 404, out)
                if path in ("/config", "/api/config"):
                    return self._json(200, SERVICE.save_config(body).as_dict())
            return self._json(404, {"error": f"no route for POST {path}"})
        except Exception as exc:
            import traceback

            traceback.print_exc()
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- static files ----------------------------------------------------
    def _static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        # Refuse to serve anything outside web/, however the path is spelled.
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._json(404, {"error": f"not found: {path}"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)


def run_simulator(scenario: str, speed: float, interval: float | None) -> None:
    """Drive the virtual rig straight into the service, in-process."""
    from sim import scenarios

    scen = scenarios.get(scenario)
    step = interval or scen.interval_s
    nodes = scen.build()
    SIM_STATUS.update({"running": True, "scenario": scen.name, "speed": speed,
                       "description": scen.description, "sent": 0})
    t = scen.start_epoch
    SIM_CLOCK["t"] = t
    elapsed = 0.0
    wall_start = time.time()
    sent = 0
    print(f"[sim] {scen.name}: {scen.description}", file=sys.stderr)

    while elapsed <= scen.duration_s:
        SIM_CLOCK["t"] = t
        scen.apply(t, elapsed, nodes)
        for node in nodes.values():
            node.step_physics(t, step)
            payload = node.sample(t)

            def deliver(p: dict) -> bool:
                with LOCK:
                    # `now` is the simulated clock, so freshness checks compare
                    # like with like instead of calling every reading stale.
                    SERVICE.ingest(p, now=t)
                return True

            n_sent, _ = node.publish(payload, deliver)
            sent += n_sent
        SIM_STATUS.update({"sent": sent, "elapsed_s": round(elapsed, 1),
                           "nodes": {k: v.status() for k, v in nodes.items()}})
        if speed > 0:
            nap = (wall_start + elapsed / speed) - time.time()
            if nap > 0:
                time.sleep(nap)
        t += step
        elapsed += step

    SIM_STATUS["running"] = False
    # Freeze the clock at the scenario's end rather than snapping back to wall
    # time, so the final state stays readable instead of instantly going stale.
    SIM_CLOCK["t"] = t
    print(f"[sim] finished: {sent} records ingested", file=sys.stderr)


def carry_baselines_only() -> None:
    """Keep what the warmup learned; discard the timeline it learned it on.

    A warmup's job is to give the leak and demand modules a sense of normal
    before anything is judged abnormal. Its rolling series, latches and
    alerts belong to a different run: leaving them in place interleaved two
    timelines in the same buffers, and the consumption integral then
    double-counted into a 2398 L/h draw on a 5000 L tank. So the baselines
    cross over and nothing else does.
    """
    from engine.state import DeviceState
    from services.common.app import STATE_KEY

    with LOCK:
        for item in SERVICE.repo.list_devices():
            raw = item.get(STATE_KEY)
            if not isinstance(raw, dict) or not raw:
                continue
            old = DeviceState.from_dict(raw)
            fresh = DeviceState(old.device_id)
            fresh.flow_baseline = old.flow_baseline
            fresh.demand_baseline = old.demand_baseline
            fresh.temp_baseline = old.temp_baseline
            fresh.demand_model = {"rows": (old.demand_model.get("rows") or [])[-720:]}
            SERVICE.repo.put_device({**item, STATE_KEY: fresh.as_dict(),
                                     "situation": {}, "samples": 0, "gaps": 0})
        SERVICE.repo.alerts.clear()
        SERVICE.repo.readings.clear()
    print("[warmup] learned baselines carried over; timeline reset", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AquaSentinel local server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--simulate", metavar="SCENARIO", nargs="?", const="demo",
                    help="also run the virtual rig (default scenario: demo)")
    ap.add_argument("--speed", type=float, default=600.0,
                    help="simulated seconds per real second (default 600, i.e. 10 min/s)")
    ap.add_argument("--interval", type=float, default=None, help="override the sample interval")
    ap.add_argument("--fixed-clock", action="store_true",
                    help="keep the simulator's fixed reference date instead of anchoring to today")
    ap.add_argument("--warmup", metavar="SCENARIO", default=None,
                    help="run this scenario at full speed first, to establish baselines")
    args = ap.parse_args(argv)

    # A live run anchors the simulated clock to today, so the dashboard does
    # not spend the whole demo reporting a year of clock skew.
    from sim import scenarios as _scen

    if args.fixed_clock:
        _scen.use_fixed_epoch()
    elif args.simulate or args.warmup:
        _scen.anchor_to_now(_scen.get(args.simulate or args.warmup).start_hour_local)

    if args.warmup:
        print(f"[warmup] running {args.warmup} at full speed to learn what normal looks like...",
              file=sys.stderr)
        run_simulator(args.warmup, 0.0, args.interval)
        carry_baselines_only()

    if args.simulate:
        threading.Thread(target=run_simulator, args=(args.simulate, args.speed, args.interval),
                         daemon=True).start()

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    with Server((args.host, args.port), Handler) as httpd:
        print(f"[server] AquaSentinel on http://{args.host}:{args.port}", file=sys.stderr)
        print(f"[server] dashboard: http://{args.host}:{args.port}/", file=sys.stderr)
        print(f"[server] api:       http://{args.host}:{args.port}/overview", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[server] stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
