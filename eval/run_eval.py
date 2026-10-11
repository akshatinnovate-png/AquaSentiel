#!/usr/bin/env python3
"""Measure what the engine actually does, and report it honestly.

For each scenario we drive the simulated rig through the real engine and score:

  * **detection delay** -- simulated seconds from the moment a condition truly
    begins (the scenario's declared ground truth) to the first alert at or
    above the expected severity;
  * **missed detections** -- truth windows that produced no qualifying alert;
  * **false alarms** -- alerts raised while no truth window for that module is
    open, counted per episode rather than per sample;
  * **estimate error** -- for the tank, absolute error of the reported volume
    against the simulator's true volume, and of the depletion ETA against the
    time the tank truly ran dry;
  * **forecast skill** -- held-out MAE of the weather-aware demand model
    against the seasonal-naive baseline it has to beat.

A number this harness will not produce is an accuracy percentage with no
denominator.  Every figure below carries its trial count.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.config import DEFAULT_SITE, SiteConfig
from engine.model import Severity
from engine.pipeline import Engine
from engine.state import DeviceState
from sim import scenarios
from sim.sensors import TankModel

RESULTS_DIR = pathlib.Path(__file__).parent / "results"


def site_for_sim() -> SiteConfig:
    """The site config matching the simulated rig's geometry.

    Deliberately explicit: the engine must be told the tank it is watching.
    Getting this wrong is the most common way a level reading becomes a wrong
    volume, so the harness sets it rather than letting a default drift.
    """
    cfg = SiteConfig.from_dict(DEFAULT_SITE.as_dict())
    cfg.tank.capacity_l = 5000.0
    cfg.tank.height_cm = 180.0
    cfg.calibrated = True
    return cfg


class ScenarioRun:
    """One scenario driven through the engine, with scoring."""

    # A depletion ETA is only a claim about the future while water is actually
    # being drawn. Roughly a quarter of this site's mean daytime draw.
    MEANINGFUL_DRAW_LPH = 50.0

    def __init__(self, scen: scenarios.Scenario, cfg: SiteConfig, warmup: "ScenarioRun | None" = None):
        self.scen = scen
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.nodes = scen.build()
        self.states: dict[str, DeviceState] = {}
        if warmup is not None:
            # Carry learned baselines across from a warm-up run.  A leak
            # detector with no idea what normal looks like is not a detector,
            # and pretending otherwise would flatter the numbers.
            for dev, st in warmup.states.items():
                fresh = DeviceState(dev)
                fresh.flow_baseline = st.flow_baseline
                fresh.demand_baseline = st.demand_baseline
                fresh.temp_baseline = st.temp_baseline
                fresh.demand_model = {"rows": (st.demand_model.get("rows") or [])[-600:]}
                self.states[dev] = fresh

        self.first_detect: dict[str, float] = {}      # module -> elapsed s
        self.alert_episodes: list[dict] = []
        self.samples = 0
        self.rejected = 0
        self.truth_log: list[dict] = []
        self.tank_errors: list[tuple[float, float, float]] = []   # elapsed, reported, true
        self.eta_snapshots: list[tuple[float, float, float]] = []  # elapsed, eta_h, rate_lph
        self.true_empty_at: float | None = None
        self.last_situation: dict = {}
        self.demand_validation: dict = {}
        self.buffered_replays = 0
        self.unknown_when_faulted = 0
        self.faulted_samples = 0
        # For predicate-based truth: when did each condition *genuinely* begin,
        # and is it open right now? Read from the simulator's internal state,
        # never from sensor output, so noise cannot move the ground truth.
        self.observed_open: dict[str, bool] = {}
        self.observed_start: dict[str, float] = {}
        self.observed_spans: dict[str, list[list[float]]] = {}
        self.module_errors: list[dict] = []

    # -- helpers -----------------------------------------------------------
    def _state(self, dev: str) -> DeviceState:
        if dev not in self.states:
            self.states[dev] = DeviceState(dev)
        return self.states[dev]

    def _update_observed_truth(self, elapsed: float) -> None:
        """Read the simulator's true state and record when conditions start."""
        for tr in self.scen.truth:
            if not tr.observed:
                continue
            try:
                now_open = bool(tr.predicate(self.nodes)) and tr.contains(elapsed)
            except Exception:
                now_open = False
            was_open = self.observed_open.get(tr.module, False)
            if now_open and not was_open:
                self.observed_start[tr.module] = elapsed
                self.observed_spans.setdefault(tr.module, []).append([elapsed, elapsed])
            elif now_open and was_open:
                self.observed_spans[tr.module][-1][1] = elapsed
            self.observed_open[tr.module] = now_open

    def _truth_open(self, module: str, elapsed: float) -> bool:
        for tr in self.scen.truth:
            if tr.module != module:
                continue
            if tr.observed:
                if self.observed_open.get(module, False):
                    return True
            elif tr.contains(elapsed):
                return True
        return False

    def _precursor_open(self, module: str, elapsed: float) -> bool:
        """Is a looser version of the condition present? Then an alert is early."""
        for tr in self.scen.truth:
            if tr.module != module or tr.precursor is None or not tr.contains(elapsed):
                continue
            try:
                if bool(tr.precursor(self.nodes)):
                    return True
            except Exception:
                continue
        return False

    def _truth_start(self, module: str, elapsed: float) -> float | None:
        """When the condition genuinely began, for the delay measurement."""
        for tr in self.scen.truth:
            if tr.module != module:
                continue
            if tr.observed:
                if self.observed_open.get(module, False):
                    return self.observed_start.get(module)
            elif tr.contains(elapsed):
                return tr.start_s
        return None

    def _expected_sev(self, module: str) -> int:
        for tr in self.scen.truth:
            if tr.module == module:
                return tr.min_severity
        return 2

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        scen = self.scen
        interval = scen.interval_s
        t = scen.start_epoch
        elapsed = 0.0
        pending: list[dict] = []

        while elapsed <= scen.duration_s:
            scen.apply(t, elapsed, self.nodes)
            for node in self.nodes.values():
                node.step_physics(t, interval)
            # Ground truth is read after the physics advances and before any
            # sensor is sampled.
            self._update_observed_truth(elapsed)
            for node in self.nodes.values():
                payload = node.sample(t)
                if any(getattr(s, "faulted", False) for s in node.sensors.values()):
                    self.faulted_samples += 1

                # Honour the node's own buffering so the outage test exercises
                # the real path rather than a shortcut.
                delivered: list[dict] = []
                node.publish(payload, lambda p: (delivered.append(dict(p)), True)[1])
                for rec in delivered:
                    if rec.get("buffered"):
                        self.buffered_replays += 1
                    pending.append((elapsed, rec))

            for sample_elapsed, rec in pending:
                self._ingest(rec, sample_elapsed, t)
            pending = []

            t += interval
            elapsed += interval

        self._finalise()

    def _ingest(self, rec: dict, elapsed: float, now_t: float) -> None:
        dev = rec["device_id"]
        st = self._state(dev)
        res = self.engine.ingest(rec, st, now=now_t)
        for err in res.module_errors:
            if not any(e["error"] == err["error"] for e in self.module_errors):
                self.module_errors.append(err)
        self.samples += 1
        self.rejected += len(res.rejected)
        if res.situation:
            self.last_situation = res.situation

        # Did the engine refuse to invent a value for a dead sensor?
        node = self.nodes.get(dev)
        if node and any(getattr(s, "faulted", False) for s in node.sensors.values()):
            dev_mod = res.modules.get("device")
            if dev_mod and dev_mod.severity >= Severity.WATCH:
                self.unknown_when_faulted += 1

        for alert in res.alerts:
            if alert.state != "active":
                continue
            module = alert.module
            expected = self._expected_sev(module)
            truth_open = self._truth_open(module, elapsed)
            episode = {
                "module": module,
                "elapsed_s": round(elapsed, 1),
                "severity": int(alert.severity),
                "confidence": round(alert.confidence, 3),
                "truth_open": truth_open,
                "reason": alert.reason,
                "early": (not truth_open) and self._precursor_open(module, elapsed),
                "false_alarm": (
                    (not truth_open)
                    and module != "device"
                    and not self._precursor_open(module, elapsed)
                ),
            }
            self.alert_episodes.append(episode)
            if truth_open and int(alert.severity) >= expected and module not in self.first_detect:
                began = self._truth_start(module, elapsed)
                if began is not None:
                    self.first_detect[module] = round(elapsed - began, 1)

        # Tank volume and ETA accuracy against the simulator's truth.
        tank_mod = res.modules.get("tank")
        node1 = self.nodes.get("aquasentinel-node-01")
        if tank_mod and node1 and isinstance(node1.sensors.get("tank"), TankModel):
            true_tank: TankModel = node1.sensors["tank"]
            reported = tank_mod.value("remaining_l")
            if reported is not None and not getattr(true_tank, "faulted", False):
                self.tank_errors.append((elapsed, reported, true_tank.volume_l))
            eta = tank_mod.value("eta_depletion_h")
            if eta is not None:
                self.eta_snapshots.append((elapsed, eta, tank_mod.value("consumption_lph") or 0.0))
            if true_tank.volume_l <= 1.0 and self.true_empty_at is None:
                self.true_empty_at = elapsed

        dem = res.modules.get("demand")
        if dem:
            sig = dem.signal("forecast_demand_l_next_h")
            if sig and sig.detail.get("validation"):
                self.demand_validation = sig.detail["validation"] | {"note": sig.detail.get("note", "")}

    # -- scoring -----------------------------------------------------------
    def _finalise(self) -> None:
        pass

    def score(self) -> dict:
        modules_in_truth = sorted({tr.module for tr in self.scen.truth})
        detections = []
        for module in modules_in_truth:
            delay = self.first_detect.get(module)
            detections.append({
                "module": module,
                "detected": delay is not None,
                "delay_s": delay,
                "delay_min": round(delay / 60.0, 2) if delay is not None else None,
            })

        # Count false alarms per episode, and only for modules this scenario
        # was not designed to trigger.
        false_alarms = [e for e in self.alert_episodes if e["false_alarm"]]

        out: dict = {
            "scenario": self.scen.name,
            "description": self.scen.description,
            "claim": self.scen.claim,
            "duration_h": round(self.scen.duration_s / 3600.0, 2),
            "sample_interval_s": self.scen.interval_s,
            "samples_ingested": self.samples,
            "implausible_readings_rejected": self.rejected,
            "truth_windows": [
                ({"module": tr.module, "label": tr.label, "source": "observed from the simulator",
                  "spans_min": [[round(a / 60, 1), round(b / 60, 1)]
                                for a, b in self.observed_spans.get(tr.module, [])]}
                 if tr.observed else
                 {"module": tr.module, "label": tr.label, "source": "injected by the scenario",
                  "start_min": round(tr.start_s / 60, 1), "end_min": round(tr.end_s / 60, 1)})
                for tr in self.scen.truth
            ],
            "detections": detections,
            "missed": [d["module"] for d in detections if not d["detected"]],
            "false_alarm_episodes": len(false_alarms),
            "false_alarms": [
                {"module": e["module"], "at_min": round(e["elapsed_s"] / 60, 1),
                 "severity": e["severity"], "reason": e["reason"][:140]}
                for e in false_alarms
            ],
            "alert_episodes_total": len(self.alert_episodes),
            "early_alert_episodes": sum(1 for e in self.alert_episodes if e.get("early")),
            "module_errors": [{"module": e["module"], "error": e["error"]} for e in self.module_errors],
            "buffered_records_replayed": self.buffered_replays,
        }

        if self.tank_errors:
            errs = [abs(rep - tru) for _, rep, tru in self.tank_errors]
            cap = self.cfg.tank.capacity_l or 1.0
            # As a percentage of CAPACITY, not of the current volume. Dividing
            # by a volume that approaches zero sent the "relative error" to
            # 277%, which said nothing about the sensor and everything about
            # the denominator. Percent-of-full-scale is also how a level
            # sensor's accuracy is normally specified, so it is comparable.
            out["tank_volume_error"] = {
                "n": len(errs),
                "mae_l": round(sum(errs) / len(errs), 1),
                "max_l": round(max(errs), 1),
                "mae_pct_of_capacity": round(100.0 * (sum(errs) / len(errs)) / cap, 3),
                "max_pct_of_capacity": round(100.0 * max(errs) / cap, 3),
            }
        if self.eta_snapshots and self.true_empty_at is not None:
            # Score each ETA against how long the tank *actually* had left --
            # but only while there is a meaningful draw. The ETA states its own
            # assumption ("consumption continues at the current rate"), so
            # scoring it at 3 a.m., when the current rate is near zero and it
            # correctly reports days, measures our choice of scoring moment
            # rather than the estimate.
            errs, rel = [], []
            for elapsed, eta_h, rate_lph in self.eta_snapshots:
                true_remaining_h = (self.true_empty_at - elapsed) / 3600.0
                if true_remaining_h > 0.25 and rate_lph >= self.MEANINGFUL_DRAW_LPH:
                    errs.append(abs(eta_h - true_remaining_h))
                    rel.append(abs(eta_h - true_remaining_h) / true_remaining_h)
            if errs:
                srt = sorted(errs)
                out["eta_error"] = {
                    "n": len(errs),
                    "scored_when": f"reported draw >= {self.MEANINGFUL_DRAW_LPH:g} L/h",
                    "mae_h": round(sum(errs) / len(errs), 2),
                    "median_h": round(srt[len(srt) // 2], 2),
                    "p90_h": round(srt[int(0.9 * (len(srt) - 1))], 2),
                    "median_relative_pct": round(100.0 * sorted(rel)[len(rel) // 2], 1),
                    "true_empty_at_h": round(self.true_empty_at / 3600.0, 2),
                }
            else:
                out["eta_error"] = {"n": 0, "note": "no ETA was issued while the draw was meaningful"}
        elif self.eta_snapshots:
            out["eta_error"] = {
                "n": 0,
                "note": "the tank did not run dry within the scenario, so no ETA could be scored",
            }
        if self.demand_validation:
            out["demand_forecast"] = self.demand_validation
        if self.faulted_samples:
            out["sensor_fault_handling"] = {
                "faulted_samples": self.faulted_samples,
                "flagged_as_degraded": self.unknown_when_faulted,
                "flagged_pct": round(100.0 * self.unknown_when_faulted / self.faulted_samples, 1),
            }
        return out


def run_scenarios(names: list[str], warm: bool = True) -> dict:
    cfg = site_for_sim()
    results: list[dict] = []
    warmup: ScenarioRun | None = None

    if warm:
        # Learn what normal looks like before judging anything as abnormal.
        t0 = time.time()
        warmup = ScenarioRun(scenarios.get("baseline_short"), cfg)
        warmup.run()
        base_score = warmup.score()
        base_score["role"] = "warm-up and false-alarm test"
        base_score["wall_clock_s"] = round(time.time() - t0, 1)
        results.append(base_score)
        print(f"  baseline_short: {base_score['samples_ingested']} samples, "
              f"{base_score['false_alarm_episodes']} false-alarm episode(s)", file=sys.stderr)

    for name in names:
        if name == "baseline_short" and warm:
            continue
        t0 = time.time()
        run = ScenarioRun(scenarios.get(name), cfg, warmup=warmup)
        run.run()
        score = run.score()
        score["wall_clock_s"] = round(time.time() - t0, 1)
        results.append(score)
        det = ", ".join(
            f"{d['module']}={'%.1f min' % d['delay_min'] if d['detected'] else 'MISSED'}"
            for d in score["detections"]
        ) or "no truth windows"
        print(f"  {name}: {det}; false alarms={score['false_alarm_episodes']}", file=sys.stderr)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine_version": __import__("engine").__version__,
        "site": {"capacity_l": cfg.tank.capacity_l, "height_cm": cfg.tank.height_cm,
                 "calibrated": cfg.calibrated},
        "scenarios": results,
    }


def to_markdown(report: dict) -> str:
    lines = [
        "# AquaSentinel -- measured results",
        "",
        f"Generated {report['generated_at']} by `eval/run_eval.py` against engine "
        f"v{report['engine_version']}.",
        "",
        "Every figure below comes from driving the simulated rig through the real detection",
        "engine and comparing against the simulator's ground truth. Reproduce with `make eval`.",
        "",
        "## Detection delay and false alarms",
        "",
        "| Scenario | Module | Detected | Delay | False-alarm episodes | Samples |",
        "|---|---|---|---|---|---|",
    ]
    for s in report["scenarios"]:
        if not s["detections"]:
            lines.append(
                f"| {s['scenario']} | _(none expected)_ | - | - | **{s['false_alarm_episodes']}** "
                f"| {s['samples_ingested']} |"
            )
        for d in s["detections"]:
            delay = f"{d['delay_min']:.1f} min" if d["detected"] else "-"
            mark = "yes" if d["detected"] else "**MISSED**"
            lines.append(
                f"| {s['scenario']} | {d['module']} | {mark} | {delay} | "
                f"{s['false_alarm_episodes']} | {s['samples_ingested']} |"
            )

    lines += ["", "## Estimate accuracy", ""]
    any_acc = False
    for s in report["scenarios"]:
        if "tank_volume_error" in s:
            any_acc = True
            e = s["tank_volume_error"]
            lines.append(
                f"- **{s['scenario']}** tank volume: MAE {e['mae_l']} L "
                f"({e['mae_pct_of_capacity']}% of the 5000 L capacity), worst {e['max_l']} L "
                f"({e['max_pct_of_capacity']}%), n={e['n']}."
            )
        if "eta_error" in s and s["eta_error"].get("n"):
            e = s["eta_error"]
            lines.append(
                f"- **{s['scenario']}** depletion ETA: median error {e['median_h']} h "
                f"({e['median_relative_pct']}% of the true time remaining), MAE {e['mae_h']} h, "
                f"90th percentile {e['p90_h']} h, n={e['n']}, scored when {e['scored_when']} "
                f"(the tank truly ran dry at {e['true_empty_at_h']} h)."
            )
        elif "eta_error" in s:
            lines.append(f"- **{s['scenario']}** depletion ETA: {s['eta_error']['note']}.")
    if not any_acc:
        lines.append("_No tank-instrumented scenario was run._")

    lines += ["", "## Demand forecast vs baseline", ""]
    shown = False
    for s in report["scenarios"]:
        v = s.get("demand_forecast")
        if not v:
            continue
        shown = True
        lines.append(
            f"- **{s['scenario']}**: baseline MAE "
            f"{v.get('baseline_mae_lph')} L/h vs model MAE {v.get('model_mae_lph')} L/h "
            f"(train n={v.get('n_train')}, holdout n={v.get('n_holdout')}). "
            f"Adopted: **{v.get('adopted')}**. {v.get('note', '')}"
        )
    if not shown:
        lines.append("_No scenario accumulated enough demand history to score a forecast._")

    lines += ["", "## Robustness", ""]
    for s in report["scenarios"]:
        bits = []
        if s.get("implausible_readings_rejected"):
            bits.append(f"{s['implausible_readings_rejected']} implausible readings rejected")
        if s.get("buffered_records_replayed"):
            bits.append(f"{s['buffered_records_replayed']} buffered records replayed after the outage")
        if s.get("sensor_fault_handling"):
            h = s["sensor_fault_handling"]
            bits.append(
                f"{h['flagged_pct']}% of faulted samples flagged as degraded "
                f"({h['flagged_as_degraded']}/{h['faulted_samples']})"
            )
        if bits:
            lines.append(f"- **{s['scenario']}**: " + "; ".join(bits) + ".")

    lines += ["", "## What each scenario entitles us to claim", ""]
    for s in report["scenarios"]:
        if s.get("claim"):
            lines.append(f"- **{s['scenario']}**: {s['claim']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AquaSentinel evaluation harness")
    ap.add_argument("--scenarios", nargs="*",
                    default=["flood", "leak", "depletion", "heatwave", "outage",
                             "sensor_fault", "longhaul", "demo"])
    ap.add_argument("--no-warm", action="store_true", help="skip the baseline warm-up run")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    args = ap.parse_args(argv)

    # Reproducibility: a fixed reference date means a run today and a run next
    # month produce the same numbers, which is the whole point of publishing them.
    scenarios.use_fixed_epoch()
    print("[eval] running scenarios through the real engine...", file=sys.stderr)
    report = run_scenarios(args.scenarios, warm=not args.no_warm)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    (out_dir / "REPORT.md").write_text(to_markdown(report))
    print(f"[eval] wrote {out_dir/'metrics.json'} and {out_dir/'REPORT.md'}", file=sys.stderr)

    missed = [(s["scenario"], m) for s in report["scenarios"] for m in s.get("missed", [])]
    fa = sum(s["false_alarm_episodes"] for s in report["scenarios"])
    errors = [(s["scenario"], e) for s in report["scenarios"] for e in s.get("module_errors", [])]
    print(f"[eval] summary: {len(missed)} missed detection(s), {fa} false-alarm episode(s), "
          f"{len(errors)} module error(s)", file=sys.stderr)
    for scen, module in missed:
        print(f"[eval]   MISSED {module} in {scen}", file=sys.stderr)
    for scen, err in errors:
        print(f"[eval]   ERROR  {err['module']} in {scen}: {err['error']}", file=sys.stderr)
    # A crashing detection module is a hard failure, not a scoring footnote.
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
