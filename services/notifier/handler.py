"""Escalation Lambda, driven by a Step Functions state machine.

Why a state machine rather than just sending an SNS message: a critical alert
that nobody acknowledges needs to be raised again, and "wait thirty minutes,
then check whether a human has acted, then escalate" is a workflow, not a
function. Expressing it in Step Functions keeps the waiting out of Lambda
(where it would be billed and capped) and makes the escalation path auditable
in the console -- you can see exactly which alert is waiting on whom.

Two actions, selected by `action` in the input:
    notify  -- send the first notification
    check   -- report whether the alert has been acknowledged yet
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.common.app import Service

_service: Service | None = None


def _svc() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


def handler(event, context):  # noqa: ARG001
    svc = _svc()
    action = (event or {}).get("action", "check")
    alert_id = (event or {}).get("alert_id") or ((event or {}).get("detail") or {}).get("alert_id")
    if not alert_id:
        return {"ok": False, "error": "no alert_id in input"}

    alert = svc.repo.get_alert(alert_id)
    if not alert:
        return {"ok": False, "error": "unknown alert", "alert_id": alert_id}

    if action == "notify":
        svc.notifier.publish(alert, svc.config().name)
        svc.repo.update_alert(alert_id, notified_at=time.time(),
                              escalation_level=int(alert.get("escalation_level") or 0))
        return {"ok": True, "alert_id": alert_id, "state": alert.get("state"), "action": "notified"}

    state = alert.get("state")
    if state in ("acknowledged", "cleared"):
        return {"ok": True, "alert_id": alert_id, "state": state, "resolved": True}

    level = int(alert.get("escalation_level") or 0) + 1
    escalated = dict(alert)
    escalated["title"] = f"Still unacknowledged: {alert.get('title')}"
    escalated["reason"] = (
        f"This alert has been active for "
        f"{(time.time() - float(alert.get('ts') or time.time())) / 60:.0f} minutes "
        f"with no acknowledgement.\n\n{alert.get('reason')}"
    )
    svc.notifier.publish(escalated, svc.config().name)
    svc.repo.update_alert(alert_id, escalation_level=level, escalated_at=time.time())
    print(json.dumps({"level": "info", "msg": "escalated", "alert_id": alert_id, "level": level}))
    return {"ok": True, "alert_id": alert_id, "state": state, "resolved": False,
            "escalation_level": level}
