"""Module D -- heat exposure and water-demand forecasting.

Two separate things live here, and keeping them separate is the point:

1. **Heat exposure.** We compute the NOAA/Rothfusz heat index from the
   measured temperature and humidity, because what matters to people is the
   combination, not the thermometer alone.  We compare it against a
   *locally configured advisory* threshold and say so.  We never call it a
   heatwave: that is a declaration only a meteorological agency can make.

2. **Demand forecasting.** We fit a seasonal-naive baseline (what did this
   site use at this hour, on this kind of day?) and a small ridge regression
   that adds heat and time-of-day features.  Then we score both on held-out
   data and **only adopt the weather-aware model if it actually beats the
   baseline**.  If it does not, the dashboard shows the baseline and says so.
   This is how we avoid the usual "we added ML and it got better" claim with
   nothing behind it.
"""
from __future__ import annotations

import math

from engine.config import SiteConfig
from engine.model import ModuleResult, Reading, Severity, Signal
from engine.state import Buckets, DeviceState
from engine.stats import clamp, mae, quantile, solve_ridge


def heat_index_c(t_c: float, rh_pct: float) -> float:
    """Rothfusz heat index, with the standard low-range and two adjustments.

    Implemented in Fahrenheit because that is how the regression was published,
    then converted back, which keeps it checkable against NWS tables.
    """
    t_f = t_c * 9.0 / 5.0 + 32.0
    rh = clamp(rh_pct, 0.0, 100.0)
    # Steadman's simple form, used below 80 F and to decide if we need more.
    simple = 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh * 0.094)
    if (simple + t_f) / 2.0 < 80.0:
        return (simple - 32.0) * 5.0 / 9.0
    hi = (
        -42.379
        + 2.04901523 * t_f
        + 10.14333127 * rh
        - 0.22475541 * t_f * rh
        - 0.00683783 * t_f * t_f
        - 0.05481717 * rh * rh
        + 0.00122874 * t_f * t_f * rh
        + 0.00085282 * t_f * rh * rh
        - 0.00000199 * t_f * t_f * rh * rh
    )
    if rh < 13.0 and 80.0 <= t_f <= 112.0:
        hi -= ((13.0 - rh) / 4.0) * math.sqrt((17.0 - abs(t_f - 95.0)) / 17.0)
    elif rh > 85.0 and 80.0 <= t_f <= 87.0:
        hi += ((rh - 85.0) / 10.0) * ((87.0 - t_f) / 5.0)
    return (hi - 32.0) * 5.0 / 9.0


# --------------------------------------------------------------------------
# Forecasting
# --------------------------------------------------------------------------

# People use more water in the heat during the day, not at 3 a.m., and the
# effect scales with how much they were using anyway -- a 10% increase on a
# lunchtime peak is many more litres than 10% of a sleeping campus. So the
# heat term needs to be an interaction, not an additive constant.
#
# This is not a guess. A first version used a bare `hi_anom` term and, over a
# fourteen-day run, lost to the seasonal-naive baseline on held-out data
# (32.7 vs 30.0 L/h). The interaction terms below were added because that
# measurement said the plain term was the wrong shape.
DAY_HOURS = (9, 20)


def _features(hour: int, dow: int, baseline_l: float, hi_anom_c: float) -> list[float]:
    """Design row for the demand model.

    bias, the seasonal-naive baseline, a smooth time-of-day, a weekend flag,
    and the heat anomaly in three forms: on its own, scaled by the baseline
    (the multiplicative effect), and restricted to daytime hours.
    """
    daytime = 1.0 if DAY_HOURS[0] <= hour <= DAY_HOURS[1] else 0.0
    return [
        1.0,
        baseline_l,
        hi_anom_c,
        hi_anom_c * baseline_l / 100.0,     # scaled to keep the columns comparable
        hi_anom_c * daytime,
        math.sin(2 * math.pi * hour / 24.0),
        math.cos(2 * math.pi * hour / 24.0),
        1.0 if dow >= 5 else 0.0,
    ]


