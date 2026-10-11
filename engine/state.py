"""Per-device rolling state.

The detection modules are stateless functions; all memory lives here.  The
whole object round-trips through `as_dict()` / `from_dict()` so the same state
can sit in a DynamoDB item between Lambda invocations or in a dict in the local
server.  Buffers are bounded by both age and count so a device item can never
grow past DynamoDB's 400 KB limit.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Iterable, Sequence

from engine.stats import median_filter, ols_slope, quantile, Slope

# Keep ~2 hours at a 15 s cadence, which covers every rolling window we use.
MAX_POINTS = 480
MAX_AGE_S = 7200.0

# Hour-of-day baselines are kept as compact reservoirs, not raw history.
BUCKET_CAP = 64


class Series:
    """A bounded time series of (timestamp, value) pairs."""

    __slots__ = ("points", "max_points", "max_age_s")

    def __init__(self, max_points: int = MAX_POINTS, max_age_s: float = MAX_AGE_S):
        self.points: Deque[tuple[float, float]] = deque(maxlen=max_points)
        self.max_points = max_points
        self.max_age_s = max_age_s

    def push(self, ts: float, value: float) -> None:
        # Out-of-order arrivals happen when a node replays its offline buffer.
        if self.points and ts < self.points[-1][0]:
            self.points.append((ts, value))
            self.points = deque(sorted(self.points), maxlen=self.max_points)
        else:
            self.points.append((ts, value))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.max_age_s
        while self.points and self.points[0][0] < cutoff:
            self.points.popleft()

    # -- views -------------------------------------------------------------
    def window(self, now: float, seconds: float) -> list[tuple[float, float]]:
        cutoff = now - seconds
        return [(t, v) for t, v in self.points if t >= cutoff]

    def values(self, now: float | None = None, seconds: float | None = None) -> list[float]:
        if now is None or seconds is None:
            return [v for _, v in self.points]
        return [v for _, v in self.window(now, seconds)]

    @property
    def last(self) -> float | None:
        return self.points[-1][1] if self.points else None

    @property
    def last_ts(self) -> float | None:
        return self.points[-1][0] if self.points else None

    def __len__(self) -> int:
        return len(self.points)

    # -- derived -----------------------------------------------------------
    def smoothed(self, window: int = 5) -> list[float]:
        """Causal median filter over the whole buffer."""
        return median_filter([v for _, v in self.points], window)

    def smoothed_last(self, window: int = 5) -> float | None:
        vals = [v for _, v in self.points]
        if not vals:
            return None
        return median_filter(vals[-window:], window)[-1]

    def slope_per_s(self, now: float, seconds: float, smooth: int = 0) -> Slope:
        """Trend of this series over the trailing window, in units per second."""
        win = self.window(now, seconds)
        if len(win) < 3:
            return Slope(0.0, 0.0, 0.0, len(win), win[-1][1] if win else 0.0)
        ts = [t for t, _ in win]
        vs = [v for _, v in win]
        if smooth > 1:
            vs = median_filter(vs, smooth)
        t0 = ts[0]
        return ols_slope([t - t0 for t in ts], vs)

    def integral(self, now: float, seconds: float) -> float:
        """Trapezoidal integral over the window, in value-seconds.

        Used to turn litres/minute into litres actually lost.
        """
        win = self.window(now, seconds)
        if len(win) < 2:
            return 0.0
        total = 0.0
        for (t0, v0), (t1, v1) in zip(win, win[1:]):
            total += 0.5 * (v0 + v1) * (t1 - t0)
        return total

    def as_dict(self) -> dict:
        return {
            "p": [[round(t, 3), round(v, 4)] for t, v in self.points],
            "mp": self.max_points,
            "ma": self.max_age_s,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Series":
        s = cls(int((d or {}).get("mp", MAX_POINTS)), float((d or {}).get("ma", MAX_AGE_S)))
        for item in (d or {}).get("p", []):
            try:
                s.points.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return s


class Buckets:
    """Hour-of-day reservoirs used to learn "normal" without storing history.

    We keep up to `BUCKET_CAP` recent samples per (hour, day-type) bucket.
    That is enough for a robust p50/p95 and it bounds the stored state.
    """

    __slots__ = ("data", "cap")

    def __init__(self, cap: int = BUCKET_CAP):
        self.data: dict[str, list[float]] = {}
        self.cap = cap

    @staticmethod
    def key(hour: int, dow: int | None = None) -> str:
        if dow is None:
            return f"h{hour:02d}"
        return f"h{hour:02d}-{'we' if dow >= 5 else 'wd'}"

    def add(self, key: str, value: float) -> None:
        arr = self.data.setdefault(key, [])
        arr.append(float(value))
        if len(arr) > self.cap:
            del arr[0 : len(arr) - self.cap]

    def get(self, key: str) -> list[float]:
        return self.data.get(key, [])

    def count(self, key: str) -> int:
        return len(self.data.get(key, []))

    def p(self, key: str, q: float) -> float | None:
        arr = self.data.get(key)
        return quantile(arr, q) if arr else None

    def total_samples(self) -> int:
        return sum(len(v) for v in self.data.values())

    def as_dict(self) -> dict:
        return {"d": {k: [round(x, 4) for x in v] for k, v in self.data.items()}, "c": self.cap}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Buckets":
        b = cls(int((d or {}).get("c", BUCKET_CAP)))
        for k, v in (d or {}).get("d", {}).items():
            b.data[k] = [float(x) for x in v]
        return b


class Latch:
    """Dwell + hysteresis + cooldown for one module's alert state.

    Without this, a single noisy sample fires an alert and the dashboard cries
    wolf.  `dwell` requires a condition to persist, and an alert only clears
    once the condition has been absent for `clear_dwell` samples.
    """

    __slots__ = ("severity", "candidate", "count", "clear_count", "since_ts",
                 "last_fire_ts", "alert_id", "held_reason", "held_evidence", "held_action")

    def __init__(self) -> None:
        self.severity: int = 0
        self.candidate: int = 0
        self.count: int = 0
        self.clear_count: int = 0
        self.since_ts: float = 0.0
        self.last_fire_ts: float = 0.0
        self.alert_id: str | None = None
        # The reason that justified the current latched severity. Without it,
        # a held severity gets rendered beside whatever the latest sample
        # happened to say, which is how a WARNING ends up captioned
        # "node online, nothing wrong".
        self.held_reason: str = ""
        self.held_evidence: list = []
        self.held_action: str = "" 

    def update(self, proposed: int, ts: float, dwell: int = 2, clear_dwell: int = 3) -> tuple[int, bool, bool]:
        """Feed a proposed severity; return (effective, escalated, cleared)."""
        escalated = False
        cleared = False
        if proposed > self.severity:
            if proposed == self.candidate:
                self.count += 1
            else:
                self.candidate, self.count = proposed, 1
            if self.count >= dwell:
                self.severity = proposed
                self.since_ts = ts
                self.last_fire_ts = ts
                self.candidate, self.count, self.clear_count = 0, 0, 0
                escalated = True
        elif proposed < self.severity:
            self.clear_count += 1
            self.candidate, self.count = 0, 0
            if self.clear_count >= clear_dwell:
                was = self.severity
                self.severity = proposed
                self.clear_count = 0
                if was > 0 and proposed == 0:
                    cleared = True
                elif proposed > 0:
                    # de-escalation still counts as a state change worth showing
                    self.since_ts = ts
        else:
            self.candidate, self.count, self.clear_count = 0, 0, 0
        return self.severity, escalated, cleared

    def as_dict(self) -> dict:
        return {
            "sev": self.severity,
            "cand": self.candidate,
            "n": self.count,
            "cn": self.clear_count,
            "since": self.since_ts,
            "fire": self.last_fire_ts,
            "aid": self.alert_id,
            "hr": self.held_reason,
            "he": self.held_evidence,
            "ha": self.held_action,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Latch":
        l = cls()
        d = d or {}
        l.severity = int(d.get("sev", 0))
        l.candidate = int(d.get("cand", 0))
        l.count = int(d.get("n", 0))
        l.clear_count = int(d.get("cn", 0))
        l.since_ts = float(d.get("since", 0.0))
        l.last_fire_ts = float(d.get("fire", 0.0))
        l.alert_id = d.get("aid")
        l.held_reason = str(d.get("hr") or "")
        l.held_evidence = list(d.get("he") or [])
        l.held_action = str(d.get("ha") or "")
        return l


class DeviceState:
    """All rolling memory for one node."""

    SERIES_NAMES = (
        "street_water_cm",
        "tank_level_cm",
        "tank_volume_l",
        "flow_lpm",
        "rain_rate_mmhr",
        "temperature_c",
        "humidity_pct",
        "heat_index_c",
    )

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.series: dict[str, Series] = {n: Series() for n in self.SERIES_NAMES}
        # Learned "normal" per hour-of-day.
        self.flow_baseline = Buckets()
        self.demand_baseline = Buckets()
        self.temp_baseline = Buckets()
        self.latches: dict[str, Latch] = {m: Latch() for m in ("flood", "leak", "tank", "demand", "device")}
        self.last_ts: float = 0.0
        self.first_ts: float = 0.0
        self.last_seq: int = 0
        self.gaps: int = 0               # count of detected transmission gaps
        self.samples: int = 0
        self.rain_total_mm: float = 0.0
        self.last_refill_ts: float = 0.0
        self.leak_volume_l: float = 0.0  # running estimate of excess volume
        self.leak_started_ts: float = 0.0
        self.flow_run_started_ts: float = 0.0   # start of the current non-stop flow
        self.demand_model: dict = {}     # fitted weights + held-out scores
        self.hour_usage_l: dict[str, float] = {}  # accumulating litres per local hour
        # Heat anomaly is measured peak-to-peak across days, so we need to
        # know which local day we are in and how hot it has got so far.
        self.hi_day_index: int = -1
        self.hi_day_max: float = -999.0
        # Hourly demand accumulator. Training rows are one per completed local
        # hour, not one per sample, so the forecaster sees days of history
        # rather than minutes.
        self.usage_hour_key: int = -1
        self.usage_hour_l: float = 0.0
        self.usage_hour_hi_sum: float = 0.0
        self.usage_hour_n: int = 0
        # Which modules this node has ever produced a result for. Used to tell
        # "this node has no climate sensor" apart from "the climate sensor
        # missed this one sample" -- which look identical in a single reading.
        self.seen_modules: list[str] = []
        # Recent (timestamp, metric) rejections. A sensor that drops one
        # reading in a hundred is behaving normally; one that keeps dropping
        # them is faulty. Telling those apart needs a short memory.
        self.reject_history: list[list] = []

    # -- ingestion ---------------------------------------------------------
    def s(self, name: str) -> Series:
        if name not in self.series:
            self.series[name] = Series()
        return self.series[name]

    def note_arrival(self, ts: float, seq: int, expected_interval_s: float) -> bool:
        """Record arrival; return True if a transmission gap was detected."""
        gap = False
        if self.last_ts and ts - self.last_ts > max(3 * expected_interval_s, 60):
            self.gaps += 1
            gap = True
        if not self.first_ts:
            self.first_ts = ts
        self.last_ts = max(self.last_ts, ts)
        self.last_seq = max(self.last_seq, seq)
        self.samples += 1
        return gap

    # -- serialisation -----------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "series": {k: v.as_dict() for k, v in self.series.items() if len(v)},
            "flow_baseline": self.flow_baseline.as_dict(),
            "demand_baseline": self.demand_baseline.as_dict(),
            "temp_baseline": self.temp_baseline.as_dict(),
            "latches": {k: v.as_dict() for k, v in self.latches.items()},
            "last_ts": self.last_ts,
            "first_ts": self.first_ts,
            "last_seq": self.last_seq,
            "gaps": self.gaps,
            "samples": self.samples,
            "rain_total_mm": round(self.rain_total_mm, 3),
            "last_refill_ts": self.last_refill_ts,
            "leak_volume_l": round(self.leak_volume_l, 3),
            "leak_started_ts": self.leak_started_ts,
            "flow_run_started_ts": self.flow_run_started_ts,
            "demand_model": self.demand_model,
            "hour_usage_l": {k: round(v, 3) for k, v in self.hour_usage_l.items()},
            "hi_day_index": self.hi_day_index,
            "hi_day_max": round(self.hi_day_max, 3),
            "usage_hour_key": self.usage_hour_key,
            "usage_hour_l": round(self.usage_hour_l, 4),
            "usage_hour_hi_sum": round(self.usage_hour_hi_sum, 4),
            "usage_hour_n": self.usage_hour_n,
            "seen_modules": list(self.seen_modules),
            "reject_history": [[round(ts, 1), m] for ts, m in self.reject_history[-60:]],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceState":
        st = cls(str(d.get("device_id", "unknown")))
        for k, v in (d.get("series") or {}).items():
            st.series[k] = Series.from_dict(v)
        for n in cls.SERIES_NAMES:
            st.series.setdefault(n, Series())
        st.flow_baseline = Buckets.from_dict(d.get("flow_baseline"))
        st.demand_baseline = Buckets.from_dict(d.get("demand_baseline"))
        st.temp_baseline = Buckets.from_dict(d.get("temp_baseline"))
        for k, v in (d.get("latches") or {}).items():
            st.latches[k] = Latch.from_dict(v)
        for m in ("flood", "leak", "tank", "demand", "device"):
            st.latches.setdefault(m, Latch())
        st.last_ts = float(d.get("last_ts", 0.0))
        st.first_ts = float(d.get("first_ts", 0.0))
        st.last_seq = int(d.get("last_seq", 0))
        st.gaps = int(d.get("gaps", 0))
        st.samples = int(d.get("samples", 0))
        st.rain_total_mm = float(d.get("rain_total_mm", 0.0))
        st.last_refill_ts = float(d.get("last_refill_ts", 0.0))
        st.leak_volume_l = float(d.get("leak_volume_l", 0.0))
        st.leak_started_ts = float(d.get("leak_started_ts", 0.0))
        st.flow_run_started_ts = float(d.get("flow_run_started_ts", 0.0))
        st.demand_model = d.get("demand_model") or {}
        st.hour_usage_l = {k: float(v) for k, v in (d.get("hour_usage_l") or {}).items()}
        st.hi_day_index = int(d.get("hi_day_index", -1))
        st.hi_day_max = float(d.get("hi_day_max", -999.0))
        st.usage_hour_key = int(d.get("usage_hour_key", -1))
        st.usage_hour_l = float(d.get("usage_hour_l", 0.0))
        st.usage_hour_hi_sum = float(d.get("usage_hour_hi_sum", 0.0))
        st.usage_hour_n = int(d.get("usage_hour_n", 0))
        st.seen_modules = list(d.get("seen_modules") or [])
        st.reject_history = [[float(x[0]), str(x[1])] for x in (d.get("reject_history") or [])]
        return st
