"""Core data types shared by every layer of AquaSentinel.

Design rule: a `Reading` is what we *measured*, a `Signal` is what we
*derived*, and an `Alert` is what we *decided*.  Keeping those three separate
is what lets the dashboard honestly label each number, and it is the single
most important modelling decision in this project.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Any

# Canonical metric names.  The firmware, the cloud and the UI all use these.
METRICS = (
    "rain_mm",            # rainfall accumulated in the sample interval, mm
    "rain_rate_mmhr",     # instantaneous rainfall intensity, mm/hour
    "street_water_cm",    # water depth at the monitored low point, cm
    "tank_level_cm",      # water depth inside the storage tank, cm
    "tank_level_pct",     # convenience: fill percentage, 0-100
    "flow_lpm",           # measured flow through the service line, litres/min
    "temperature_c",      # air temperature, degrees Celsius
    "humidity_pct",       # relative humidity, percent
)

# Physically possible ranges.  Anything outside these is a sensor fault, not a
# measurement, and must never reach the detection logic.
PLAUSIBLE_RANGE: dict[str, tuple[float, float]] = {
    "rain_mm": (0.0, 200.0),
    "rain_rate_mmhr": (0.0, 500.0),
    "street_water_cm": (-5.0, 400.0),
    "tank_level_cm": (-5.0, 500.0),
    "tank_level_pct": (-2.0, 105.0),
    "flow_lpm": (-1.0, 200.0),
    "temperature_c": (-20.0, 65.0),
    "humidity_pct": (0.0, 100.0),
}


class Severity(IntEnum):
    """Ordered so that `max()` over a list of severities is meaningful."""

    OK = 0
    INFO = 1
    WATCH = 2
    WARNING = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def parse(cls, v: Any) -> "Severity":
        if isinstance(v, cls):
            return v
        if isinstance(v, int):
            return cls(max(0, min(4, v)))
        try:
            return cls[str(v).upper()]
        except KeyError:
            return cls.OK


class Quality(IntEnum):
    """How much we trust a single measurement."""

    GOOD = 0
    SMOOTHED = 1      # value survived filtering but was noisy
    SUSPECT = 2       # out of expected band, kept but flagged
    IMPLAUSIBLE = 3   # outside physical range, rejected
    STALE = 4         # no fresh sample within the expected interval
    MISSING = 5       # sensor reported nothing


@dataclass
class Reading:
    """One timestamped record from one node. Mirrors the firmware payload."""

    device_id: str
    ts: float                                   # epoch seconds, UTC
    metrics: dict[str, float] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)
    seq: int = 0                                # firmware sequence number
    buffered: bool = False                      # replayed after an outage
    fw_version: str = ""
    rssi: int | None = None                     # Wi-Fi signal, for node health

    def get(self, name: str) -> float | None:
        """Metric value, or None when absent or rejected as implausible."""
        if name not in self.metrics:
            return None
        if self.quality.get(name, Quality.GOOD) >= Quality.IMPLAUSIBLE:
            return None
        v = self.metrics[name]
        return None if v is None else float(v)

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "ts": self.ts,
            "readings": {k: v for k, v in self.metrics.items()},
            "quality": dict(self.quality),
            "seq": self.seq,
            "buffered": self.buffered,
            "fw_version": self.fw_version,
            "rssi": self.rssi,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "Reading":
        """Parse a firmware payload, rejecting anything physically impossible.

        This is the trust boundary: everything downstream may assume that a
        metric still present in `metrics` was inside its plausible range, or is
        explicitly flagged.
        """
        metrics_in = payload.get("readings") or payload.get("metrics") or {}
        quality: dict[str, int] = {
            k: int(v) for k, v in (payload.get("quality") or {}).items()
        }
        metrics: dict[str, float] = {}
        for name, raw in metrics_in.items():
            if raw is None:
                quality[name] = int(Quality.MISSING)
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                quality[name] = int(Quality.IMPLAUSIBLE)
                continue
            if val != val or val in (float("inf"), float("-inf")):  # NaN / inf
                quality[name] = int(Quality.IMPLAUSIBLE)
                continue
            lo, hi = PLAUSIBLE_RANGE.get(name, (-1e9, 1e9))
            if not (lo <= val <= hi):
                quality[name] = int(Quality.IMPLAUSIBLE)
                metrics[name] = val          # keep for forensics, get() hides it
                continue
            metrics[name] = val
            quality.setdefault(name, int(Quality.GOOD))

        ts = payload.get("timestamp", payload.get("ts"))
        return cls(
            device_id=str(payload.get("device_id") or payload.get("deviceId") or "unknown"),
            ts=_parse_ts(ts),
            metrics=metrics,
            quality=quality,
            seq=int(payload.get("seq") or 0),
            buffered=bool(payload.get("buffered")),
            fw_version=str(payload.get("fw_version") or ""),
            rssi=payload.get("rssi"),
        )


def _parse_ts(ts: Any) -> float:
    """Accept epoch seconds, epoch millis or ISO-8601; default to now."""
    if ts is None:
        return time.time()
    if isinstance(ts, (int, float)):
        return float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        try:
            return float(ts)
        except (TypeError, ValueError):
            return time.time()


@dataclass
class Signal:
    """A derived quantity with provenance.

    `kind` says whether a caretaker is looking at something we measured, a
    short-horizon projection, or a model forecast.  The dashboard renders each
    kind differently, which is how we avoid dressing an estimate up as a fact.
    """

    name: str
    value: float | None
    unit: str = ""
    kind: str = "derived"            # measured | derived | projected | forecast
    confidence: float = 1.0          # 0..1
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        if isinstance(d["value"], float):
            d["value"] = round(d["value"], 4)
        d["confidence"] = round(self.confidence, 3)
        return d


@dataclass
class Alert:
    """A decision, with the evidence that produced it."""

    alert_id: str
    device_id: str
    module: str                       # flood | leak | tank | demand | device
    severity: Severity
    title: str
    reason: str                       # plain-language, caretaker-readable
    evidence: list[dict] = field(default_factory=list)
    recommendation: str = ""
    ts: float = 0.0
    state: str = "active"             # active | cleared | acknowledged
    confidence: float = 1.0
    cleared_ts: float | None = None
    acknowledged_by: str | None = None
    explanation: str | None = None    # optional Bedrock-generated narrative

    def as_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "device_id": self.device_id,
            "module": self.module,
            "severity": int(self.severity),
            "severity_label": self.severity.label,
            "title": self.title,
            "reason": self.reason,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "ts": self.ts,
            "state": self.state,
            "confidence": round(self.confidence, 3),
            "cleared_ts": self.cleared_ts,
            "acknowledged_by": self.acknowledged_by,
            "explanation": self.explanation,
        }


@dataclass
class ModuleResult:
    """What one detection module returns for one reading."""

    module: str
    severity: Severity = Severity.OK
    state: str = "NORMAL"
    signals: list[Signal] = field(default_factory=list)
    reason: str = ""
    evidence: list[dict] = field(default_factory=list)
    recommendation: str = ""
    confidence: float = 1.0
    title: str = ""

    def signal(self, name: str) -> Signal | None:
        for s in self.signals:
            if s.name == name:
                return s
        return None

    def value(self, name: str, default: float | None = None) -> float | None:
        s = self.signal(name)
        return default if s is None or s.value is None else s.value

    def as_dict(self) -> dict:
        return {
            "module": self.module,
            "severity": int(self.severity),
            "severity_label": self.severity.label,
            "state": self.state,
            "title": self.title,
            "reason": self.reason,
            "recommendation": self.recommendation,
            "confidence": round(self.confidence, 3),
            "signals": [s.as_dict() for s in self.signals],
            "evidence": self.evidence,
        }
