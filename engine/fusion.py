"""Cross-module fusion -- the part that makes this one system, not four tabs.

Three jobs:

1. **Supply outlook.** Re-project tank depletion using the *forecast* demand
   from Module D instead of the current draw from Module C.  This is the one
   genuinely new number that neither module can produce alone, and it is the
   one a caretaker actually acts on: "at today's heat, you run dry before the
   evening, not tomorrow morning."

2. **Co-occurrence.** Report that signals are happening together, with
   deliberately careful wording.  Heat did not cause the leak; rain does not
   guarantee a flood.  We surface the coincidence and let a human judge, and
   the phrasing in `COOCCURRENCE` is written to make that impossible to
   misread.

3. **Inspection priority.** One ranked queue across all modules, because a
   caretaker has one pair of hands.  Rank = severity x confidence x how much
   a visit would change the outcome.
"""
from __future__ import annotations

from engine.config import SiteConfig
from engine.model import ModuleResult, Severity, Signal
from engine.stats import clamp

# Impact weight: how much does a human visit improve the outcome?  A critical
# flood reading needs people kept away (high), a heat advisory mostly needs
# planning (low).
IMPACT = {"flood": 1.0, "leak": 0.8, "tank": 0.75, "demand": 0.4, "device": 0.35}


def _r(results: dict[str, ModuleResult], module: str, signal: str, default=None):
    m = results.get(module)
    return default if m is None else m.value(signal, default)


def supply_outlook(results: dict[str, ModuleResult], cfg: SiteConfig) -> dict:
    """Project depletion under forecast demand rather than the current draw."""
    remaining = _r(results, "tank", "remaining_l")
    current_rate = _r(results, "tank", "consumption_lph")
    forecast_total = _r(results, "demand", "forecast_demand_l_next_h")
    demand_mod = results.get("demand")
    horizon = cfg.demand.forecast_horizon_h

    out: dict = {
        "remaining_l": remaining,
        "current_rate_lph": current_rate,
        "eta_current_h": _r(results, "tank", "eta_depletion_h"),
        "eta_forecast_h": None,
        "forecast_rate_lph": None,
        "horizon_h": horizon,
        "shortfall_l": None,
        "basis": "none",
        "note": "",
    }
    if remaining is None:
        out["note"] = "No tank instrumented on this node."
        return out
    if remaining <= 1.0:
        # Checked before the forecast: an empty tank makes the projection
        # irrelevant whether or not a forecast exists, and the alternative
        # wording was "covers it with about 0 L to spare".
        out["note"] = "The tank is empty. There is nothing left to project."
        out["eta_current_h"] = 0.0
        out["eta_forecast_h"] = 0.0
        return out
    if forecast_total is None or horizon <= 0:
        out["basis"] = "current-draw only"
        out["note"] = "Not enough demand history to forecast; showing the current draw only."
        return out

    fc_rate = forecast_total / horizon
    out["forecast_rate_lph"] = round(fc_rate, 2)
    src = "ridge model" if (demand_mod and (demand_mod.signal("forecast_demand_l_next_h") or Signal("", None)).detail.get("validation", {}).get("adopted")) else "seasonal-naive baseline"
    out["basis"] = src

    # Walk the forecast hour by hour so an uneven profile is respected --
    # averaging it would hide an afternoon peak, which is the whole point.
    points = []
    if demand_mod:
        sig = demand_mod.signal("forecast_series")
        if sig:
            points = sig.detail.get("points") or []

    vol = remaining
    hours = 0.0
    emptied = False
    for p in points:
        draw = float(p.get("litres_per_h") or 0.0)
        if draw <= 0:
            hours += 1.0
            continue
        if vol <= draw:
            hours += vol / draw
            vol = 0.0
            emptied = True
            break
        vol -= draw
        hours += 1.0

    if emptied:
        out["eta_forecast_h"] = round(hours, 2)
        out["beyond_horizon"] = False
    else:
        # The tank survives the whole forecast window. Dividing the remaining
        # volume by the final hour's rate here once produced an ETA of 2156
        # hours -- three months -- because that hour happened to be the middle
        # of the night. A six-hour forecast cannot support a three-month claim,
        # so we say what we actually know: it lasts past the horizon.
        out["eta_forecast_h"] = None
        out["beyond_horizon"] = True
        out["survives_horizon_h"] = horizon
        out["remaining_after_horizon_l"] = round(vol, 1)

    out["shortfall_l"] = round(max(0.0, forecast_total - remaining), 1)

    eta_c, eta_f = out["eta_current_h"], out["eta_forecast_h"]
    if eta_f is not None and eta_c and eta_f < eta_c * 0.85:
        out["note"] = (
            f"Forecast demand is higher than the current draw, so the tank is projected to run low "
            f"in about {eta_f:.1f} h rather than {eta_c:.1f} h. Estimate, from the {src}."
        )
    elif eta_f is not None:
        out["note"] = f"Projected to last about {eta_f:.1f} h under forecast demand. Estimate, from the {src}."
    else:
        kept = out.get("remaining_after_horizon_l")
        tail = (f" Beyond that the forecast does not reach; at the current draw the estimate is "
                f"{eta_c:.1f} h." if eta_c else
                " Beyond that the forecast does not reach, and the current draw is too low to "
                "project from.")
        out["note"] = (
            f"Forecast demand over the next {horizon} h is {forecast_total:.0f} L, which this "
            f"tank covers"
            + (f" with about {kept:.0f} L to spare." if kept is not None else ".")
            + tail
        )
    return out


