"""Application services: the layer between HTTP/MQTT and the engine.

Everything here is transport-agnostic and store-agnostic, so the same code
serves an API Gateway request, an IoT Core message and a local HTTP request.
The Lambda handlers and `local/server.py` are thin shims over this file.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from engine.config import DEFAULT_SITE, SiteConfig
from engine.model import Severity
from engine.pipeline import Engine
from engine.state import DeviceState
from services.common.repo import MemoryRepo, build_repo

# How much engine state to keep inline on the device item. The rolling series
# are the bulky part; DynamoDB items cap at 400 KB, and the bounded buffers in
# `engine.state` are sized so a serialised state stays far below that.
STATE_KEY = "engine_state"


class Notifier:
    """Fan out alerts. Publishes to SNS in AWS, collects them locally.

    Only alert *changes* arrive here -- the engine's latches already decided
    that something is newly worth saying -- so this never has to rate-limit.
    """

    def __init__(self, topic_arn: str | None = None, bus_name: str | None = None):
        self.topic_arn = topic_arn or os.environ.get("ALERT_TOPIC_ARN")
        self.bus_name = bus_name or os.environ.get("EVENT_BUS_NAME")
        self.sent: list[dict] = []     # populated when there is no SNS
        self._sns = None
        self._events = None

    def _clients(self):
        if self.topic_arn and self._sns is None:
            import boto3

            self._sns = boto3.client("sns")
        if self.bus_name and self._events is None:
            import boto3

            self._events = boto3.client("events")
        return self._sns, self._events

    def publish(self, alert: dict, site_name: str = "") -> None:
        self.sent.append(alert)
        sns, events = self._clients()
        subject = f"[AquaSentinel {alert.get('severity_label', '')}] {alert.get('title', '')}"[:99]
        body = (
            f"{alert.get('title')}\n\n"
            f"{alert.get('reason')}\n\n"
            f"What to do: {alert.get('recommendation') or 'Monitor.'}\n\n"
            f"Site: {site_name}\nNode: {alert.get('device_id')}\n"
            f"Confidence: {alert.get('confidence')}\n"
            f"Evidence:\n"
            + "\n".join(
                f"  - {e.get('signal')} = {e.get('value')}"
                + (f" (threshold {e['threshold']})" if e.get("threshold") is not None else "")
                + (f" -- {e['note']}" if e.get("note") else "")
                for e in (alert.get("evidence") or [])
            )
            + "\n\nThis is an automated estimate from a monitoring prototype. "
              "Verify on site before acting on it."
        )
        if sns:
            try:
                sns.publish(
                    TopicArn=self.topic_arn,
                    Subject=subject,
                    Message=body,
                    MessageAttributes={
                        "severity": {"DataType": "String", "StringValue": str(alert.get("severity_label"))},
                        "module": {"DataType": "String", "StringValue": str(alert.get("module"))},
                    },
                )
            except Exception as exc:  # a failed notification must not fail ingestion
                print(json.dumps({"level": "error", "msg": "sns publish failed", "error": str(exc)}))
        if events:
            try:
                events.put_events(Entries=[{
                    "Source": "aquasentinel",
                    "DetailType": f"alert.{alert.get('module')}.{alert.get('severity_label', '').lower()}",
                    "Detail": json.dumps(alert),
                    "EventBusName": self.bus_name,
                }])
            except Exception as exc:
                print(json.dumps({"level": "error", "msg": "eventbridge put failed", "error": str(exc)}))


class Service:
    """The whole application, one object."""

    def __init__(self, repo=None, cfg: SiteConfig | None = None, notifier: Notifier | None = None,
                 clock=None):
        self.repo = repo if repo is not None else build_repo()
        self.notifier = notifier if notifier is not None else Notifier()
        # The clock is injected rather than assumed.
        #
        # Freshness is "how long since anything arrived", which needs the same
        # clock the arrivals are stamped with. In production that is wall time.
        # Under an accelerated simulation the rig's clock advances faster than
        # wall time by design, and comparing the two made a rig reporting every
        # few seconds look like it had drifted thirty hours. Handing the
        # service the clock its data is on keeps one code path honest in both
        # cases, instead of adding a "simulation mode" branch to the logic.
        self.clock = clock or time.time
        self._cfg = cfg
        self._cfg_loaded_at = 0.0
        self.engine = Engine(self.config())

    # -- configuration ---------------------------------------------------
    def config(self, force: bool = False) -> SiteConfig:
        """Site calibration, cached for a minute so Lambda does not re-read it."""
        if self._cfg is not None and not force:
            return self._cfg
        now = self.clock()
        if not force and now - self._cfg_loaded_at < 60 and getattr(self, "_cached", None):
            return self._cached
        site_id = os.environ.get("SITE_ID", DEFAULT_SITE.site_id)
        stored = None
        try:
            stored = self.repo.get_config(site_id)
        except Exception as exc:
            print(json.dumps({"level": "warn", "msg": "config read failed", "error": str(exc)}))
        cfg = SiteConfig.from_dict(stored) if stored else DEFAULT_SITE
        self._cached = cfg
        self._cfg_loaded_at = now
        self.engine = Engine(cfg)
        return cfg

    def save_config(self, cfg_dict: dict) -> SiteConfig:
        cfg = SiteConfig.from_dict(cfg_dict)
        self.repo.put_config(cfg.site_id, cfg.as_dict())
        self._cfg = None
        self._cached = cfg
        self._cfg_loaded_at = self.clock()
        self.engine = Engine(cfg)
        return cfg

    # -- write path ------------------------------------------------------
    def ingest(self, payload: dict, now: float | None = None) -> dict:
        """One telemetry record: evaluate, persist, notify."""
        cfg = self.config()
        device_id = str(payload.get("device_id") or payload.get("deviceId") or "unknown")
        item = self.repo.get_device(device_id) or {}
        state = (
            DeviceState.from_dict(item[STATE_KEY])
            if isinstance(item.get(STATE_KEY), dict) and item[STATE_KEY]
            else DeviceState(device_id)
        )

        result = self.engine.ingest(payload, state, now=now)

        for err in result.module_errors:
            print(json.dumps({"level": "error", "msg": "detection module failed",
                              "module": err["module"], "error": err["error"]}))

        meta = (cfg.devices or {}).get(device_id, {})
        received_at = self.clock()
        # Skew between the node's clock and ours. This is a different problem
        # from staleness and must not be confused with it: a node with a wrong
        # clock is still reporting, and a node with a perfect clock can still
        # be dead.
        skew_s = received_at - result.ts
        self.repo.put_device({
            "device_id": device_id,
            "label": meta.get("label", device_id),
            "location": meta.get("location"),
            "last_ts": result.ts,            # the device's own clock
            "last_seen_at": received_at,     # our clock, when we accepted it
            "clock_skew_s": round(skew_s, 1),
            "fw_version": (result.reading.fw_version if result.reading else ""),
            "rssi": (result.reading.rssi if result.reading else None),
            "samples": state.samples,
            "gaps": state.gaps,
            "online": True,
            "situation": result.situation,
            STATE_KEY: state.as_dict(),
        })

        if result.reading:
            self.repo.put_reading(device_id, result.ts, {
                "readings": result.reading.metrics,
                "quality": result.reading.quality,
                "seq": result.reading.seq,
                "buffered": result.reading.buffered,
                "severity": result.situation.get("overall_severity", 0),
            })

        published = []
        for alert in result.alerts:
            d = alert.as_dict()
            self.repo.put_alert(d)
            # A back-filled record describes the past. Paging someone about a
            # condition that was happening twenty minutes ago, as though it
            # were now, is how a monitoring system loses its audience.
            replayed = bool(result.reading and result.reading.buffered)
            if alert.state == "superseded":
                # Closing a superseded episode must not resurrect it and must
                # not notify: the replacement alert is the notification.
                self.repo.update_alert(d["alert_id"], state="superseded",
                                       cleared_ts=d.get("cleared_ts"))
                continue
            if alert.state == "active" and not replayed:
                self.notifier.publish(d, cfg.name)
                published.append(d["alert_id"])
            elif replayed:
                self.repo.update_alert(d["alert_id"], notified=False,
                                       note="raised from a back-filled reading; not notified")

        return {
            "ok": True,
            "device_id": device_id,
            "ts": result.ts,
            "overall_severity": result.situation.get("overall_severity", 0),
            "alerts_raised": [a.alert_id for a in result.alerts if a.state == "active"],
            "alerts_cleared": [a.alert_id for a in result.alerts if a.state == "cleared"],
            "notified": published,
            "rejected_metrics": result.rejected,
            "gap_detected": result.gap_detected,
            "module_errors": [e["module"] for e in result.module_errors],
        }

    # -- read path -------------------------------------------------------
    def overview(self) -> dict:
        cfg = self.config()
        devices = self.repo.list_devices()
        now = self.clock()
        sits, worst = [], 0
        for d in devices:
            sit = d.get("situation") or {}
            # Measured against OUR clock at the moment we accepted the
            # reading, not against the timestamp the node put on it. Those
            # differ whenever a node's clock is wrong or it is replaying
            # history, and using the node's version made a healthy rig
            # reporting every few seconds show up as stale, because its clock
            # sat in the past. The question is "how long since anything
            # arrived from this node", and only our own clock can answer it.
            age = now - float(d.get("last_seen_at") or d.get("last_ts") or 0)
            skew = float(d.get("clock_skew_s") or 0.0)
            # Freshness is computed here, at read time, not stored. A record
            # that was live when written is stale ten minutes later, and the
            # dashboard must never show the stored word "online" as if it were
            # still true.
            stale = age > cfg.stale_after_s
            sit = {**sit, "stale": stale, "age_s": round(age, 1),
                   "device_ts": d.get("last_ts"),
                   "clock_skew_s": round(skew, 1),
                   "clock_skew_warning": abs(skew) > max(300.0, cfg.stale_after_s * 2),
                   "label": d.get("label", d.get("device_id")),
                   "location": d.get("location")}
            if not stale:
                worst = max(worst, int(sit.get("overall_severity") or 0))
            sits.append(sit)

        queue: list[dict] = []
        for s in sits:
            if s.get("stale"):
                continue
            queue.extend(s.get("inspection_queue") or [])
        queue.sort(key=lambda i: (-i.get("priority", 0), -i.get("severity", 0)))
        for n, item in enumerate(queue, 1):
            item["rank"] = n

        return {
            "site": {"site_id": cfg.site_id, "name": cfg.name,
                     "calibrated": cfg.calibrated, "calibration_note": cfg.calibration_note,
                     "sample_interval_s": cfg.sample_interval_s,
                     "stale_after_s": cfg.stale_after_s},
            "generated_at": now,
            "overall_severity": worst,
            "overall_label": Severity(worst).label,
            "devices": sits,
            "inspection_queue": queue[:12],
            "active_alerts": self.repo.list_alerts(state="active", limit=50),
            "stale_devices": [s["device_id"] for s in sits if s.get("stale")],
        }

    def device_detail(self, device_id: str, window_s: float = 7200.0,
                      limit: int = 600) -> dict:
        cfg = self.config()
        d = self.repo.get_device(device_id)
        if not d:
            return {}
        # Readings are keyed by the device's own clock, so the history window
        # is relative to its latest timestamp rather than to ours. A node whose
        # clock is a week out still gets the right slice of its own history.
        latest = float(d.get("last_ts") or 0)
        readings = self.repo.query_readings(device_id, since=latest - window_s, limit=limit)
        if not readings:
            readings = self.repo.query_readings(device_id, since=None, limit=limit)
        now = self.clock()
        age = now - float(d.get("last_seen_at") or d.get("last_ts") or 0)
        skew = float(d.get("clock_skew_s") or 0.0)
        return {
            "device_id": device_id,
            "label": d.get("label", device_id),
            "location": d.get("location"),
            "last_ts": d.get("last_ts"),
            "last_seen_at": d.get("last_seen_at"),
            "clock_skew_s": round(skew, 1),
            "clock_skew_warning": abs(skew) > max(300.0, cfg.stale_after_s * 2),
            "age_s": round(age, 1),
            "stale": age > cfg.stale_after_s,
            "fw_version": d.get("fw_version"),
            "rssi": d.get("rssi"),
            "samples": d.get("samples"),
            "gaps": d.get("gaps"),
            "situation": d.get("situation") or {},
            "series": _to_series(readings),
            "alerts": self.repo.list_alerts(device_id=device_id, limit=50),
        }

    def alerts(self, state: str | None = None, device_id: str | None = None,
               limit: int = 200) -> dict:
        return {"alerts": self.repo.list_alerts(device_id=device_id, state=state, limit=limit)}

    def acknowledge(self, alert_id: str, who: str = "caretaker") -> dict:
        updated = self.repo.update_alert(
            alert_id, state="acknowledged", acknowledged_by=who, acknowledged_at=self.clock()
        )
        return {"ok": bool(updated), "alert": updated}

    def metrics(self) -> dict:
        """The measured results, read from the evaluation harness output."""
        for path in ("eval/results/metrics.json",
                     os.path.join(os.path.dirname(__file__), "../../eval/results/metrics.json")):
            try:
                with open(path) as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
        return {"error": "no metrics yet; run `make eval`"}


def _to_series(readings: list[dict]) -> dict[str, list[list[float]]]:
    """Pivot stored records into per-metric [[ts, value], ...] for charting."""
    out: dict[str, list[list[float]]] = {}
    for r in readings:
        ts = float(r.get("ts") or 0)
        metrics = r.get("readings") or {}
        quality = r.get("quality") or {}
        for name, val in metrics.items():
            if val is None:
                continue
            # Quality 3+ means the value was rejected as physically
            # impossible; charting it would draw a spike that never happened.
            if int(quality.get(name, 0) or 0) >= 3:
                continue
            out.setdefault(name, []).append([ts, float(val)])
    for arr in out.values():
        arr.sort(key=lambda p: p[0])
    return out
