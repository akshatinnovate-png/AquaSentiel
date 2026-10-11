"""Physical models for each simulated sensor, plus realistic imperfection.

Every class exposes `read(t)` returning the value a real sensor would report,
including its own error behaviour.  The *true* value is kept separately on each
model so the evaluation harness can score the engine against ground truth.
"""
from __future__ import annotations

import math
import random


class Sensor:
    """Base class adding the error modes that matter to the detectors."""

    def __init__(self, noise: float = 0.0, resolution: float = 0.0,
                 dropout_p: float = 0.0, spike_p: float = 0.0, spike_mag: float = 0.0,
                 bias: float = 0.0, rng: random.Random | None = None):
        self.noise = noise              # gaussian sigma, in sensor units
        self.resolution = resolution    # quantisation step (ADC / pulse counting)
        self.dropout_p = dropout_p      # chance of returning nothing this sample
        self.spike_p = spike_p          # chance of a wild outlier
        self.spike_mag = spike_mag
        self.bias = bias                # constant calibration error
        self.rng = rng or random.Random(0)
        self.faulted = False            # set by scenario fault injection
        self.fault_mode = "none"        # none | stuck | disconnected | drift
        self._stuck_value: float | None = None
        self._drift = 0.0

    def inject_fault(self, mode: str) -> None:
        self.fault_mode = mode
        self.faulted = mode != "none"
        self._stuck_value = None

    def _apply(self, true_value: float) -> float | None:
        if self.fault_mode == "disconnected":
            return None
        if self.fault_mode == "stuck":
            if self._stuck_value is None:
                self._stuck_value = true_value
            return self._stuck_value
        if self.fault_mode == "drift":
            self._drift += 0.02
        v = true_value + self.bias + self._drift
        if self.noise:
            v += self.rng.gauss(0.0, self.noise)
        if self.spike_p and self.rng.random() < self.spike_p:
            v += self.rng.choice([-1, 1]) * self.spike_mag
        if self.dropout_p and self.rng.random() < self.dropout_p:
            return None
        if self.resolution:
            v = round(v / self.resolution) * self.resolution
        return v


class TankModel(Sensor):
    """A tank that fills from a pump and drains through taps.

    Level is integrated from the net flow, which means the detectors see the
    same shape a real tank produces: a steady drain with step-changes when a
    tap opens, and a fast near-linear rise during a refill.
    """

    def __init__(self, capacity_l: float = 5000.0, height_cm: float = 180.0,
                 start_pct: float = 85.0, rng: random.Random | None = None):
        # An HC-SR04-class ultrasonic sensor: ~0.3 cm noise, 0.1 cm resolution,
        # and the occasional wild reading off a rippling surface.
        super().__init__(noise=0.3, resolution=0.1, spike_p=0.004, spike_mag=12.0, rng=rng)
        self.capacity_l = capacity_l
        self.height_cm = height_cm
        self.volume_l = capacity_l * start_pct / 100.0
        self.inflow_lpm = 0.0
        self.outflow_lpm = 0.0

    @property
    def true_level_cm(self) -> float:
        return self.height_cm * self.volume_l / self.capacity_l

    @property
    def true_pct(self) -> float:
        return 100.0 * self.volume_l / self.capacity_l

    def step(self, dt_s: float) -> None:
        net = (self.inflow_lpm - self.outflow_lpm) * dt_s / 60.0
        self.volume_l = max(0.0, min(self.capacity_l, self.volume_l + net))

    def read(self, t: float) -> float | None:
        """Report water depth in cm (what the firmware converts from range)."""
        return self._apply(self.true_level_cm)


class FlowModel(Sensor):
    """A turbine flow meter on the service line.

    Real meters count pulses, so they quantise and they under-read badly at
    very low flow -- which is precisely the regime leak detection cares about.
    We model both, because a detector that only works on clean flow data is
    not evidence of anything.
    """

    def __init__(self, k_pulses_per_l: float = 450.0, rng: random.Random | None = None):
        super().__init__(noise=0.03, rng=rng)
        self.k = k_pulses_per_l
        self.true_lpm = 0.0
        self.cutoff_lpm = 0.05   # below this the turbine does not spin reliably

    def read(self, t: float) -> float | None:
        if self.fault_mode == "disconnected":
            return None
        if self.fault_mode == "stuck":
            if self._stuck_value is None:
                self._stuck_value = self.true_lpm
            return self._stuck_value

        # Noise belongs on the flow, not on the count: it is turbulence and
        # pulse-timing jitter, both of which happen before the pulses exist.
        v = self.true_lpm + self.bias
        if self.noise:
            v += self.rng.gauss(0.0, self.noise)
        if v < self.cutoff_lpm:
            # A turbine below its cut-in speed is not turning, so it emits no
            # pulses and the reading is exactly zero. Letting sensor noise
            # through here would put a permanent floor under the minimum
            # night flow -- the one number the leak detector most depends on
            # being genuinely zero when nothing is flowing.
            return 0.0
        pulses = max(0, int(v * self.k / 60.0))
        return pulses * 60.0 / self.k


