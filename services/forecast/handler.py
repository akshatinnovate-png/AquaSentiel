"""Scheduled re-evaluation Lambda, fired by EventBridge.

Telemetry-driven evaluation can only react to a reading that arrives. Two
things need a clock instead:

  * **a node that has gone quiet.** Silence is the one condition no incoming
    message will ever tell you about, and a dashboard that shows the last
    known state as though it were current is worse than one that shows
    nothing. This is the single most important job here.
  * **refreshing the demand forecast** so the next few hours are projected
    against the latest history even on a quiet afternoon.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.model import Severity
from services.common.app import STATE_KEY, Service

_service: Service | None = None


def _svc() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


def handler(event, context):  # noqa: ARG001
    svc = _svc()
    cfg = svc.config(force=True)
    now = time.time()
    # A node is only called offline once it has missed several intervals, so a
    # single dropped message does not raise an alarm.
    offline_after = max(float(cfg.stale_after_s) * 3, 300.0)

    gone_quiet, refreshed = [], []
    for device in svc.repo.list_devices():
        device_id = device.get("device_id")
        if not device_id:
            continue
        # Our clock, not the node's: a node with a skewed clock has not
        # gone quiet, and must not be reported as though it had.
        age = now - float(device.get("last_seen_at") or device.get("last_ts") or 0)
        if age <= offline_after:
            refreshed.append(device_id)
            continue

        gone_quiet.append({"device_id": device_id, "age_s": round(age, 1)})
        alert_id = f"device-offline-{device_id}"
        if (svc.repo.get_alert(alert_id) or {}).get("state") in ("active", "acknowledged"):
            continue   # already reported; do not re-page every five minutes

        alert = {
            "alert_id": alert_id,
            "device_id": device_id,
            "module": "device",
            "severity": int(Severity.WARNING),
            "severity_label": Severity.WARNING.label,
            "title": "Node has stopped reporting",
            "reason": (
                f"No reading from {device.get('label') or device_id} for "
                f"{age / 60:.0f} minutes (expected every {cfg.sample_interval_s}s). "
                f"Everything this node measures is now unmonitored, and the last "
                f"values shown for it describe the past, not the present."
            ),
            "recommendation": "Check the node's power, wiring and Wi-Fi link.",
            "evidence": [{"signal": "reading_age_s", "value": round(age, 1),
                          "threshold": round(offline_after, 1),
                          "note": "time since the last accepted reading"}],
            "ts": now,
            "state": "active",
            "confidence": 1.0,
        }
        svc.repo.put_alert(alert)
        svc.notifier.publish(alert, cfg.name)
        # Mark it offline so the overview stops implying the node is healthy.
        device["online"] = False
        svc.repo.put_device(device)

    # Clear any offline alert for a node that has come back.
    recovered = []
    for device_id in refreshed:
        alert_id = f"device-offline-{device_id}"
        existing = svc.repo.get_alert(alert_id)
        if existing and existing.get("state") in ("active", "acknowledged"):
            svc.repo.update_alert(alert_id, state="cleared", cleared_ts=now)
            recovered.append(device_id)

    out = {"checked": len(refreshed) + len(gone_quiet), "offline": gone_quiet,
           "recovered": recovered, "online": len(refreshed)}
    print(json.dumps({"level": "info", "msg": "scheduled sweep", **out}))
    return out