# Each entry: (condition, headline, careful wording).
COOCCURRENCE = [
    (
        lambda r: (_r(r, "demand", "heat_index_c") or 0) >= 38 and (_r(r, "tank", "fill_pct") or 100) <= 40,
        "High heat while stored supply is already low",
        "Both conditions are present at the same time. Heat raises expected demand and the tank is "
        "below 40%. This is a co-occurrence worth planning around, not evidence that one caused the other.",
    ),
    (
        lambda r: (r.get("leak") or ModuleResult("leak")).severity >= Severity.WATCH
        and (_r(r, "tank", "fill_pct") or 100) <= 40,
        "Possible water loss while supply is low",
        "Unexplained flow is being detected while the tank is low. If the flow is real loss it is "
        "shortening the time until the tank is empty. Verify the flow before assuming either.",
    ),
    (
        lambda r: (_r(r, "flood", "rain_rate_mmhr") or 0) >= 7.5
        and (r.get("flood") or ModuleResult("flood")).severity >= Severity.WATCH,
        "Rain falling and the monitored low point is wet",
        "Rainfall and rising water at the instrumented point are both present. Rainfall corroborates "
        "the level reading; it does not by itself mean a flood will follow.",
    ),
    (
        lambda r: (r.get("leak") or ModuleResult("leak")).severity >= Severity.WATCH
        and (_r(r, "demand", "heat_index_c") or 0) >= 38,
        "Unusual flow during a high-heat period",
        "High heat genuinely raises water use, so some of this extra flow may be legitimate demand. "
        "Treat the loss score with extra caution today and verify on site.",
    ),
]


def inspection_queue(results: dict[str, ModuleResult], device_id: str) -> list[dict]:
    """One ranked list of what a caretaker should look at first."""
    items: list[dict] = []
    for module, res in results.items():
        if res is None or res.severity <= Severity.INFO:
            continue
        impact = IMPACT.get(module, 0.5)
        score = (int(res.severity) / 4.0) * clamp(res.confidence, 0.1, 1.0) * impact
        items.append({
            "module": module,
            "device_id": device_id,
            "severity": int(res.severity),
            "severity_label": res.severity.label,
            "confidence": round(res.confidence, 3),
            "priority": round(score, 4),
            "what": res.title,
            "why": res.reason,
            "action": res.recommendation or "Monitor.",
        })
    items.sort(key=lambda i: (-i["priority"], -i["severity"]))
    for n, item in enumerate(items, 1):
        item["rank"] = n
    return items


def situation(results: dict[str, ModuleResult], cfg: SiteConfig, device_id: str) -> dict:
    """The one object the dashboard's Overview page renders."""
    live = {k: v for k, v in results.items() if v is not None}
    overall = max((v.severity for v in live.values()), default=Severity.OK)

    notes: list[dict] = []
    for cond, headline, wording in COOCCURRENCE:
        try:
            if cond(live):
                notes.append({"headline": headline, "detail": wording})
        except Exception:  # a missing signal must never break the overview
            continue

    outlook = supply_outlook(live, cfg)
    queue = inspection_queue(live, device_id)

    if overall >= Severity.CRITICAL:
        headline = "Critical: immediate attention needed"
    elif overall == Severity.WARNING:
        headline = "Warning: action needed today"
    elif overall == Severity.WATCH:
        headline = "Watch: keep an eye on it"
    else:
        headline = "All monitored risks normal"

    return {
        "device_id": device_id,
        "site_id": cfg.site_id,
        "site_name": cfg.name,
        "overall_severity": int(overall),
        "overall_label": overall.label,
        "headline": headline,
        "modules": {k: v.as_dict() for k, v in live.items()},
        "cooccurrence": notes,
        "supply_outlook": outlook,
        "inspection_queue": queue,
        "calibrated": cfg.calibrated,
        "calibration_note": cfg.calibration_note,
    }