class StreetWaterModel(Sensor):
    """Water depth at a monitored low point, filled by runoff and drained.

    Inflow is a fraction of rainfall intensity over a contributing area;
    outflow follows Torricelli through the drain, so depth responds the way a
    real channel does: fast rise, slow tail, and a plateau when the drain
    matches the inflow.
    """

    # Discharge coefficient of the drain inlet, calibrated so that a clear
    # drain passes a 45 mm/h storm on this catchment with only ~3 cm of head.
    DRAIN_K_L_S = 55.0

    def __init__(self, area_m2: float = 60.0, drain_coeff: float = 0.9,
                 rng: random.Random | None = None):
        super().__init__(noise=0.35, resolution=0.1, spike_p=0.006, spike_mag=9.0, rng=rng)
        self.depth_cm = 0.0
        self.area_m2 = area_m2
        self.drain_coeff = drain_coeff
        self.blocked = 0.0           # 0 = clear, 1 = fully blocked
        self.runoff_ratio = 0.75     # paved surface
        self.contributing_m2 = 900.0

    def step(self, dt_s: float, rain_mmhr: float) -> None:
        # Inflow from runoff. One mm of rain on one square metre is one litre,
        # so mm/h * m2 is litres/h directly -- no volume conversion needed.
        inflow_l_s = rain_mmhr * self.contributing_m2 * self.runoff_ratio / 3600.0
        # Outflow through the drain, Torricelli-like: proportional to sqrt(h).
        head_m = max(0.0, self.depth_cm) / 100.0
        capacity = self.drain_coeff * (1.0 - self.blocked)
        outflow_l_s = capacity * self.DRAIN_K_L_S * math.sqrt(max(0.0, head_m))
        net_l = (inflow_l_s - outflow_l_s) * dt_s
        # litres -> cm over the ponding area
        self.depth_cm = max(0.0, self.depth_cm + net_l / (self.area_m2 * 10.0))

    def read(self, t: float) -> float | None:
        out = self._apply(self.depth_cm)
        return None if out is None else max(0.0, out)


class RainModel(Sensor):
    """A tipping-bucket gauge: reports in discrete 0.2 mm tips."""

    def __init__(self, tip_mm: float = 0.2, rng: random.Random | None = None):
        super().__init__(rng=rng)
        self.tip_mm = tip_mm
        self.intensity_mmhr = 0.0
        self._accum = 0.0
        self._tips_this_sample = 0

    def step(self, dt_s: float) -> None:
        self._accum += self.intensity_mmhr * dt_s / 3600.0
        self._tips_this_sample = 0
        while self._accum >= self.tip_mm:
            self._accum -= self.tip_mm
            self._tips_this_sample += 1

    def read_increment_mm(self, t: float) -> float | None:
        if self.fault_mode == "disconnected":
            return None
        return self._tips_this_sample * self.tip_mm

    def read(self, t: float) -> float | None:
        """Intensity in mm/h, as the firmware derives it from recent tips."""
        if self.fault_mode == "disconnected":
            return None
        return self.intensity_mmhr


class ClimateModel(Sensor):
    """Temperature and humidity with a diurnal cycle.

    Humidity is anti-correlated with temperature, as it is in reality, which
    matters because the heat index depends on both and a naive simulator that
    drives them independently produces impossible combinations.
    """

    def __init__(self, base_c: float = 28.0, amplitude_c: float = 7.0,
                 peak_hour: float = 15.0, tz_offset_h: float = 5.5,
                 rng: random.Random | None = None):
        # A DHT22-class sensor: +/-0.5 C, 0.1 C resolution, occasional dropout.
        super().__init__(noise=0.25, resolution=0.1, dropout_p=0.01, rng=rng)
        self.base_c = base_c
        self.amplitude_c = amplitude_c
        self.peak_hour = peak_hour
        self.tz_offset_h = tz_offset_h
        self.heat_offset_c = 0.0     # scenario-driven heat spell
        self.true_temp_c = base_c
        self.true_rh = 55.0
        self._rh_sensor = Sensor(noise=1.5, resolution=0.1, rng=rng)

    def local_hour(self, t: float) -> float:
        return ((t + self.tz_offset_h * 3600) / 3600.0) % 24.0

    def step(self, dt_s: float, t: float) -> None:
        hour = self.local_hour(t)
        # Diurnal curve peaking in mid-afternoon.
        self.true_temp_c = (
            self.base_c
            + self.amplitude_c * math.sin(2 * math.pi * (hour - self.peak_hour + 6.0) / 24.0)
            + self.heat_offset_c
        )
        # Hotter air holds the same absolute moisture at a lower relative
        # humidity. The slope matters: too shallow and the model produces
        # 35 C at 68% RH, a heat index of 50 C, which would be an extreme
        # event rather than the ordinary hot afternoon it is meant to be.
        # These values give ~85% RH on a 22 C night and ~40% on a 35 C
        # afternoon, which is typical inland post-monsoon weather.
        self.true_rh = max(18.0, min(95.0, 85.0 - 3.45 * (self.true_temp_c - 22.0)))

    def read(self, t: float) -> float | None:
        return self._apply(self.true_temp_c)

    def read_humidity(self, t: float) -> float | None:
        self._rh_sensor.fault_mode = self.fault_mode
        v = self._rh_sensor._apply(self.true_rh)
        return None if v is None else max(0.0, min(100.0, v))
