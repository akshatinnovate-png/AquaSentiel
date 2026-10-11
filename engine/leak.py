"""Module B -- water loss and wastage detection.

We deliberately use the technique real water utilities use rather than
inventing one: **minimum night flow**.  During hours when a site should be
idle, the lowest sustained flow it still draws is the best available proxy for
loss, because legitimate demand goes to zero but a leak does not.

On top of that we add two corroborating detectors:
  * continuous flow -- no return to zero for an implausibly long stretch;
  * baseline excess -- flow far above what this site normally draws at this
    hour of the week, sustained.

The output is an evidence score, never a verdict.  A tap left running and a
burst pipe look identical to a flow meter, so the alert says "possible loss"
and lists exactly which signals fired, so a caretaker can check in one trip.
"""
from __future__ import annotations

from engine.config import SiteConfig
from engine.model import ModuleResult, Reading, Severity, Signal
from engine.state import Buckets, DeviceState
from engine.stats import clamp, quantile


def _baseline(state: DeviceState, cfg: SiteConfig, ts: float) -> tuple[float | None, float | None, int]:
    """Learned (p50, p95, n) flow for this hour-of-week bucket."""
    hour = cfg.local_hour(ts)
    dow = cfg.local_dow(ts)
    key = Buckets.key(hour, dow)
    n = state.flow_baseline.count(key)
    if n < cfg.leak.baseline_min_samples:
        # Fall back to the hour-of-day bucket pooled across day types.
        pooled: list[float] = []
        for k, arr in state.flow_baseline.data.items():
            if k.startswith(f"h{hour:02d}"):
                pooled.extend(arr)
        if len(pooled) < cfg.leak.baseline_min_samples:
            return None, None, len(pooled)
        return quantile(pooled, 0.5), quantile(pooled, 0.95), len(pooled)
    return state.flow_baseline.p(key, 0.5), state.flow_baseline.p(key, 0.95), n


