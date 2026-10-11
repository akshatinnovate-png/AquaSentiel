"""Module A -- rising water and local waterlogging risk.

What this module honestly does:
  * MEASURES water depth at one surveyed low point, and rainfall intensity.
  * DERIVES a smoothed depth and a rate of rise, with a goodness-of-fit.
  * PROJECTS how long until the locally-surveyed danger depth is reached,
    assuming the current rate continues.

What it does not do: forecast a flood.  A citywide or even street-wide flood
forecast needs drainage topology, catchment data and validation we do not have.
Everything here is local to the instrumented point and is labelled as such.
"""
from __future__ import annotations

from engine.config import SiteConfig
from engine.model import ModuleResult, Reading, Severity, Signal
from engine.state import DeviceState
from engine.stats import clamp


def evaluate(reading: Reading, state: DeviceState, cfg: SiteConfig) -> ModuleResult | None:
    fc = cfg.flood
    res = ModuleResult(module="flood", title="Flood / waterlogging")
    if not fc.enabled:
        return None

    depth_raw = reading.get("street_water_cm")
    if depth_raw is None:
        return None  # this node does not watch a low point

    series = state.s("street_water_cm")
    now = reading.ts

    # 1. Smooth.  A waterproof ultrasonic sensor sees splashes and the odd
    #    dropout; a causal median filter removes those without lagging much.
    depth = series.smoothed_last(fc.smooth_window)
    if depth is None:
        depth = depth_raw

    # 2. Rate of rise, by least squares over the trailing window.  We use the
    #    fit quality to decide whether to believe the trend at all.
    slope = series.slope_per_s(now, fc.slope_window_s, smooth=fc.smooth_window)
    rise_cm_min = slope.value * 60.0
    trend_trusted = slope.n >= 3 and (slope.r2 >= fc.min_r2_for_trend or abs(rise_cm_min) > fc.rise_warn_cm_min)

    # Is there measurable water at all? Several checks below depend on this,
    # because a trend fitted to a dry channel is fitting sensor noise.
    water_present = depth >= max(1.0, fc.watch_cm * 0.25)

    rain_rate = reading.get("rain_rate_mmhr")
    if rain_rate is None:
        rr_series = state.s("rain_rate_mmhr")
        rain_rate = rr_series.last if len(rr_series) else None

    # 3. Combine depth, rate of rise and rainfall into a severity.
    by_depth = Severity.OK
    if depth >= fc.danger_cm:
        by_depth = Severity.CRITICAL
    elif depth >= fc.warn_cm:
        by_depth = Severity.WARNING
    elif depth >= fc.watch_cm:
        by_depth = Severity.WATCH

    by_rise = Severity.OK
    if trend_trusted and water_present:
        if rise_cm_min >= fc.rise_critical_cm_min:
            by_rise = Severity.CRITICAL
        elif rise_cm_min >= fc.rise_warn_cm_min:
            by_rise = Severity.WARNING
        elif rise_cm_min >= fc.rise_watch_cm_min:
            by_rise = Severity.WATCH

    # Rainfall is corroborating evidence, never a measurement of water at this
    # location. Heavy rain with a dry channel means the drain is coping, which
    # is good news -- reporting it as a watch produced the nonsense line
    # "water is present at 0.0 cm, rainfall 9 mm/h", so rain now requires
    # measurable water before it can raise anything.
    by_rain = Severity.OK
    if rain_rate is not None and water_present:
        if rain_rate >= fc.rain_intensity_warn_mmhr and depth >= fc.watch_cm:
            by_rain = Severity.WARNING
        elif rain_rate >= fc.rain_intensity_watch_mmhr:
            by_rain = Severity.WATCH

    severity = Severity(max(int(by_depth), int(by_rise), int(by_rain)))

    # Water that is deep *and* still rising is worse than either alone.
    if by_depth >= Severity.WATCH and by_rise >= Severity.WARNING:
        severity = Severity(min(int(Severity.CRITICAL), int(severity) + 1))

    # 4. Project time to the surveyed danger depth at the current rate.
    eta_min: float | None = None
    if trend_trusted and rise_cm_min > 0.05 and depth < fc.danger_cm:
        eta_min = (fc.danger_cm - depth) / rise_cm_min

    # Hysteresis: a falling level must drop clearly below the band it was in.
    latch = state.latches["flood"]
    if latch.severity > 0 and severity < latch.severity:
        band = {
            int(Severity.CRITICAL): fc.danger_cm,
            int(Severity.WARNING): fc.warn_cm,
            int(Severity.WATCH): fc.watch_cm,
        }.get(int(latch.severity))
        if band is not None and depth > band - fc.clear_hysteresis_cm:
            severity = Severity(latch.severity)

    state_name = {
        Severity.OK: "NORMAL",
        Severity.INFO: "NORMAL",
        Severity.WATCH: "WATCH",
        Severity.WARNING: "WARNING",
        Severity.CRITICAL: "CRITICAL",
    }[severity]

    res.severity = severity
    res.state = state_name
    res.confidence = round(clamp(0.45 + 0.4 * slope.r2 + (0.15 if len(series) >= fc.smooth_window else 0.0), 0.3, 0.98), 3)
    res.signals = [
        Signal("water_depth_cm", depth, "cm", "measured", 1.0,
               {"raw_cm": round(depth_raw, 2), "filter": f"causal median, {fc.smooth_window} samples"}),
        Signal("rate_of_rise_cm_min", round(rise_cm_min, 3), "cm/min", "derived",
               clamp(slope.r2, 0.0, 1.0),
               {"window_s": fc.slope_window_s, "fit": slope.as_dict(), "trusted": trend_trusted}),
        Signal("rain_rate_mmhr", rain_rate, "mm/h", "measured" if reading.get("rain_rate_mmhr") is not None else "derived"),
        Signal("depth_fraction_of_danger", round(depth / fc.danger_cm, 3) if fc.danger_cm else None,
               "ratio", "derived", 1.0, {"danger_cm": fc.danger_cm}),
        Signal("eta_to_danger_min", round(eta_min, 1) if eta_min is not None else None, "min", "projected",
               clamp(slope.r2 * 0.8, 0.0, 0.8),
               {"assumes": "current rate of rise continues unchanged"}),
    ]

    ev: list[dict] = [{
        "signal": "water_depth_cm",
        "value": round(depth, 2),
        "threshold": fc.danger_cm if by_depth == Severity.CRITICAL else fc.warn_cm if by_depth == Severity.WARNING else fc.watch_cm,
        "note": "depth at the surveyed low point",
    }]
    if by_rise > Severity.OK:
        ev.append({
            "signal": "rate_of_rise_cm_min",
            "value": round(rise_cm_min, 2),
            "threshold": fc.rise_critical_cm_min if by_rise == Severity.CRITICAL else fc.rise_warn_cm_min if by_rise == Severity.WARNING else fc.rise_watch_cm_min,
            "note": f"least-squares trend over {fc.slope_window_s}s, r2={slope.r2:.2f}",
        })
    if by_rain > Severity.OK and rain_rate is not None:
        ev.append({
            "signal": "rain_rate_mmhr",
            "value": round(rain_rate, 1),
            "threshold": fc.rain_intensity_warn_mmhr if by_rain == Severity.WARNING else fc.rain_intensity_watch_mmhr,
            "note": "corroborating rainfall intensity (does not by itself raise severity above WATCH)",
        })
    res.evidence = ev

    # Plain language for a caretaker, not for an engineer.
    if severity >= Severity.CRITICAL:
        res.reason = (
            f"Water at the monitored low point is {depth:.1f} cm, at or past the surveyed danger "
            f"depth of {fc.danger_cm:.0f} cm"
            + (f", and still rising at {rise_cm_min:.1f} cm/min." if rise_cm_min > 0.1 else ".")
        )
        res.recommendation = "Treat the location as unsafe to cross. Restrict access and inspect the drain now."
    elif severity == Severity.WARNING:
        res.reason = (
            f"Water is {depth:.1f} cm and rising at {rise_cm_min:.1f} cm/min"
            + (f"; at this rate it reaches the {fc.danger_cm:.0f} cm danger depth in about {eta_min:.0f} min."
               if eta_min is not None else f"; the danger depth is {fc.danger_cm:.0f} cm.")
        )
        res.recommendation = "Inspect the drain inlet for blockage and prepare to close the approach."
    elif severity == Severity.WATCH:
        bits = [f"water is present at {depth:.1f} cm"]
        if by_rise > Severity.OK:
            bits.append(f"rising at {rise_cm_min:.1f} cm/min")
        if by_rain > Severity.OK and rain_rate is not None:
            bits.append(f"rainfall {rain_rate:.0f} mm/h")
        res.reason = "Watch: " + ", ".join(bits) + "."
        res.recommendation = "No action needed yet. Re-check if the level keeps rising."
    elif rain_rate is not None and rain_rate >= fc.rain_intensity_watch_mmhr:
        res.reason = (
            f"Rain falling at {rain_rate:.0f} mm/h but the low point is dry ({depth:.1f} cm) -- "
            f"the drain is coping."
        )
        res.recommendation = ""
    else:
        res.reason = f"Water depth {depth:.1f} cm, stable. Nothing at this location needs attention."
        res.recommendation = ""

    return res
