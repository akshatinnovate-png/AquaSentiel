"""Site calibration.

Every threshold in AquaSentinel lives here, per site, and *nothing* in the
detection code hard-codes a number that claims to be a public-safety limit.
That is a deliberate honesty constraint: the danger depth at one underpass
says nothing about the next one, so the engine refuses to invent it.  The
defaults below are calibration values for the demonstration rig only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class FloodConfig:
    """Thresholds for the monitored low point.

    `warn_cm` / `danger_cm` must come from a survey of the actual location.
    The values here describe our bench rig's 30 cm channel.
    """

    enabled: bool = True
    sensor_mount_cm: float = 30.0      # distance from sensor face to dry bed
    watch_cm: float = 6.0              # water present and worth watching
    warn_cm: float = 12.0              # approaching locally-defined danger
    danger_cm: float = 18.0            # locally-surveyed unsafe depth
    rise_watch_cm_min: float = 0.4     # persistent rise
    rise_warn_cm_min: float = 1.0      # fast rise
    rise_critical_cm_min: float = 2.5  # very fast rise
    rain_intensity_watch_mmhr: float = 7.5   # IMD "moderate" rain, per hour
    rain_intensity_warn_mmhr: float = 22.0   # IMD "heavy" rain, per hour
    smooth_window: int = 5             # causal median filter width, samples
    slope_window_s: int = 180          # rate-of-rise regression window
    dwell_samples: int = 2             # consecutive samples before escalating
    clear_hysteresis_cm: float = 2.0   # must fall this far below to clear
    min_r2_for_trend: float = 0.5      # below this the "trend" is just noise


@dataclass
class LeakConfig:
    """Flow-anomaly thresholds for the service line."""

    enabled: bool = True
    zero_flow_lpm: float = 0.08        # below this the meter reads 'no flow'
    expected_idle_hours: tuple[int, ...] = (0, 1, 2, 3, 4, 23)  # local hours
    min_night_flow_lpm: float = 0.25   # sustained idle-hour flow that concerns us
    continuous_flow_minutes: float = 45.0   # non-stop flow that concerns us
    excess_factor: float = 2.5         # multiple of the learned baseline p95
    excess_minutes: float = 20.0       # how long excess must persist
    baseline_min_samples: int = 12     # per hour-of-day bucket
    history_hours: int = 24 * 14       # how much history the baseline learns from
    dwell_samples: int = 3
    score_alert: float = 0.55          # 0..1 evidence score that raises an alert
    score_warning: float = 0.75


@dataclass
class TankConfig:
    """Storage-tank geometry and calibration.

    `level_to_litres` is the dip-chart: a list of [level_cm, litres] pairs
    measured by filling the tank in steps.  Supplying it is what turns a
    distance reading into a volume we are willing to show a caretaker.
    """

    enabled: bool = True
    capacity_l: float = 5000.0         # a common school rooftop tank
    height_cm: float = 180.0
    sensor_mount_cm: float = 195.0     # sensor face to tank floor
    shape: str = "vertical_cylinder"   # vertical_cylinder | rectangular | curve
    level_to_litres: list[list[float]] = field(default_factory=list)
    low_pct: float = 25.0              # plan a refill
    critical_pct: float = 10.0         # act now
    refill_detect_lpm: float = 4.0      # inflow rate that counts as a refill
    # A refill must show as a sustained, statistically significant rise. A
    # single 0.3 cm ultrasonic jitter is ~8 L on a 5000 L tank, which across a
    # 15 s gap looks like 32 L/min of inflow -- eight times this threshold.
    refill_window_s: int = 300
    refill_min_samples: int = 6
    consumption_window_s: int = 3600   # trailing window for the usage trend
    # Noise guards. With a 5000 L / 180 cm tank, 1 cm of ultrasonic noise is
    # ~28 L, so a short window can manufacture an enormous false "draw".
    # We require a real span, enough samples, and a slope that is
    # statistically distinguishable from zero.
    min_fit_span_s: int = 900           # at least 15 min of record
    min_fit_samples: int = 10
    slope_significance: float = 1.96    # sigmas the slope must clear
    min_consumption_lph: float = 2.0   # below this an ETA is meaningless
    eta_warn_hours: float = 6.0
    eta_critical_hours: float = 2.0
    dwell_samples: int = 4


@dataclass
class DemandConfig:
    """Heat and demand model settings."""

    enabled: bool = True
    # Absolute heat-index bands, aligned with the NWS categories
    # ("Danger" begins at 41 C, "Extreme Danger" at 54 C). Local advisory
    # levels, not an official heatwave declaration.
    heat_index_watch_c: float = 41.0
    heat_index_warn_c: float = 45.0
    heat_index_critical_c: float = 50.0
    # A hot afternoon is normal in October in much of India, so an absolute
    # threshold alone produces constant noise. Following IMD practice we also
    # require a departure from what this site normally sees at this hour.
    heat_anomaly_watch_c: float = 2.0
    # Above this, the heat index is dangerous regardless of what is "normal"
    # here, so the anomaly requirement is waived.
    heat_index_absolute_c: float = 47.0
    forecast_horizon_h: int = 6
    baseline_days: int = 14
    min_train_samples: int = 48
    holdout_fraction: float = 0.25
    ridge_l2: float = 0.5
    # A weather-aware model is only used if it beats the seasonal-naive
    # baseline on held-out data by at least this fraction of its MAE.
    model_adoption_margin: float = 0.05


# At what severity does a module deserve a notification, as opposed to simply
# being visible on the dashboard?
#
# Flood and node health page at WATCH because they are measurements of a
# condition that can hurt someone or that invalidates other readings. Tank and
# demand page at WARNING because their WATCH state is a projection, and a
# projection that pages every lunchtime trains people to ignore the system.
ALERT_FROM_SEVERITY: dict[str, int] = {
    "flood": 2,
    "leak": 2,
    # The tank's WATCH state is ETA-driven, and a short ETA during a demand
    # peak is routine, so it stays on the dashboard rather than paging.
    "tank": 3,
    # Heat WATCH does page: it requires both an absolute advisory level and a
    # departure from this site's own normal peak, which measured zero false
    # alarms across a 24 h quiet-site run, and "demand will be up today" is
    # exactly the kind of warning a water manager can act on.
    "demand": 2,
    "device": 2,
}


@dataclass
class SiteConfig:
    """Everything the engine needs to know about one monitored site."""

    site_id: str = "demo-site"
    name: str = "Demo School Campus"
    timezone_offset_h: float = 5.5     # IST; the engine is otherwise UTC-only
    sample_interval_s: int = 15
    stale_after_s: int = 120           # no fresh sample => mark readings stale
    flood: FloodConfig = field(default_factory=FloodConfig)
    leak: LeakConfig = field(default_factory=LeakConfig)
    tank: TankConfig = field(default_factory=TankConfig)
    demand: DemandConfig = field(default_factory=DemandConfig)
    devices: dict[str, dict] = field(default_factory=dict)
    calibrated: bool = False           # set true only after a real calibration
    calibration_note: str = (
        "Demonstration-rig values. Flood and tank thresholds must be re-surveyed "
        "before any real deployment; they are not public-safety limits."
    )

    def as_dict(self) -> dict:
        d = asdict(self)
        d["leak"]["expected_idle_hours"] = list(self.leak.expected_idle_hours)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SiteConfig":
        if not d:
            return cls()
        sub = {
            "flood": FloodConfig,
            "leak": LeakConfig,
            "tank": TankConfig,
            "demand": DemandConfig,
        }
        kwargs: dict[str, Any] = {}
        for key, val in d.items():
            if key in sub and isinstance(val, dict):
                known = {f for f in sub[key].__dataclass_fields__}
                clean = {k: v for k, v in val.items() if k in known}
                if key == "leak" and "expected_idle_hours" in clean:
                    clean["expected_idle_hours"] = tuple(int(h) for h in clean["expected_idle_hours"])
                kwargs[key] = sub[key](**clean)
            elif key in cls.__dataclass_fields__:
                kwargs[key] = val
        return cls(**kwargs)

    def local_hour(self, ts: float) -> int:
        """Hour-of-day at the site. Baselines are only meaningful in local time."""
        return int(((ts + self.timezone_offset_h * 3600) // 3600) % 24)

    def local_dow(self, ts: float) -> int:
        """0=Monday .. 6=Sunday, at the site."""
        import datetime

        local = datetime.datetime.fromtimestamp(
            ts + self.timezone_offset_h * 3600, datetime.timezone.utc
        )
        return local.weekday()


DEFAULT_SITE = SiteConfig(
    devices={
        "aquasentinel-node-01": {
            "label": "Rooftop tank + service line",
            "location": {"x": 0.62, "y": 0.34, "place": "Block A rooftop"},
            "sensors": ["tank_level_cm", "flow_lpm", "temperature_c", "humidity_pct"],
        },
        "aquasentinel-node-02": {
            "label": "North gate low point",
            "location": {"x": 0.24, "y": 0.68, "place": "North gate underpass"},
            "sensors": ["street_water_cm", "rain_mm", "rain_rate_mmhr"],
        },
    }
)