def evaluate(reading: Reading, state: DeviceState, cfg: SiteConfig) -> ModuleResult | None:
    lc = cfg.leak
    if not lc.enabled:
        return None
    flow = reading.get("flow_lpm")
    if flow is None:
        return None

    now = reading.ts
    series = state.s("flow_lpm")
    hour = cfg.local_hour(now)
    is_idle_hour = hour in lc.expected_idle_hours
    flowing = flow > lc.zero_flow_lpm

    # -- track the current uninterrupted run of flow ----------------------
    if flowing:
        if not state.flow_run_started_ts:
            state.flow_run_started_ts = now
    else:
        state.flow_run_started_ts = 0.0
    run_min = (now - state.flow_run_started_ts) / 60.0 if state.flow_run_started_ts else 0.0

    # -- learn "normal" from history, but never from a suspected leak -----
    p50, p95, n_base = _baseline(state, cfg, now)

    # -- Detector 1: minimum night flow -----------------------------------
    # The *minimum* over a 30-minute idle window, so one legitimate midnight
    # flush does not look like a leak.
    mnf_window = series.values(now, 1800)
    mnf = min(mnf_window) if len(mnf_window) >= 4 else None
    mnf_fired = bool(is_idle_hour and mnf is not None and mnf >= lc.min_night_flow_lpm)

    # -- Detector 2: continuous flow --------------------------------------
    # Gated to the idle hours. A campus draws water continuously from morning
    # to evening, so an ungated "flow has not stopped for 45 minutes" detector
    # fires for most of every school day -- it produced a 721-minute
    # "continuous flow" alert in testing. What is actually diagnostic is flow
    # that carries on THROUGH the period when the site should be quiet.
    continuous_fired = is_idle_hour and run_min >= lc.continuous_flow_minutes

    # -- Detector 3: sustained excess over the learned baseline -----------
    excess_fired = False
    excess_ratio: float | None = None
    if p95 is not None and p95 > lc.zero_flow_lpm:
        excess_ratio = flow / p95
        recent = series.values(now, lc.excess_minutes * 60)
        if recent and len(recent) >= 4:
            share_over = sum(1 for v in recent if v > p95 * lc.excess_factor) / len(recent)
            excess_fired = share_over >= 0.8 and flow > p95 * lc.excess_factor

    # -- Evidence score ----------------------------------------------------
    # Weights reflect how specific each detector is to true loss: night flow
    # is the most specific, raw excess the least.
    score = 0.0
    ev: list[dict] = []
    if mnf_fired:
        score += 0.55
        ev.append({
            "signal": "minimum_night_flow_lpm",
            "value": round(mnf, 3),
            "threshold": lc.min_night_flow_lpm,
            "note": f"lowest flow in the last 30 min during an expected-idle hour ({hour:02d}:00 local)",
        })
    if continuous_fired:
        score += 0.3
        ev.append({
            "signal": "continuous_flow_min",
            "value": round(run_min, 1),
            "threshold": lc.continuous_flow_minutes,
            "note": "flow has not returned to zero for this long",
        })
    if excess_fired and p95 is not None:
        score += 0.25
        ev.append({
            "signal": "flow_vs_baseline_p95",
            "value": round(excess_ratio or 0.0, 2),
            "threshold": lc.excess_factor,
            "note": f"sustained flow above {lc.excess_factor:g}x the p95 for this hour (baseline from {n_base} samples)",
        })
    score = clamp(score, 0.0, 1.0)

    # Confidence is capped by how much history the baseline has seen.  With a
    # day of data we cannot claim to know what normal looks like.
    base_conf = clamp(n_base / (lc.baseline_min_samples * 4.0), 0.15, 1.0) if n_base else 0.15
    confidence = round(clamp(0.3 + 0.6 * base_conf, 0.2, 0.92), 3)

    # -- Volume lost: integrate flow above the baseline over the event ----
    if score >= lc.score_alert:
        if not state.leak_started_ts:
            state.leak_started_ts = state.flow_run_started_ts or now
            state.leak_volume_l = 0.0
        prev_ts = series.points[-2][0] if len(series.points) >= 2 else now
        dt_min = max(0.0, (now - prev_ts) / 60.0)
        excess_lpm = max(0.0, flow - (p50 or 0.0))
        state.leak_volume_l += excess_lpm * dt_min
    else:
        state.leak_started_ts = 0.0
        state.leak_volume_l = 0.0

    severity = Severity.OK
    if score >= lc.score_warning:
        severity = Severity.WARNING
    elif score >= lc.score_alert:
        severity = Severity.WATCH
    elif flowing and is_idle_hour:
        severity = Severity.INFO

    # Only update the learned baseline from samples we do *not* suspect.
    # Otherwise a leak that runs for a week becomes the new "normal".
    if severity <= Severity.INFO:
        state.flow_baseline.add(Buckets.key(hour, cfg.local_dow(now)), flow)

    duration_min = (now - state.leak_started_ts) / 60.0 if state.leak_started_ts else 0.0

    res = ModuleResult(module="leak", title="Possible water loss")
    res.severity = severity
    res.state = {
        Severity.OK: "NORMAL",
        Severity.INFO: "NOTED",
        Severity.WATCH: "POSSIBLE_LOSS",
        Severity.WARNING: "LIKELY_LOSS",
        Severity.CRITICAL: "LIKELY_LOSS",
    }[severity]
    res.confidence = confidence
    res.evidence = ev
    res.signals = [
        Signal("flow_lpm", flow, "L/min", "measured"),
        Signal("minimum_night_flow_lpm", round(mnf, 3) if mnf is not None else None, "L/min", "derived",
               1.0 if is_idle_hour else 0.4, {"window": "30 min", "idle_hour": is_idle_hour}),
        Signal("continuous_flow_min", round(run_min, 1), "min", "derived"),
        Signal("baseline_p50_lpm", round(p50, 3) if p50 is not None else None, "L/min", "derived",
               base_conf, {"samples": n_base, "learned_from": "this hour of the week, suspect samples excluded"}),
        Signal("baseline_p95_lpm", round(p95, 3) if p95 is not None else None, "L/min", "derived", base_conf),
        Signal("loss_score", round(score, 3), "score 0-1", "derived", confidence,
               {"detectors_fired": [e["signal"] for e in ev]}),
        Signal("estimated_volume_lost_l", round(state.leak_volume_l, 2), "L", "projected",
               clamp(confidence * 0.8, 0.1, 0.8),
               {"method": "integral of (flow - baseline p50) over the event", "duration_min": round(duration_min, 1)}),
    ]

    if severity >= Severity.WATCH:
        fired = ", ".join(e["signal"].replace("_", " ") for e in ev)
        res.reason = (
            f"Possible water loss on this line: {fired}. "
            f"About {state.leak_volume_l:.1f} L above normal over {duration_min:.0f} min."
        )
        res.recommendation = (
            "Check for a running tap, open valve or visible wet patch first, then isolate the line "
            "and watch whether flow stops. A flow anomaly alone does not prove a leak."
        )
    elif severity == Severity.INFO:
        res.reason = f"Flow of {flow:.2f} L/min during an expected-idle hour. Noted, not yet an alert."
        res.recommendation = ""
    else:
        res.reason = f"Flow {flow:.2f} L/min, consistent with normal use at this hour."
        res.recommendation = ""
    return res