def baseline_demand(state: DeviceState, cfg: SiteConfig, hour: int, dow: int) -> tuple[float | None, int]:
    """Seasonal-naive: the median litres this site used at this hour-of-week."""
    key = Buckets.key(hour, dow)
    arr = state.demand_baseline.get(key)
    if len(arr) >= 3:
        return quantile(arr, 0.5), len(arr)
    pooled: list[float] = []
    for k, vals in state.demand_baseline.data.items():
        if k.startswith(f"h{hour:02d}"):
            pooled.extend(vals)
    if pooled:
        return quantile(pooled, 0.5), len(pooled)
    return None, 0


def fit_demand_model(state: DeviceState, cfg: SiteConfig) -> dict:
    """Fit and *validate* the weather-aware model against the baseline.

    Returns a dict recording both scores and whether the model was adopted.
    The honest outcome -- "the baseline won" -- is a first-class result here,
    not a failure.
    """
    dcfg = cfg.demand
    # Training rows are (hour, dow, heat_anomaly_c, observed_litres_per_h).
    samples: list[tuple[int, int, float, float]] = []
    train_rows = state.demand_model.get("rows") or []
    for r in train_rows:
        try:
            samples.append((int(r[0]), int(r[1]), float(r[2]), float(r[3])))
        except (TypeError, ValueError, IndexError):
            continue

    out: dict = {
        "rows": train_rows[-720:],
        "n_train": 0,
        "n_holdout": 0,
        "baseline_mae": None,
        "model_mae": None,
        "adopted": False,
        "weights": None,
        "note": "",
    }
    if len(samples) < dcfg.min_train_samples:
        out["note"] = (
            f"Not enough history to test a weather-aware model "
            f"({len(samples)}/{dcfg.min_train_samples} samples). Showing the baseline."
        )
        return out

    split = int(len(samples) * (1.0 - dcfg.holdout_fraction))
    train, holdout = samples[:split], samples[split:]
    if len(holdout) < 4:
        out["note"] = "Holdout set too small to judge the model. Showing the baseline."
        return out

    # Baseline computed from the training portion only -- using the holdout to
    # build the baseline would flatter the comparison.
    buckets: dict[str, list[float]] = {}
    for hour, dow, _, litres in train:
        buckets.setdefault(Buckets.key(hour, dow), []).append(litres)
    hour_only: dict[int, list[float]] = {}
    for hour, _dow, _hi, litres in train:
        hour_only.setdefault(hour, []).append(litres)
    overall = quantile([s[3] for s in train], 0.5)

    def base_pred(hour: int, dow: int) -> float:
        arr = buckets.get(Buckets.key(hour, dow))
        if arr and len(arr) >= 2:
            return quantile(arr, 0.5)
        arr = hour_only.get(hour)
        if arr:
            return quantile(arr, 0.5)
        return overall

    x_rows = [_features(h, d, base_pred(h, d), hi) for h, d, hi, _ in train]
    y = [s[3] for s in train]
    weights = solve_ridge(x_rows, y, dcfg.ridge_l2)

    base_hold = [base_pred(h, d) for h, d, _, _ in holdout]
    model_hold = [
        sum(w * f for w, f in zip(weights, _features(h, d, base_pred(h, d), hi)))
        for h, d, hi, _ in holdout
    ]
    model_hold = [max(0.0, v) for v in model_hold]
    truth = [s[3] for s in holdout]

    b_mae, m_mae = mae(base_hold, truth), mae(model_hold, truth)
    adopted = b_mae > 0 and m_mae < b_mae * (1.0 - dcfg.model_adoption_margin)

    out.update(
        n_train=len(train),
        n_holdout=len(holdout),
        baseline_mae=round(b_mae, 3),
        model_mae=round(m_mae, 3),
        adopted=bool(adopted),
        weights=[round(w, 6) for w in weights] if adopted else None,
        improvement_pct=round(100.0 * (b_mae - m_mae) / b_mae, 1) if b_mae > 0 else 0.0,
        note=(
            f"Weather-aware model adopted: held-out MAE {m_mae:.2f} L/h vs baseline {b_mae:.2f} L/h."
            if adopted else
            f"Baseline retained: the weather-aware model did not beat it on held-out data "
            f"({m_mae:.2f} vs {b_mae:.2f} L/h)."
        ),
    )
    return out


