"""The engine entry point.

    engine = Engine(site_config)
    result = engine.ingest(payload, state)   # state is mutated in place

One call takes a raw firmware payload and returns alerts, module results, a
fused situation report and the updated state.  The same call is made by the
`evaluator` Lambda, the local dev server and the evaluation harness, so there
is exactly one code path to test.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from engine import demand, flood, leak, tank
from engine.config import ALERT_FROM_SEVERITY, DEFAULT_SITE, SiteConfig
from engine.fusion import situation
from engine.model import Alert, ModuleResult, Quality, Reading, Severity, Signal
from engine.state import DeviceState, Latch

MODULES = (("flood", flood), ("leak", leak), ("tank", tank), ("demand", demand))

# What each module needs from a reading, used only to explain a gap in plain
# language when a sensor misses a sample.
MODULE_INPUTS = {
    "flood": ("street water level",),
    "leak": ("flow",),
    "tank": ("tank level",),
    "demand": ("temperature", "humidity"),
}
MODULE_TITLES = {
    "flood": "Flood / waterlogging",
    "leak": "Possible water loss",
    "tank": "Stored supply",
    "demand": "Heat and water demand",
}


@dataclass
class IngestResult:
    device_id: str
    ts: float
    accepted: bool
    reading: Reading | None = None
    modules: dict[str, ModuleResult] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)   # newly raised or cleared
    situation: dict = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    gap_detected: bool = False
    module_errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "ts": self.ts,
            "accepted": self.accepted,
            "reading": self.reading.as_dict() if self.reading else None,
            "modules": {k: v.as_dict() for k, v in self.modules.items()},
            "alerts": [a.as_dict() for a in self.alerts],
            "situation": self.situation,
            "rejected": self.rejected,
            "gap_detected": self.gap_detected,
            "module_errors": [
                {"module": e["module"], "error": e["error"]} for e in self.module_errors
            ],
        }


def alert_id(device_id: str, module: str, since_ts: float) -> str:
    """Stable id for one alert episode, so retries do not duplicate it.

    Keying on the episode start rather than the current time is what makes
    ingestion idempotent -- a replayed message lands on the same alert.
    """
    raw = f"{device_id}|{module}|{int(since_ts)}"
    return f"{module}-{hashlib.sha1(raw.encode()).hexdigest()[:12]}"


class Engine:
    def __init__(self, cfg: SiteConfig | None = None):
        self.cfg = cfg or DEFAULT_SITE
        # Every module exception seen by this engine instance. Callers assert
        # this is empty; the local server and Lambdas log it.
        self.module_errors: list[dict] = []

    # ------------------------------------------------------------------
    def ingest(self, payload: dict, state: DeviceState | None = None, now: float | None = None) -> IngestResult:
        reading = Reading.from_payload(payload)
        st = state or DeviceState(reading.device_id)
        now = now if now is not None else reading.ts

        rejected = [
            name for name, q in reading.quality.items()
            if int(q) >= int(Quality.IMPLAUSIBLE)
        ]
        gap = st.note_arrival(reading.ts, reading.seq, self.cfg.sample_interval_s)

        # Push every accepted metric into its rolling series *before* the
        # modules run, so each module sees the current sample in its window.
        for name in ("street_water_cm", "tank_level_cm", "flow_lpm",
                     "rain_rate_mmhr", "temperature_c", "humidity_pct"):
            v = reading.get(name)
            if v is not None:
                st.s(name).push(reading.ts, v)
        rain = reading.get("rain_mm")
        if rain is not None:
            st.rain_total_mm += rain

        results: dict[str, ModuleResult] = {}
        errors_before = len(self.module_errors)
        for name, module in MODULES:
            try:
                res = module.evaluate(reading, st, self.cfg)
            except Exception as exc:
                # One bad module must not stop the others, but it must never be
                # quiet either: a swallowed exception here once let the tank
                # module fail on every sample of a 14 h run while the dashboard
                # showed a cheerful green. A failed module is a device-health
                # problem with a traceback attached, and `module_errors` lets
                # the caller (and the test suite) assert that there were none.
                import traceback

                detail = traceback.format_exc(limit=4)
                self.module_errors.append({"module": name, "error": f"{type(exc).__name__}: {exc}",
                                           "traceback": detail})
                res = ModuleResult(
                    module=name,
                    severity=Severity.WARNING,
                    state="ERROR",
                    title=f"{name} detection unavailable",
                    reason=(
                        f"The {name} detection module failed on this reading "
                        f"({type(exc).__name__}: {exc}). Treat {name} as unmonitored until this is fixed."
                    ),
                    recommendation=f"Do not rely on the {name} panel. Check the service logs.",
                    confidence=0.0,
                )
            if res is not None:
                results[name] = res
                if name not in st.seen_modules:
                    st.seen_modules.append(name)
            elif name in st.seen_modules:
                # This node does have the sensor; it just did not report it in
                # this sample. Saying so is the honest answer, and it keeps the
                # panel on screen instead of implying the hardware is absent.
                missing = MODULE_INPUTS.get(name, ())
                results[name] = ModuleResult(
                    module=name,
                    severity=Severity.INFO,
                    state="NO_DATA",
                    title=MODULE_TITLES.get(name, name),
                    reason=(
                        f"No reading for this module in the latest sample "
                        f"({', '.join(missing)} not reported). The last known state is still "
                        f"shown on the charts; this instant is unmeasured."
                    ),
                    recommendation="",
                    confidence=0.0,
                )

        health = self._device_health(reading, st, rejected, gap, now)
        if health is not None:
            results["device"] = health

        alerts = self._apply_latches(results, st, reading.ts)
        sit = situation(results, self.cfg, reading.device_id)

        return IngestResult(
            device_id=reading.device_id,
            ts=reading.ts,
            accepted=True,
            reading=reading,
            modules=results,
            alerts=alerts,
            situation=sit,
            rejected=rejected,
            gap_detected=gap,
            module_errors=self.module_errors[errors_before:],
        )

    # ------------------------------------------------------------------
    def _device_health(self, reading: Reading, st: DeviceState, rejected: list[str],
                       gap: bool, now: float) -> ModuleResult | None:
        """Sensor and link health. A stale reading must never look live."""
        age_s = max(0.0, now - reading.ts)
        stale = age_s > self.cfg.stale_after_s
        problems: list[str] = []
        severity = Severity.OK

        # A single dropped or out-of-range reading is ordinary sensor
        # behaviour -- a DHT22 drops roughly one sample in a hundred. Raising
        # a WARNING for each one filled the alert history with dozens of
        # "node health, cleared" entries and buried the alerts that mattered.
        # A fault is a *rate*, so we look at the recent history.
        for name in rejected:
            st.reject_history.append([reading.ts, name])
        if len(st.reject_history) > 120:
            del st.reject_history[: len(st.reject_history) - 120]
        window_start = reading.ts - self.REJECT_WINDOW_S
        recent = [m for ts_, m in st.reject_history if ts_ >= window_start]
        persistent = sorted({m for m in recent if recent.count(m) >= self.REJECT_FAULT_COUNT})

        if persistent:
            problems.append(
                f"sensor reading(s) repeatedly outside their physical range: "
                f"{', '.join(persistent)} "
                f"({self.REJECT_FAULT_COUNT}+ rejections in the last "
                f"{self.REJECT_WINDOW_S / 60:.0f} min)"
            )
            severity = Severity.WARNING
        elif rejected:
            problems.append(
                f"{len(rejected)} reading(s) dropped this sample ({', '.join(sorted(rejected))}); "
                f"isolated, so not treated as a fault"
            )
            severity = Severity(max(int(severity), int(Severity.INFO)))
        if stale:
            problems.append(f"last reading is {age_s/60:.0f} min old (expected every {self.cfg.sample_interval_s}s)")
            severity = Severity(max(int(severity), int(Severity.WARNING)))
        if gap:
            problems.append("a transmission gap was detected and back-filled from the node's buffer"
                            if reading.buffered else "a transmission gap was detected")
            severity = Severity(max(int(severity), int(Severity.WATCH)))
        flatlined = self._flatlined(st, reading.ts)
        if flatlined:
            problems.append(
                "sensor reading(s) frozen at a constant value with no jitter: " + ", ".join(flatlined)
            )
            severity = Severity(max(int(severity), int(Severity.WARNING)))
        if reading.rssi is not None and reading.rssi < -85:
            problems.append(f"weak Wi-Fi signal ({reading.rssi} dBm)")
            severity = Severity(max(int(severity), int(Severity.INFO)))

        if not problems:
            return ModuleResult(
                module="device", severity=Severity.OK, state="ONLINE",
                title="Node health",
                reason=f"Node online, {st.samples} samples received, {st.gaps} gap(s) so far.",
                signals=[
                    Signal("reading_age_s", round(age_s, 1), "s", "measured"),
                    Signal("samples_received", float(st.samples), "count", "measured"),
                    Signal("transmission_gaps", float(st.gaps), "count", "measured"),
                    Signal("rssi_dbm", float(reading.rssi) if reading.rssi is not None else None, "dBm", "measured"),
                ],
                confidence=1.0,
            )
        return ModuleResult(
            module="device",
            severity=severity,
            state="STALE" if stale else "DEGRADED",
            title="Node health",
            reason="; ".join(problems).capitalize() + ".",
            recommendation=(
                "Treat the affected readings as unavailable, not as zero. Check power, wiring and Wi-Fi at the node."
            ),
            evidence=[{"signal": "reading_age_s", "value": round(age_s, 1),
                       "threshold": self.cfg.stale_after_s, "note": "freshness of the latest sample"}]
            + ([{"signal": "rejected_in_window", "value": len(recent),
                 "threshold": self.REJECT_FAULT_COUNT,
                 "note": f"out-of-range readings in the last {self.REJECT_WINDOW_S / 60:.0f} min"}]
               if persistent else [])
            + ([{"signal": "sensor_variance", "value": 0.0, "threshold": None,
                 "note": f"no variation at all in {', '.join(flatlined)} over the check window"}]
               if flatlined else []),
            signals=[
                Signal("reading_age_s", round(age_s, 1), "s", "measured"),
                Signal("rejected_metrics", float(len(rejected)), "count", "measured", 1.0, {"names": rejected}),
                Signal("flatlined_metrics", float(len(flatlined)), "count", "derived", 1.0,
                       {"names": flatlined,
                        "method": "zero sample variance over the check window; a working sensor "
                                  "always jitters at its own resolution"}),
                Signal("transmission_gaps", float(st.gaps), "count", "measured"),
                Signal("rssi_dbm", float(reading.rssi) if reading.rssi is not None else None, "dBm", "measured"),
            ],
            confidence=1.0,
        )

    # ------------------------------------------------------------------
    # How long a metric must be perfectly constant before we call it frozen,
    # and how many samples that needs. Long enough that a genuinely steady
    # quantity -- a dry channel, a tank between draws -- is not accused.
    FLATLINE_WINDOW_S = 600.0
    FLATLINE_MIN_SAMPLES = 20
    # How many rejections of one metric, within this window, make it a fault
    # rather than ordinary sensor noise.
    REJECT_WINDOW_S = 600.0
    REJECT_FAULT_COUNT = 4

    def _flatlined(self, st: DeviceState, now: float) -> list[str]:
        """Metrics that have not moved at all, which a real sensor never does.

        A sensor frozen at a plausible value is the dangerous failure: it keeps
        reporting, so nothing looks wrong, and the detectors quietly trust a
        number that stopped being a measurement some time ago.

        Exempted: a metric sitting at exactly zero. A dry channel really does
        read 0.0 cm sample after sample, and a closed tap really does read
        0 L/min, because the firmware clamps below the sensor's cut-in point.
        """
        out: list[str] = []
        for name in ("street_water_cm", "tank_level_cm", "flow_lpm", "temperature_c", "humidity_pct"):
            series = st.series.get(name)
            if series is None or len(series) < self.FLATLINE_MIN_SAMPLES:
                continue
            win = series.window(now, self.FLATLINE_WINDOW_S)
            if len(win) < self.FLATLINE_MIN_SAMPLES:
                continue
            if win[-1][0] - win[0][0] < self.FLATLINE_WINDOW_S * 0.5:
                continue
            vals = [v for _, v in win]
            if abs(vals[0]) < 1e-9:
                continue
            if max(vals) - min(vals) < 1e-9:
                out.append(name)
        return out

    # ------------------------------------------------------------------
    def _apply_latches(self, results: dict[str, ModuleResult], st: DeviceState, ts: float) -> list[Alert]:
        """Turn per-sample severities into alert episodes.

        Returns only *changes* -- newly raised, escalated or cleared alerts --
        because that is what deserves a notification.  The steady state is
        visible on the dashboard and does not need to page anyone.
        """
        out: list[Alert] = []
        for module, res in results.items():
            latch = st.latches.setdefault(module, Latch())
            dwell = 2
            cfg_block = getattr(self.cfg, module, None)
            if cfg_block is not None and hasattr(cfg_block, "dwell_samples"):
                dwell = int(getattr(cfg_block, "dwell_samples"))
            if module == "device":
                dwell = 1  # health problems are reported immediately
            if res.state in ("ERROR", "NO_DATA"):
                # Dwell exists to stop a noisy *measurement* from firing an
                # alert. A module that threw an exception, or a sensor that
                # reported nothing, is not a noisy measurement -- it is a fact
                # about the system, and holding it behind four samples of
                # smoothing is how a crash stays invisible.
                dwell = 1

            raw = int(res.severity)
            effective, escalated, cleared = latch.update(raw, ts, dwell=dwell, clear_dwell=3)

            if escalated:
                # Remember what justified this severity, so it can still be
                # explained on a later sample when the condition is absent.
                latch.held_reason = res.reason
                latch.held_evidence = list(res.evidence)
                latch.held_action = res.recommendation
            elif effective == 0:
                latch.held_reason, latch.held_evidence, latch.held_action = "", [], ""

            # The dashboard shows the latched severity, not the raw one, so a
            # single noisy sample never changes what a caretaker sees. But the
            # text must then describe the latched condition rather than the
            # current sample, and must say that the condition is no longer
            # being observed -- otherwise a held WARNING appears above the
            # words "nothing wrong", and the dashboard contradicts itself.
            if effective > raw and latch.held_reason:
                res.reason = (
                    f"{latch.held_reason} This is not present in the latest reading; the alert "
                    f"stays up until the condition has been absent for several samples."
                )
                res.evidence = latch.held_evidence
                res.recommendation = latch.held_action or res.recommendation
                res.state = f"{res.state}_CLEARING" if not res.state.endswith("_CLEARING") else res.state
            res.severity = Severity(effective)

            threshold = ALERT_FROM_SEVERITY.get(module, int(Severity.WATCH))
            if escalated and effective >= threshold:
                previous = latch.alert_id
                latch.alert_id = alert_id(st.device_id, module, latch.since_ts)
                if previous and previous != latch.alert_id:
                    # The same condition, now worse. Leaving the old episode
                    # open puts two alerts for one tank on the dashboard,
                    # quoting different numbers, and a caretaker has to work
                    # out which is current.
                    out.append(Alert(
                        alert_id=previous,
                        device_id=st.device_id,
                        module=module,
                        severity=Severity.OK,
                        title=res.title or module,
                        reason=f"Superseded by a more severe alert for the same condition.",
                        ts=ts,
                        state="superseded",
                        confidence=res.confidence,
                        cleared_ts=ts,
                    ))
                out.append(Alert(
                    alert_id=latch.alert_id,
                    device_id=st.device_id,
                    module=module,
                    severity=Severity(effective),
                    title=res.title or module,
                    reason=res.reason,
                    evidence=res.evidence,
                    recommendation=res.recommendation,
                    ts=ts,
                    state="active",
                    confidence=res.confidence,
                ))
            elif cleared and latch.alert_id:
                out.append(Alert(
                    alert_id=latch.alert_id,
                    device_id=st.device_id,
                    module=module,
                    severity=Severity.OK,
                    title=res.title or module,
                    reason=f"Cleared: {res.reason}",
                    evidence=[],
                    recommendation="",
                    ts=ts,
                    state="cleared",
                    confidence=res.confidence,
                    cleared_ts=ts,
                ))
                latch.alert_id = None
        return out
