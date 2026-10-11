"""Demonstration and test scenarios.

Each scenario drives the physical models over time and -- crucially -- declares
its own **ground truth**: the windows during which a condition genuinely
exists.  The evaluation harness scores the engine against those windows, which
is how we can report a detection delay and a false-alarm count instead of a
screenshot.

A scenario never touches the engine, and the engine never sees the scenario.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

from sim.node import VirtualNode, build_default_nodes
from sim.sensors import ClimateModel, FlowModel, RainModel, StreetWaterModel, TankModel

# A scenario's clock starts at midnight IST, so the engine's local-hour logic
# lines up with the scenario's own sense of time.
#
# Two modes, deliberately:
#   * FIXED_EPOCH -- a constant date, used by the evaluation harness so a run
#     today and a run next month produce identical numbers.
#   * anchor_to_now() -- today's midnight, used for live demos. Without it the
#     dashboard correctly but uselessly reported every node's clock as being a
#     year out, because the simulated clock really was.
FIXED_EPOCH = 1760313600.0 - 5.5 * 3600.0   # 2025-10-13 00:00 IST
TZ_OFFSET_H = 5.5
EPOCH = FIXED_EPOCH


def anchor_to_now(start_hour_local: float = 0.0) -> float:
    """Set the clock so a scenario beginning at `start_hour_local` starts now.

    The scenario keeps its own sense of local time -- the demo still opens at
    18:00, because that is what makes its narrative run in order -- while the
    timestamps it emits begin at the current instant rather than on a fixed
    date in the past.
    """
    global EPOCH
    import time as _time

    EPOCH = _time.time() - start_hour_local * 3600.0
    return EPOCH


def use_fixed_epoch() -> float:
    global EPOCH
    EPOCH = FIXED_EPOCH
    return EPOCH

# A 1 HP domestic booster pump delivers roughly this into a rooftop tank.
PUMP_LPM = 60.0


def _set_tank_pct(nodes: dict[str, "VirtualNode"], pct: float) -> None:
    """Set the start level as a percentage, so changing capacity cannot
    silently turn a scenario into a different test."""
    tank = nodes["aquasentinel-node-01"].sensors["tank"]
    tank.volume_l = tank.capacity_l * pct / 100.0


@dataclass
class Truth:
    """A window during which a condition genuinely exists.

    Two ways to declare it:

    * a fixed window, for a condition we *inject* (a leak we switched on, a
      heat offset we applied) -- we know exactly when it started;
    * a `predicate` over the simulator's internal state, for a condition the
      physics decides (water actually ponding, a tank actually below 40%).

    The predicate form matters for honesty. Hand-guessing "ponding starts at
    14 minutes" and then measuring the delay against that guess measures the
    guess. Reading the simulator's true depth and timing the alert against the
    moment it genuinely crossed the threshold measures the detector.
    """

    module: str
    start_s: float
    end_s: float
    label: str
    min_severity: int = 2    # the severity we expect the engine to reach
    predicate: Callable[[dict[str, "VirtualNode"]], bool] | None = None
    # A looser condition under which an alert is *early* rather than wrong.
    # Water at 2 cm and climbing is a legitimate thing to report even though
    # the window we measure the delay against opens at 6 cm; counting that as
    # a false alarm would penalise the system for warning sooner.
    precursor: Callable[[dict[str, "VirtualNode"]], bool] | None = None

    def contains(self, elapsed: float) -> bool:
        return self.start_s <= elapsed <= self.end_s

    @property
    def observed(self) -> bool:
        return self.predicate is not None


@dataclass
class Scenario:
    name: str
    description: str
    duration_s: float
    interval_s: float = 15.0
    # Local hour the scenario begins at. A depletion test that starts at
    # midnight spends its first six hours with the site asleep and the tank
    # barely moving, which measures nothing.
    start_hour_local: float = 0.0
    truth: list[Truth] = field(default_factory=list)
    apply: Callable[[float, float, dict[str, VirtualNode]], None] = lambda t, e, n: None
    nodes_factory: Callable[[], dict[str, VirtualNode]] = build_default_nodes
    claim: str = ""          # what we are entitled to claim if this passes

    def build(self) -> dict[str, VirtualNode]:
        return self.nodes_factory()

    @property
    def start_epoch(self) -> float:
        # Read EPOCH at access time, not at construction, so anchor_to_now()
        # applies to scenarios built before it was called.
        return EPOCH + self.start_hour_local * 3600.0


# --------------------------------------------------------------------------
# Shared demand profile
# --------------------------------------------------------------------------

def school_demand_lpm(local_hour: float, heat_offset_c: float = 0.0,
                      rng: random.Random | None = None) -> float:
    """Water draw for a school campus, by hour of day.

    Two peaks (morning assembly and lunch), near-zero overnight, and a genuine
    heat sensitivity so Module D has a real relationship to find rather than
    one we asserted.
    """
    peaks = [(8.5, 1.6, 6.0), (12.5, 2.0, 7.5), (16.0, 1.2, 3.5)]
    draw = 0.05  # small constant: cistern top-up, drinking points
    for centre, width, amp in peaks:
        draw += amp * math.exp(-((local_hour - centre) ** 2) / (2 * width ** 2))
    if local_hour < 6.0 or local_hour > 21.0:
        draw *= 0.08
    # Heat sensitivity: about +6% per degree of heat anomaly, afternoon-weighted.
    if heat_offset_c > 0 and 10.0 <= local_hour <= 19.0:
        draw *= 1.0 + 0.06 * heat_offset_c
    if rng:
        draw *= max(0.0, 1.0 + rng.gauss(0.0, 0.12))
    return max(0.0, draw)


def _local_hour(t: float, tz: float = 5.5) -> float:
    return ((t + tz * 3600) / 3600.0) % 24.0


# -- predicates over the simulator's true internal state -------------------
# These read the physical models directly, never the sensor output, so sensor
# noise cannot move the ground truth we score against.

def ponding(threshold_cm: float = 6.0) -> Callable[[dict], bool]:
    def pred(nodes: dict) -> bool:
        street = nodes["aquasentinel-node-02"].sensors.get("street")
        return bool(street is not None and street.depth_cm >= threshold_cm)
    return pred


def tank_below(pct: float = 40.0) -> Callable[[dict], bool]:
    def pred(nodes: dict) -> bool:
        tank = nodes["aquasentinel-node-01"].sensors.get("tank")
        return bool(tank is not None and tank.true_pct <= pct)
    return pred


def heat_applied(min_offset_c: float = 1.0) -> Callable[[dict], bool]:
    def pred(nodes: dict) -> bool:
        clim = nodes["aquasentinel-node-01"].sensors.get("climate")
        return bool(clim is not None and clim.heat_offset_c >= min_offset_c)
    return pred


def heat_index_above(threshold_c: float) -> Callable[[dict], bool]:
    """True while the site's TRUE heat index exceeds a threshold.

    Timing a heat alert from midnight on a day that happens to be hot
    measures the calendar, not the detector: the heat index does not cross an
    advisory level until the afternoon, so the "delay" came out at 12 hours.
    This predicate opens the window at the moment the air genuinely gets that
    hot, computed from the model's true temperature and humidity rather than
    from the noisy sensor output.
    """
    from engine.demand import heat_index_c

    def pred(nodes: dict) -> bool:
        clim = nodes["aquasentinel-node-01"].sensors.get("climate")
        if clim is None:
            return False
        return heat_index_c(clim.true_temp_c, clim.true_rh) >= threshold_c
    return pred


def _drive_consumption(t: float, nodes: dict[str, VirtualNode], rng: random.Random,
                       extra_lpm: float = 0.0) -> None:
    """Couple the tank, the flow meter and the demand profile.

    The flow meter measures what leaves the tank.  Keeping them coupled is what
    makes the cross-module story (heat -> demand -> depletion) real rather than
    a pair of unrelated graphs.
    """
    node = nodes.get("aquasentinel-node-01")
    if node is None:
        return
    tank: TankModel = node.sensors["tank"]
    flow: FlowModel = node.sensors["flow"]
    clim: ClimateModel = node.sensors["climate"]
    hour = _local_hour(t)
    draw = school_demand_lpm(hour, clim.heat_offset_c, rng) + extra_lpm
    flow.true_lpm = draw
    tank.outflow_lpm = draw


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def scenario_baseline(hours: float = 72.0) -> Scenario:
    """Several normal days. Produces the history every baseline needs.

    This is also our false-alarm test: on a quiet site the engine should raise
    essentially nothing, and any alert it does raise is a false positive we
    have to report.
    """
    rng = random.Random(101)

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        _drive_consumption(t, nodes, rng)
        node1 = nodes["aquasentinel-node-01"]
        tank: TankModel = node1.sensors["tank"]
        hour = _local_hour(t)
        # An automatic float-switch pump, topping up whenever the tank falls
        # below half, at any hour. This is what an ordinary working site has,
        # and it is what makes this scenario a fair false-alarm test: an
        # earlier version only pumped between 02:00 and 05:00, so a campus
        # using 4300 L/day out of a 5000 L tank ran down to 14% every single
        # afternoon. The engine correctly warned about that, and the warnings
        # were then counted as false alarms -- the scenario was wrong, not the
        # detector. The failure of this pump is what the depletion scenario is.
        if tank.true_pct < 50.0:
            tank.inflow_lpm = PUMP_LPM
        elif tank.true_pct > 92.0:
            tank.inflow_lpm = 0.0
        elif tank.inflow_lpm <= 0:
            tank.inflow_lpm = 0.0
        # A little light rain, far below anything that ponds.
        rain: RainModel = nodes["aquasentinel-node-02"].sensors["rain"]
        rain.intensity_mmhr = 1.2 if 0.35 < (hour / 24.0) % 0.5 < 0.37 else 0.0

    return Scenario(
        name="baseline",
        description=(f"{hours:.0f} h of normal campus operation: diurnal demand, an automatic "
                     f"float-switch refill, light rain. Nothing should be reported."),
        duration_s=hours * 3600,
        truth=[],
        apply=apply,
        claim="Any alert raised here is a false alarm, and is counted as one.",
    )


def scenario_flood(minutes: float = 90.0) -> Scenario:
    """A short, intense downpour onto a partly blocked drain.

    The blockage is the realistic part: a clear drain at this rainfall would
    not pond at all, and we want to detect the case that actually hurts.
    """
    rng = random.Random(202)
    rain_start, rain_end = 10 * 60, 45 * 60
    block_start = 15 * 60

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        _drive_consumption(t, nodes, rng)
        n2 = nodes["aquasentinel-node-02"]
        rain: RainModel = n2.sensors["rain"]
        street: StreetWaterModel = n2.sensors["street"]
        if rain_start <= elapsed <= rain_end:
            # Ramp up to ~45 mm/h, which the IMD would call heavy rain.
            frac = (elapsed - rain_start) / max(1.0, (rain_end - rain_start))
            rain.intensity_mmhr = 45.0 * math.sin(math.pi * min(1.0, frac))
        else:
            rain.intensity_mmhr = 0.0
        street.blocked = 0.75 if elapsed >= block_start else 0.0

    return Scenario(
        name="flood",
        description="35 min of heavy rain (peak ~45 mm/h) onto a 75% blocked drain at the north gate.",
        duration_s=minutes * 60,
        interval_s=10.0,
        truth=[
            # Observed, not assumed: the window is however long the simulated
            # channel actually holds 6 cm or more of water.
            Truth("flood", 0, minutes * 60, "water genuinely ponding (>=6 cm) at the low point",
                  min_severity=2, predicate=ponding(6.0), precursor=ponding(1.0)),
        ],
        apply=apply,
        claim="Detection of rising water at an instrumented point, with a measured delay. Not a flood forecast.",
    )


def scenario_leak(hours: float = 10.0) -> Scenario:
    """A small overnight leak on the service line.

    0.6 L/min is deliberately modest: it is invisible to a caretaker, it is
    only ~36 L/h, and it is the regime where automated detection earns its
    keep. It starts in the idle hours so minimum-night-flow can see it.
    """
    rng = random.Random(303)
    leak_start_h, leak_rate = 1.0, 0.6

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        extra = leak_rate if elapsed >= leak_start_h * 3600 else 0.0
        _drive_consumption(t, nodes, rng, extra_lpm=extra)
        tank: TankModel = nodes["aquasentinel-node-01"].sensors["tank"]
        tank.inflow_lpm = 0.0

    return Scenario(
        name="leak",
        description=f"A {leak_rate} L/min leak starting {leak_start_h:.0f} h in, during the expected-idle hours.",
        duration_s=hours * 3600,
        start_hour_local=0.0,
        truth=[Truth("leak", leak_start_h * 3600, hours * 3600, "continuous unexplained flow", min_severity=2)],
        apply=apply,
        claim="Detection of sustained unexplained flow. The alert says 'possible loss' and must be verified.",
    )


def scenario_depletion(hours: float = 14.0) -> Scenario:
    """A refill that never arrives on a busy day."""
    rng = random.Random(404)

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        _drive_consumption(t, nodes, rng)
        nodes["aquasentinel-node-01"].sensors["tank"].inflow_lpm = 0.0

    def nodes_factory() -> dict[str, VirtualNode]:
        nodes = build_default_nodes()
        _set_tank_pct(nodes, 42.0)
        return nodes

    return Scenario(
        name="depletion",
        description="From 06:00, the tank starts at 42% and no refill arrives while normal campus demand continues.",
        duration_s=hours * 3600,
        start_hour_local=6.0,
        truth=[Truth("tank", 0, hours * 3600,
                     "tank genuinely at or below its configured low threshold (25%)",
                     min_severity=2, predicate=tank_below(25.0),
                     precursor=tank_below(40.0))],
        apply=apply,
        nodes_factory=nodes_factory,
        claim="Calibrated volume and a depletion ETA with a stated range. Accuracy is measured, not claimed.",
    )


def scenario_heatwave(hours: float = 36.0) -> Scenario:
    """A hot spell that lifts demand while the tank is already middling.

    This is the scenario that exercises fusion: Module D's forecast feeds
    Module C's projection, and the honest result is an earlier depletion time
    than the current draw alone implies.
    """
    rng = random.Random(505)
    heat_start_h = 12.0

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        clim: ClimateModel = nodes["aquasentinel-node-01"].sensors["climate"]
        clim.heat_offset_c = 7.5 if elapsed >= heat_start_h * 3600 else 0.0
        _drive_consumption(t, nodes, rng)
        tank: TankModel = nodes["aquasentinel-node-01"].sensors["tank"]
        hour = _local_hour(t)
        tank.inflow_lpm = PUMP_LPM if (tank.true_pct < 35.0 and 2.0 <= hour <= 4.0) else 0.0

    def nodes_factory() -> dict[str, VirtualNode]:
        nodes = build_default_nodes()
        _set_tank_pct(nodes, 60.0)
        return nodes

    return Scenario(
        name="heatwave",
        description="A +7.5 C heat spell from hour 12, raising afternoon demand against a 60% tank.",
        duration_s=hours * 3600,
        truth=[
            Truth("demand", 0, hours * 3600,
                  "site's true heat index at or above the 41 C advisory level",
                  min_severity=2, predicate=heat_index_above(41.0), precursor=heat_applied(1.0)),
            # This scenario really does run the supply down -- that is the
            # point of it -- so the supply warnings it produces are correct
            # and must be declared, not counted against the engine.
            Truth("tank", 0, hours * 3600,
                  "tank genuinely at or below its configured low threshold (25%)",
                  min_severity=2, predicate=tank_below(25.0), precursor=tank_below(45.0)),
        ],
        apply=apply,
        nodes_factory=nodes_factory,
        claim="Heat index against a local advisory level, plus a demand forecast scored against a baseline.",
    )


def scenario_outage(minutes: float = 60.0) -> Scenario:
    """The uplink drops for 12 minutes during a rising-water event.

    The node must buffer, the backend must accept the back-fill, and the
    dashboard must not present the back-filled readings as the live state.
    """
    rng = random.Random(606)
    off_s, on_s = 20 * 60, 32 * 60

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        _drive_consumption(t, nodes, rng)
        n2 = nodes["aquasentinel-node-02"]
        rain: RainModel = n2.sensors["rain"]
        street: StreetWaterModel = n2.sensors["street"]
        rain.intensity_mmhr = 38.0 if 10 * 60 <= elapsed <= 40 * 60 else 0.0
        street.blocked = 0.7 if elapsed >= 12 * 60 else 0.0
        for node in nodes.values():
            node.set_online(not (off_s <= elapsed < on_s))

    return Scenario(
        name="outage",
        description="A 12-minute uplink outage in the middle of a rising-water event; records are buffered and replayed.",
        duration_s=minutes * 60,
        interval_s=10.0,
        truth=[Truth("flood", 0, minutes * 60, "water genuinely ponding across the outage",
                     min_severity=2, predicate=ponding(6.0), precursor=ponding(1.0))],
        apply=apply,
        claim="No readings are lost across an outage, and replayed records are labelled as back-filled.",
    )


def scenario_sensor_fault(minutes: float = 45.0) -> Scenario:
    """The tank sensor is disconnected, then the street sensor sticks.

    The required behaviour is that the system says "I do not know", not that it
    reports 0 cm of water, which would be the dangerous failure mode.
    """
    rng = random.Random(707)

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        _drive_consumption(t, nodes, rng)
        n1, n2 = nodes["aquasentinel-node-01"], nodes["aquasentinel-node-02"]
        n1.inject_sensor_fault("tank", "disconnected" if 10 * 60 <= elapsed < 25 * 60 else "none")
        n2.inject_sensor_fault("street", "stuck" if elapsed >= 30 * 60 else "none")

    return Scenario(
        name="sensor_fault",
        description="Tank sensor disconnected for 15 min, then the street sensor freezes at a fixed value.",
        duration_s=minutes * 60,
        interval_s=10.0,
        truth=[Truth("device", 10 * 60, 25 * 60, "tank sensor not reporting", min_severity=2)],
        apply=apply,
        claim="A failed sensor is reported as unavailable. It is never silently treated as a zero reading.",
    )


def scenario_longhaul(days: float = 14.0) -> Scenario:
    """Two weeks of operation with day-to-day weather variation.

    This scenario exists for one reason: to give the weather-aware demand model
    a fair test. On a one-day run the seasonal-naive baseline has roughly a
    single sample per hour-of-week bucket, so neither model has anything to
    learn and comparing them says nothing. Here there are around 336 hourly
    observations across fourteen days, with each day drawing its own heat
    offset, so demand genuinely co-varies with temperature and the ridge model
    has a real relationship to find -- or to fail to find, which is an equally
    publishable result.
    """
    rng = random.Random(909)
    # Each day gets its own heat offset: mostly ordinary, a few hot spells.
    day_offsets = [0.0] + [
        rng.choice([0.0, 0.0, 0.5, 1.5, 3.0, 5.0, 7.0, 9.0]) for _ in range(int(days) + 2)
    ]

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        day = int(elapsed // 86400)
        n1 = nodes["aquasentinel-node-01"]
        clim: ClimateModel = n1.sensors["climate"]
        tank: TankModel = n1.sensors["tank"]
        clim.heat_offset_c = day_offsets[min(day, len(day_offsets) - 1)]
        _drive_consumption(t, nodes, rng)
        # A working float-switch pump, as in the baseline scenario.
        if tank.true_pct < 50.0:
            tank.inflow_lpm = PUMP_LPM
        elif tank.true_pct > 92.0:
            tank.inflow_lpm = 0.0
        rain: RainModel = nodes["aquasentinel-node-02"].sensors["rain"]
        hour = _local_hour(t)
        # Occasional genuine rain, never enough to pond on a clear drain.
        rain.intensity_mmhr = 6.0 if (day % 4 == 2 and 16.0 <= hour <= 17.5) else 0.0

    return Scenario(
        name="longhaul",
        description=(
            f"{days:.0f} days of operation with a different heat offset each day, so the demand "
            f"forecaster can be scored on real history rather than on a single day."
        ),
        duration_s=days * 86400,
        interval_s=60.0,
        start_hour_local=0.0,
        truth=[
            Truth("demand", 0, days * 86400,
                  "site's true heat index at or above the 41 C advisory level",
                  min_severity=2, predicate=heat_index_above(41.0),
                  precursor=heat_applied(0.4)),
        ],
        apply=apply,
        claim=(
            "A forecast comparison with enough history to mean something: a seasonal-naive "
            "baseline against a weather-aware ridge model, scored on held-out hours."
        ),
    )


def scenario_demo(hours: float = 30.0) -> Scenario:
    """The full story for the three-minute video, as one continuous day.

    It starts at 18:00 so the narrative runs in the order a caretaker would
    actually live it:

        18:00  normal evening, tank already down to 48%
        00:00  a 0.9 L/min leak starts in the quiet hours
        06:00  the morning refill never comes
        13:00  a hot spell lifts afternoon demand
        17:00  a downpour onto a blocked drain at the north gate
        00:00  thirty hours in, the picture is complete

    Running the clock rather than compressing the physics is the point: every
    detector sees the regime it was designed for -- minimum night flow in the
    quiet hours, a real diurnal demand curve, a real heat departure from the
    previous day's peak. We speed up the *playback* for the video (`--speed`),
    and say so on screen, instead of speeding up the water.
    """
    rng = random.Random(808)
    leak_start_h, heat_start_h = 6.0, 19.0
    rain_start_h, rain_end_h = 23.0, 25.5
    leak_lpm = 0.9

    def apply(t: float, elapsed: float, nodes: dict[str, VirtualNode]) -> None:
        h = elapsed / 3600.0
        n1, n2 = nodes["aquasentinel-node-01"], nodes["aquasentinel-node-02"]
        clim: ClimateModel = n1.sensors["climate"]
        tank: TankModel = n1.sensors["tank"]
        rain: RainModel = n2.sensors["rain"]
        street: StreetWaterModel = n2.sensors["street"]

        clim.heat_offset_c = 10.0 if h >= heat_start_h else 0.0
        _drive_consumption(t, nodes, rng, extra_lpm=leak_lpm if h >= leak_start_h else 0.0)
        tank.inflow_lpm = 0.0            # the refill never arrives

        if rain_start_h <= h <= rain_end_h:
            frac = (h - rain_start_h) / max(0.01, rain_end_h - rain_start_h)
            rain.intensity_mmhr = 50.0 * math.sin(math.pi * min(1.0, frac))
            street.blocked = 0.8
        else:
            rain.intensity_mmhr = 0.0

    def nodes_factory() -> dict[str, VirtualNode]:
        nodes = build_default_nodes()
        _set_tank_pct(nodes, 48.0)
        return nodes

    return Scenario(
        name="demo",
        description=(
            "One continuous 30 h day from 18:00: a leak at midnight, no morning refill, "
            "a hot afternoon, then a downpour onto a blocked drain."
        ),
        duration_s=hours * 3600,
        interval_s=30.0,
        start_hour_local=18.0,
        truth=[
            Truth("leak", leak_start_h * 3600, hours * 3600,
                  f"a {leak_lpm} L/min leak is running", min_severity=2),
            Truth("tank", 0, hours * 3600,
                  "tank genuinely at or below its configured low threshold (25%)",
                  min_severity=2, predicate=tank_below(25.0), precursor=tank_below(40.0)),
            Truth("demand", 0, hours * 3600,
                  "site's true heat index at or above the 41 C advisory level",
                  min_severity=2, predicate=heat_index_above(41.0), precursor=heat_applied(1.0)),
            Truth("flood", 0, hours * 3600, "water genuinely ponding (>=6 cm) at the low point",
                  min_severity=2, predicate=ponding(6.0), precursor=ponding(1.0)),
        ],
        apply=apply,
        nodes_factory=nodes_factory,
        claim=(
            "All four modules on one real timeline, each in the regime it was designed for. "
            "Playback is sped up for the video; the physics and the detection logic are not."
        ),
    )


REGISTRY: dict[str, Callable[[], Scenario]] = {
    "baseline": lambda: scenario_baseline(),
    "baseline_short": lambda: scenario_baseline(24.0),
    "flood": lambda: scenario_flood(),
    "leak": lambda: scenario_leak(),
    "depletion": lambda: scenario_depletion(),
    "heatwave": lambda: scenario_heatwave(),
    "outage": lambda: scenario_outage(),
    "sensor_fault": lambda: scenario_sensor_fault(),
    "longhaul": lambda: scenario_longhaul(),
    "demo": lambda: scenario_demo(),
    "demo_short": lambda: scenario_demo(12.0),
}


def get(name: str) -> Scenario:
    if name not in REGISTRY:
        raise KeyError(f"unknown scenario {name!r}; available: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]()
