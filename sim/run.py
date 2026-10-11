#!/usr/bin/env python3
"""Run the virtual rig.

    # watch the firmware output
    python -m sim.run --scenario demo --transport stdout --speed 60

    # feed the local dashboard backend
    python -m sim.run --scenario demo --transport http --url http://127.0.0.1:8080 --speed 30

    # feed AWS IoT Core (the production path)
    python -m sim.run --scenario demo --transport iot --speed 30

`--speed` is a wall-clock multiplier: `--speed 60` runs one simulated minute
per real second.  `--speed 0` runs as fast as the CPU allows, which is what the
evaluation harness uses.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from sim import scenarios
from sim.transports import build as build_transport


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sim.run", description="AquaSentinel virtual hardware rig")
    ap.add_argument("--scenario", default="demo", choices=sorted(scenarios.REGISTRY),
                    help="which scenario to run")
    ap.add_argument("--transport", default="stdout", choices=["stdout", "http", "iot", "null"])
    ap.add_argument("--url", help="ingest base URL for the http transport")
    ap.add_argument("--api-key", help="API key for the http transport")
    ap.add_argument("--endpoint", help="AWS IoT Core ATS endpoint")
    ap.add_argument("--cert"); ap.add_argument("--key"); ap.add_argument("--ca")
    ap.add_argument("--topic", default="aquasentinel/telemetry")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="simulated seconds per real second; 0 = as fast as possible")
    ap.add_argument("--interval", type=float, default=None, help="override the sample interval (s)")
    ap.add_argument("--duration", type=float, default=None, help="override the duration (s)")
    ap.add_argument("--pretty", action="store_true", help="pretty-print stdout payloads")
    ap.add_argument("--quiet", action="store_true", help="suppress the progress line")
    ap.add_argument("--fixed-clock", action="store_true",
                    help="keep the fixed reference date instead of anchoring to today")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args(argv)

    if args.list:
        for name in sorted(scenarios.REGISTRY):
            s = scenarios.get(name)
            print(f"{name:14s} {s.duration_s/3600:6.2f} h  {s.description}")
            if s.claim:
                print(f"{'':14s}        claim: {s.claim}")
        return 0

    scen = scenarios.get(args.scenario)
    if not args.fixed_clock:
        scenarios.anchor_to_now(scen.start_hour_local)
        scen = scenarios.get(args.scenario)
    interval = args.interval or scen.interval_s
    duration = args.duration or scen.duration_s
    nodes = scen.build()
    transport = build_transport(
        args.transport, url=args.url, api_key=args.api_key, pretty=args.pretty,
        endpoint=args.endpoint, cert=args.cert, key=args.key, ca=args.ca,
        topic=args.topic, client_id=f"aquasentinel-sim-{args.scenario}",
    )

    if not args.quiet:
        print(f"[sim] scenario={scen.name} duration={duration/3600:.2f}h interval={interval}s "
              f"transport={args.transport} speed={'max' if args.speed == 0 else f'{args.speed:g}x'}",
              file=sys.stderr)
        print(f"[sim] {scen.description}", file=sys.stderr)

    t = scen.start_epoch
    elapsed = 0.0
    sent = failed = 0
    wall_start = time.time()
    try:
        while elapsed <= duration:
            scen.apply(t, elapsed, nodes)
            for node in nodes.values():
                node.step_physics(t, interval)
                payload = node.sample(t)
                n_sent, buffered = node.publish(payload, transport.send)
                sent += n_sent
                if n_sent == 0:
                    failed += 1

            if args.speed > 0:
                # Keep wall-clock pace with the simulated clock.
                target = wall_start + elapsed / args.speed
                nap = target - time.time()
                if nap > 0:
                    time.sleep(nap)
            if not args.quiet and int(elapsed) % 600 == 0:
                n1 = next(iter(nodes.values()))
                print(f"[sim] t+{elapsed/60:7.1f} min  sent={sent} buffered={len(n1.buffer)} "
                      f"online={n1.online}", file=sys.stderr)
            t += interval
            elapsed += interval
    except KeyboardInterrupt:
        print("\n[sim] interrupted", file=sys.stderr)
    finally:
        transport.close()

    if not args.quiet:
        print(f"[sim] done: {sent} records sent, {failed} publish attempts deferred to the node buffer",
              file=sys.stderr)
        for node in nodes.values():
            print(f"[sim] {json.dumps(node.status())}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
