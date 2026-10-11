"""Module C -- stored supply and time to depletion.

The chain is: distance reading -> water depth -> volume -> consumption rate ->
time remaining.  Each link adds error, so each link is reported separately and
the final ETA carries a range rather than a single number.

The level-to-volume step is the one people usually get wrong.  A sensor reading
only becomes litres once you know the tank, so we support three mappings:
a vertical cylinder, a rectangular tank, and -- for the real-world irregular
case -- a measured dip chart that we interpolate.
"""
from __future__ import annotations

from engine.config import SiteConfig, TankConfig
from engine.model import ModuleResult, Reading, Severity, Signal
from engine.state import DeviceState
from engine.stats import clamp, lerp_curve


def level_to_litres(level_cm: float, tc: TankConfig) -> float:
    """Convert water depth inside the tank to litres."""
    level_cm = max(0.0, min(level_cm, tc.height_cm))
    if tc.shape == "curve" and tc.level_to_litres:
        return clamp(lerp_curve(tc.level_to_litres, level_cm), 0.0, tc.capacity_l)
    # Both the cylinder and the rectangular case are linear in depth once the
    # cross-section is constant, which is why they share a branch.  The
    # distinction matters only if a future shape is non-prismatic.
    if tc.height_cm <= 0:
        return 0.0
    return clamp(tc.capacity_l * (level_cm / tc.height_cm), 0.0, tc.capacity_l)


def depth_from_distance(distance_cm: float, tc: TankConfig) -> float:
    """An ultrasonic sensor measures air gap, not water. Convert it."""
    return clamp(tc.sensor_mount_cm - distance_cm, 0.0, tc.height_cm)


def _consumption_lph(state: DeviceState, now: float, tc: TankConfig) -> tuple[float, float, int, str]:
    """Net draw in litres/hour. Returns (rate, stderr, n, method).

    Two independent estimators, and we prefer the better one:

    * **flow meter** -- integrate the measured outflow. Direct, and unaffected
      by level-sensor noise.
    * **level trend** -- least squares on the volume series. The only option
      when no meter is fitted, but vulnerable to noise: on a 5000 L / 180 cm
      tank one centimetre of ultrasonic noise is ~28 L, so a short window can
      manufacture a draw of over 1000 L/h out of nothing. That is a real bug
      this function exists to prevent, so the level trend is only trusted once
      it spans `min_fit_span_s`, has `min_fit_samples` points, and produces a
      slope distinguishable from zero.

    Refills are excluded either way: a rising level would otherwise read as
    negative consumption and produce a nonsense ETA.
    """
    from engine.stats import ols_slope

    # -- preferred: the flow meter --------------------------------------
    flow = state.s("flow_lpm")
    if len(flow) >= 4:
        span = flow.points[-1][0] - flow.points[0][0]
        if span >= min(300.0, tc.min_fit_span_s):
            window_s = min(span, float(tc.consumption_window_s))
            litres = flow.integral(now, window_s) / 60.0      # L/min * s -> L
            rate = litres * 3600.0 / window_s
            vals = flow.values(now, window_s)
            # Spread of the flow samples, as a stand-in for the slope stderr.
            from engine.stats import stdev

            return max(0.0, rate), stdev(vals) * 60.0 / max(1.0, len(vals) ** 0.5), len(vals), "flow meter"

    # -- fallback: the level trend ---------------------------------------
    vol = state.s("tank_volume_l")
    win = vol.window(now, tc.consumption_window_s)
    if state.last_refill_ts:
        win = [(ts, v) for ts, v in win if ts > state.last_refill_ts]
    if len(win) < tc.min_fit_samples:
        return 0.0, 0.0, len(win), "insufficient data"
    span = win[-1][0] - win[0][0]
    if span < tc.min_fit_span_s:
        return 0.0, 0.0, len(win), "insufficient span"

    t0 = win[0][0]
    slope = ols_slope([ts - t0 for ts, _ in win], [v for _, v in win])
    rate_lph = -slope.value * 3600.0           # positive = being consumed
    stderr_lph = slope.stderr * 3600.0
    # Reject a trend that noise could have produced.
    if stderr_lph > 0 and abs(rate_lph) < tc.slope_significance * stderr_lph:
        return 0.0, stderr_lph, slope.n, "level trend (not significant)"
    return max(0.0, rate_lph), stderr_lph, slope.n, "level trend"