def evaluate(reading: Reading, state: DeviceState, cfg: SiteConfig) -> ModuleResult | None:
    dcfg = cfg.demand
    if not dcfg.enabled:
        return None
    t_c = reading.get("temperature_c")
    rh = reading.get("humidity_pct")
    if t_c is None:
        return None
    if rh is None:
        rh = 50.0

    now = reading.ts
    hour = cfg.local_hour(now)
    dow = cfg.local_dow(now)
    hi = heat_index_c(t_c, rh)
    state.s("heat_index_c").push(now, hi)

    # -- Heat anomaly, measured the way IMD measures a heat event ---------
    #
    # Comparing an instantaneous reading against the median of its own
    # clock-hour does not work: at 11:00 the temperature is climbing steeply,
    # so a sample late in the hour sits 2-3 C above the hour's median and
    # every ordinary morning looks anomalous. IMD instead compares the day's
    # MAXIMUM against the normal maximum, so we do the same: today's peak heat
    # index against the median of previous days' peaks.
    day_index = int((now + cfg.timezone_offset_h * 3600) // 86400)
    if day_index != state.hi_day_index:
        if state.hi_day_index >= 0 and state.hi_day_max > -900:
            state.temp_baseline.add("daily_max", state.hi_day_max)
        state.hi_day_index = day_index
        state.hi_day_max = hi
    else:
        state.hi_day_max = max(state.hi_day_max, hi)

    prior_peaks = state.temp_baseline.get("daily_max")
    peak_norm = quantile(prior_peaks, 0.5) if prior_peaks else None
    hi_anom = (state.hi_day_max - peak_norm) if peak_norm is not None else 0.0
    hist_hi = prior_peaks

    # -- accumulate observed demand, one row per completed local hour ------
    #
    # Integrating the flow meter gives litres. We bank them against the
    # current local hour and emit a training row only when that hour closes.
    # An earlier version appended a row per sample and kept the last 600,
    # which at a 15 s cadence meant the model trained on the most recent two
    # and a half hours -- it could not have learned a daily shape if it tried.
    flow_series = state.s("flow_lpm")
    hour_key = int((now + cfg.timezone_offset_h * 3600) // 3600)
    if len(flow_series.points) >= 2:
        prev_ts, prev_flow = flow_series.points[-2]
        dt_min = max(0.0, (now - prev_ts) / 60.0)
        if dt_min <= 15.0:        # ignore a gap; do not invent the missing litres
            state.usage_hour_l += 0.5 * (prev_flow + (flow_series.last or 0.0)) * dt_min

    if state.usage_hour_key == -1:
        state.usage_hour_key = hour_key
    elif hour_key != state.usage_hour_key:
        closed_hour = int(state.usage_hour_key % 24)
        closed_ts = state.usage_hour_key * 3600 - cfg.timezone_offset_h * 3600
        closed_dow = cfg.local_dow(closed_ts)
        mean_anom = (state.usage_hour_hi_sum / state.usage_hour_n) if state.usage_hour_n else 0.0
        litres = state.usage_hour_l
        state.demand_baseline.add(Buckets.key(closed_hour, closed_dow), litres)
        rows = state.demand_model.setdefault("rows", [])
        rows.append([closed_hour, closed_dow, round(mean_anom, 3), round(litres, 3)])
        if len(rows) > 720:                     # 30 days of hourly history
            del rows[0 : len(rows) - 720]
        state.usage_hour_key = hour_key
        state.usage_hour_l = 0.0
        state.usage_hour_hi_sum = 0.0
        state.usage_hour_n = 0

    state.usage_hour_hi_sum += hi_anom
    state.usage_hour_n += 1

    # What the site is drawing right now, for display and for the tank module.
    observed_lph: float | None = None
    if len(flow_series) >= 4:
        span_s = flow_series.points[-1][0] - flow_series.points[0][0]
        if span_s >= 300:
            window_s = min(span_s, 3600.0)
            observed_lph = (flow_series.integral(now, window_s) / 60.0) * 3600.0 / window_s

    # -- refit periodically rather than on every sample -------------------
    last_fit = state.demand_model.get("fitted_at", 0.0)
    if now - last_fit > 900 or "adopted" not in state.demand_model:
        fit = fit_demand_model(state, cfg)
        fit["fitted_at"] = now
        fit["rows"] = state.demand_model.get("rows", [])[-720:]
        state.demand_model = fit

    model = state.demand_model
    base_now, base_n = baseline_demand(state, cfg, hour, dow)

    # -- forecast the next horizon ----------------------------------------
    forecast: list[dict] = []
    for h_ahead in range(1, dcfg.forecast_horizon_h + 1):
        f_ts = now + h_ahead * 3600
        f_hour, f_dow = cfg.local_hour(f_ts), cfg.local_dow(f_ts)
        b, _ = baseline_demand(state, cfg, f_hour, f_dow)
        if b is None:
            continue
        # Persist today's heat anomaly across the horizon.  With no external
        # forecast feed this is the only defensible assumption, and we say so.
        if model.get("adopted") and model.get("weights"):
            val = sum(w * f for w, f in zip(model["weights"], _features(f_hour, f_dow, b, hi_anom)))
            source = "model"
        else:
            val = b
            source = "baseline"
        forecast.append({
            "hour_local": f_hour,
            "hours_ahead": h_ahead,
            "litres_per_h": round(max(0.0, val), 2),
            "source": source,
        })

    forecast_total_l = sum(f["litres_per_h"] for f in forecast)
    uplift_pct = None
    if base_now and forecast:
        avg_f = forecast_total_l / len(forecast)
        uplift_pct = 100.0 * (avg_f - base_now) / base_now if base_now > 0 else None

    # -- severity from heat exposure only; demand shapes the wording ------
    #
    # Two gates, both of which must pass. An absolute heat index on its own is
    # a bad alarm in a hot climate: an October afternoon in much of India sits
    # in the high 30s and nobody needs telling. So, as IMD does, we also
    # require a departure from what THIS site normally sees at THIS hour.
    # The exception is a genuinely dangerous absolute value, where what is
    # locally "normal" stops being the point.
    # With no previous days to compare against we cannot establish a
    # departure from normal, so we deliberately do NOT alert on the advisory
    # bands -- only on a value dangerous in absolute terms. Treating "I have
    # no history" as "this is anomalous" is what produced an alert on every
    # ordinary afternoon.
    enough_history = len(prior_peaks) >= 1
    anomalous = enough_history and hi_anom >= dcfg.heat_anomaly_watch_c
    dangerous_regardless = hi >= dcfg.heat_index_absolute_c

    severity = Severity.OK
    if hi >= dcfg.heat_index_critical_c:
        severity = Severity.CRITICAL
    elif hi >= dcfg.heat_index_warn_c and (anomalous or dangerous_regardless):
        severity = Severity.WARNING
    elif hi >= dcfg.heat_index_watch_c and (anomalous or dangerous_regardless):
        severity = Severity.WATCH

    model_conf = 0.5 if not model.get("adopted") else clamp(0.55 + 0.3 * (model.get("n_train", 0) / 200.0), 0.5, 0.85)

    res = ModuleResult(module="demand", title="Heat and water demand")
    res.severity = severity
    res.state = {
        Severity.OK: "NORMAL",
        Severity.INFO: "NORMAL",
        Severity.WATCH: "ELEVATED_HEAT",
        Severity.WARNING: "HIGH_HEAT",
        Severity.CRITICAL: "EXTREME_HEAT",
    }[severity]
    res.confidence = round(clamp(0.8 if t_c is not None else 0.3, 0.3, 0.95), 3)
    res.signals = [
        Signal("temperature_c", round(t_c, 1), "C", "measured"),
        Signal("humidity_pct", round(rh, 1), "%", "measured"),
        Signal("heat_index_c", round(hi, 1), "C", "derived", 0.95,
               {"method": "Rothfusz / NOAA heat index",
                "advisory_thresholds_c": [dcfg.heat_index_watch_c, dcfg.heat_index_warn_c, dcfg.heat_index_critical_c],
                "caveat": "locally configured advisory levels, not an official heatwave declaration"}),
        Signal("heat_anomaly_c", round(hi_anom, 1), "C", "derived", 0.7,
               {"vs": "median of this site's previous daily peak heat index (IMD-style departure from normal)",
                "today_peak_c": round(state.hi_day_max, 1),
                "normal_peak_c": round(peak_norm, 1) if peak_norm is not None else None,
                "prior_days": len(prior_peaks),
                "anomaly_gate_c": dcfg.heat_anomaly_watch_c,
                "gate_passed": bool(anomalous),
                "note": "an alert needs both an absolute heat index above the advisory level "
                        "and a departure from this site's own normal for the hour"}),
        Signal("observed_demand_lph", round(observed_lph, 2) if observed_lph is not None else None,
               "L/h", "measured", 0.8),
        Signal("baseline_demand_lph", round(base_now, 2) if base_now is not None else None, "L/h", "derived",
               clamp(base_n / 20.0, 0.1, 0.9), {"method": "seasonal-naive median for this hour of the week",
                                                "samples": base_n}),
        Signal("forecast_demand_l_next_h", round(forecast_total_l, 1) if forecast else None, "L", "forecast",
               model_conf,
               {"horizon_h": dcfg.forecast_horizon_h,
                "source": "ridge model" if model.get("adopted") else "seasonal-naive baseline",
                "validation": {
                    "baseline_mae_lph": model.get("baseline_mae"),
                    "model_mae_lph": model.get("model_mae"),
                    "adopted": model.get("adopted", False),
                    "n_train": model.get("n_train", 0),
                    "n_holdout": model.get("n_holdout", 0),
                },
                "note": model.get("note", ""),
                "assumes": "the current heat anomaly persists across the horizon"}),
        Signal("demand_uplift_pct", round(uplift_pct, 1) if uplift_pct is not None else None, "%", "forecast",
               model_conf * 0.8),
        Signal("forecast_series", None, "", "forecast", model_conf, {"points": forecast}),
    ]

    ev: list[dict] = []
    if severity > Severity.OK:
        ev.append({"signal": "heat_index_c", "value": round(hi, 1),
                   "threshold": (dcfg.heat_index_critical_c if severity == Severity.CRITICAL
                                 else dcfg.heat_index_warn_c if severity == Severity.WARNING
                                 else dcfg.heat_index_watch_c),
                   "note": f"{t_c:.1f} C at {rh:.0f}% RH; locally configured advisory level"})
    if uplift_pct is not None and uplift_pct > 15:
        ev.append({"signal": "demand_uplift_pct", "value": round(uplift_pct, 1), "threshold": 15.0,
                   "note": f"forecast for the next {dcfg.forecast_horizon_h} h vs this hour's usual draw"})
    res.evidence = ev

    if severity >= Severity.WARNING:
        res.reason = (
            f"Heat index {hi:.0f} C ({t_c:.0f} C at {rh:.0f}% RH), above the locally configured "
            f"{dcfg.heat_index_warn_c:.0f} C advisory level and {hi_anom:+.1f} C above this "
            f"site's normal daily peak"
            + (f". Water demand over the next {dcfg.forecast_horizon_h} h is estimated {uplift_pct:+.0f}% "
               f"against this site's usual draw." if uplift_pct is not None else ".")
        )
        res.recommendation = (
            "Make sure drinking water points are stocked and plan a refill before peak afternoon demand. "
            "Use official weather alerts for any public heat warning."
        )
    elif severity == Severity.WATCH:
        res.reason = (
            f"Heat index {hi:.0f} C. Today's peak is {hi_anom:+.1f} C above this site's normal "
            f"daily peak, over {len(prior_peaks)} day(s) of history."
        )
        res.recommendation = "No action needed. Expect somewhat higher water use."
    elif hi >= dcfg.heat_index_watch_c and enough_history:
        res.reason = (
            f"Heat index {hi:.0f} C -- hot, but within {hi_anom:+.1f} C of this site's normal "
            f"daily peak, so it is not flagged."
        )
        res.recommendation = ""
    elif hi >= dcfg.heat_index_watch_c:
        res.reason = (
            f"Heat index {hi:.0f} C. No previous days to compare against yet, so only an "
            f"absolute reading above {dcfg.heat_index_absolute_c:.0f} C would be flagged."
        )
        res.recommendation = ""
    else:
        res.reason = f"Heat index {hi:.0f} C. Normal conditions for this site at this hour."
        res.recommendation = ""
    return res