def evaluate(reading: Reading, state: DeviceState, cfg: SiteConfig) -> ModuleResult | None:
    tc = cfg.tank
    if not tc.enabled:
        return None

    # Accept either a depth (tank_level_cm) or a pre-computed percentage.
    level_cm = reading.get("tank_level_cm")
    pct_in = reading.get("tank_level_pct")
    if level_cm is None and pct_in is None:
        return None
    if level_cm is None:
        level_cm = tc.height_cm * clamp(pct_in, 0.0, 100.0) / 100.0

    now = reading.ts
    litres = level_to_litres(level_cm, tc)
    pct = 100.0 * litres / tc.capacity_l if tc.capacity_l else 0.0

    vol_series = state.s("tank_volume_l")
    prev_litres = vol_series.last
    vol_series.push(now, litres)

    # -- refill detection --------------------------------------------------
    # From the trend over a few minutes, not from one sample pair. A single
    # 0.3 cm ultrasonic jitter is ~8 L on this tank, which across a 15 s gap
    # looks like 32 L/min of inflow -- eight times the refill threshold. Using
    # consecutive samples made the engine report a refill on every upward
    # flicker, which in turn corrupted the consumption window.
    refilling = False
    rise = vol_series.slope_per_s(now, tc.refill_window_s)
    inflow_lpm = rise.value * 60.0
    if rise.n >= tc.refill_min_samples and inflow_lpm >= tc.refill_detect_lpm:
        stderr_lpm = rise.stderr * 60.0
        if stderr_lpm <= 0 or inflow_lpm >= tc.slope_significance * stderr_lpm:
            refilling = True
            state.last_refill_ts = now

    rate_lph, rate_stderr, n_fit, rate_method = _consumption_lph(state, now, tc)

    # -- time to depletion, with a range ----------------------------------
    eta_h: float | None = None
    eta_lo: float | None = None
    eta_hi: float | None = None
    if rate_lph >= tc.min_consumption_lph and not refilling:
        eta_h = litres / rate_lph
        # Propagate the slope's own uncertainty into the ETA.
        hi_rate = rate_lph + 1.96 * rate_stderr
        lo_rate = max(tc.min_consumption_lph, rate_lph - 1.96 * rate_stderr)
        eta_lo = litres / hi_rate if hi_rate > 0 else None
        eta_hi = litres / lo_rate if lo_rate > 0 else None

    # -- severity ----------------------------------------------------------
    severity = Severity.OK
    by_pct = Severity.OK
    if pct <= tc.critical_pct:
        by_pct = Severity.CRITICAL
    elif pct <= tc.low_pct:
        by_pct = Severity.WARNING
    elif pct <= tc.low_pct * 1.6:
        by_pct = Severity.WATCH

    by_eta = Severity.OK
    if eta_h is not None:
        if eta_h <= tc.eta_critical_hours:
            by_eta = Severity.CRITICAL
        elif eta_h <= tc.eta_warn_hours:
            by_eta = Severity.WARNING
        elif eta_h <= tc.eta_warn_hours * 2:
            by_eta = Severity.WATCH
        # A projection must not outrank the measurement it is built on. At a
        # lunchtime peak a half-full tank shows a six-hour ETA, but the peak
        # passes and the overnight refill arrives -- extrapolating it forever
        # produced a daily "action needed today" on a tank that was never in
        # trouble. Unless the level itself is getting low, a short ETA is
        # information, not a warning.
        if pct > tc.low_pct * 1.6:
            by_eta = Severity(min(int(by_eta), int(Severity.WATCH)))

    severity = Severity(max(int(by_pct), int(by_eta)))
    if refilling:
        # A tank that is filling is not a tank that is running out.
        severity = Severity(min(int(severity), int(Severity.WATCH)))

    # -- abnormal drain: draining much faster than this site's own habit ---
    #
    # Bucketed by hour of day. Pooling every hour together mixes a near-zero
    # overnight draw with a lunchtime peak, which drags the 95th percentile
    # down until an ordinary 4 L/h trickle reads as "faster than usual" -- an
    # alert we saw fire repeatedly on a quiet site. There is also an absolute
    # floor: no matter what this tank's habit is, a draw this small is not
    # worth a caretaker's trip.
    from engine.stats import quantile

    ABNORMAL_FLOOR_LPH = 60.0
    abnormal = False
    hour_key = f"drain_lph_h{cfg.local_hour(now):02d}"
    hist = [v for v in state.demand_baseline.get(hour_key) if v > 0]
    if rate_lph > ABNORMAL_FLOOR_LPH and len(hist) >= 10:
        p95 = quantile(hist, 0.95)
        if p95 > 0 and rate_lph > max(p95 * 1.8, p95 + 25.0):
            abnormal = True
            severity = Severity(max(int(severity), int(Severity.WATCH)))
    if rate_lph > 0 and not refilling:
        state.demand_baseline.add(hour_key, rate_lph)

    fit_conf = clamp(0.35 + 0.5 * (n_fit / 20.0), 0.2, 0.9)
    calib_conf = 0.95 if (tc.shape == "curve" and tc.level_to_litres) else 0.7
    confidence = round(clamp(fit_conf * calib_conf + (0.0 if cfg.calibrated else -0.05), 0.15, 0.95), 3)

    res = ModuleResult(module="tank", title="Stored supply")
    res.severity = severity
    res.state = (
        "REFILLING" if refilling else
        {
            Severity.OK: "NORMAL",
            Severity.INFO: "NORMAL",
            Severity.WATCH: "WATCH",
            Severity.WARNING: "LOW",
            Severity.CRITICAL: "CRITICAL",
        }[severity]
    )
    res.confidence = confidence
    res.signals = [
        Signal("tank_level_cm", round(level_cm, 2), "cm", "measured"),
        Signal("fill_pct", round(pct, 1), "%", "derived", calib_conf,
               {"mapping": tc.shape, "capacity_l": tc.capacity_l,
                "dip_chart_points": len(tc.level_to_litres) if tc.shape == "curve" else 0}),
        Signal("remaining_l", round(litres, 1), "L", "derived", calib_conf),
        Signal("consumption_lph", round(rate_lph, 2), "L/h", "derived", fit_conf,
               {"window_s": tc.consumption_window_s, "samples": n_fit, "method": rate_method,
                "stderr_lph": round(rate_stderr, 3), "refills_excluded": bool(state.last_refill_ts)}),
        Signal("eta_depletion_h", round(eta_h, 2) if eta_h is not None else None, "h", "projected",
               clamp(fit_conf * 0.85, 0.1, 0.8),
               {"assumes": "consumption continues at the current rate",
                "range_h": [round(eta_lo, 2) if eta_lo else None, round(eta_hi, 2) if eta_hi else None]}),
        Signal("refilling", 1.0 if refilling else 0.0, "bool", "derived"),
    ]

    ev: list[dict] = [{"signal": "fill_pct", "value": round(pct, 1),
                       "threshold": tc.critical_pct if by_pct == Severity.CRITICAL else tc.low_pct,
                       "note": f"{litres:.0f} L of {tc.capacity_l:.0f} L capacity"}]
    if by_eta > Severity.OK and eta_h is not None:
        ev.append({"signal": "eta_depletion_h", "value": round(eta_h, 2),
                   "threshold": tc.eta_critical_hours if by_eta == Severity.CRITICAL else tc.eta_warn_hours,
                   "note": f"at the current {rate_lph:.1f} L/h draw"})
    if abnormal:
        ev.append({"signal": "consumption_lph", "value": round(rate_lph, 1), "threshold": None,
                   "note": "draining faster than this tank's own 95th-percentile rate"})
    res.evidence = ev

    def eta_phrase() -> str:
        if eta_h is None:
            return "consumption is too low to estimate a depletion time"
        if eta_lo and eta_hi:
            return f"about {eta_h:.1f} h left (range {eta_lo:.1f}-{eta_hi:.1f} h)"
        return f"about {eta_h:.1f} h left"

    # The wording follows whichever detector actually drove the severity.
    # Saying "tank is low" when it is 85% full would destroy a caretaker's
    # trust in every later alert, even if the ETA behind it was sound.
    level_driven = int(by_pct) >= int(by_eta)
    if refilling:
        res.reason = f"Tank is refilling: now {pct:.0f}% ({litres:.0f} L)."
        res.recommendation = ""
    elif severity >= Severity.CRITICAL:
        res.reason = (
            f"Tank is critically low at {pct:.0f}% ({litres:.0f} L) and {eta_phrase()}."
            if level_driven else
            f"Tank is at {pct:.0f}% ({litres:.0f} L) but drawing {rate_lph:.0f} L/h, so {eta_phrase()}."
        )
        res.recommendation = "Arrange a refill now and inform users that supply may be interrupted."
    elif severity == Severity.WARNING:
        res.reason = (
            f"Tank is low at {pct:.0f}% ({litres:.0f} L), drawing {rate_lph:.0f} L/h, {eta_phrase()}."
            if level_driven else
            f"Tank is at {pct:.0f}% ({litres:.0f} L) but the current {rate_lph:.0f} L/h draw leaves {eta_phrase()}."
        )
        res.recommendation = "Schedule a refill today."
    elif severity == Severity.WATCH:
        extra = " Draining faster than usual for this tank." if abnormal else ""
        res.reason = f"Tank at {pct:.0f}% ({litres:.0f} L), drawing {rate_lph:.0f} L/h.{extra}"
        res.recommendation = "Plan the next refill." if not abnormal else "Check for unusual use or loss on this tank."
    else:
        res.reason = f"Tank at {pct:.0f}% ({litres:.0f} L), drawing {rate_lph:.0f} L/h."
        res.recommendation = ""
    return res
