"""Read-side API Lambda, behind API Gateway (HTTP API).

Routes (all GET unless noted):
    GET  /overview                 site status, worst severity, inspection queue
    GET  /devices                  one entry per node
    GET  /devices/{id}             detail: situation, charts, that node's alerts
    GET  /alerts?state=&device=    alert history
    POST /alerts/{id}/ack          acknowledge
    GET  /config                   site calibration
    POST /config                   replace site calibration
    GET  /metrics                  the measured results from the eval harness
    GET  /health                   liveness
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.common.app import Service

_service: Service | None = None

CORS = {
    "content-type": "application/json",
    "access-control-allow-origin": os.environ.get("CORS_ORIGIN", "*"),
    "access-control-allow-headers": "content-type,x-api-key",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "cache-control": "no-store",
}


def _svc() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


def _resp(status: int, body) -> dict:
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def _route(event: dict) -> tuple[str, str]:
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()
    path = http.get("path") or event.get("path") or event.get("rawPath") or "/"
    stage = ctx.get("stage")
    if stage and stage != "$default" and path.startswith(f"/{stage}"):
        path = path[len(stage) + 1 :] or "/"
    return method, path.rstrip("/") or "/"


def handler(event, context):  # noqa: ARG001
    method, path = _route(event)
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    qs = event.get("queryStringParameters") or {}
    svc = _svc()
    try:
        if path in ("/", "/health"):
            return _resp(200, {"ok": True, "service": "aquasentinel-api",
                               "engine": __import__("engine").__version__})
        if path == "/overview":
            return _resp(200, svc.overview())
        if path == "/devices":
            return _resp(200, {"devices": svc.overview()["devices"]})
        if path.startswith("/devices/"):
            device_id = path.split("/devices/", 1)[1]
            detail = svc.device_detail(device_id, window_s=float(qs.get("window_s") or 7200))
            return _resp(200, detail) if detail else _resp(404, {"error": "unknown device"})
        if path == "/alerts" and method == "GET":
            return _resp(200, svc.alerts(state=qs.get("state"), device_id=qs.get("device"),
                                         limit=int(qs.get("limit") or 200)))
        if path.startswith("/alerts/") and path.endswith("/ack") and method == "POST":
            alert_id = path[len("/alerts/"):-len("/ack")]
            who = (json.loads(event.get("body") or "{}") or {}).get("who", "caretaker")
            out = svc.acknowledge(alert_id, who)
            return _resp(200 if out["ok"] else 404, out)
        if path == "/config" and method == "GET":
            return _resp(200, svc.config().as_dict())
        if path == "/config" and method == "POST":
            body = json.loads(event.get("body") or "{}")
            return _resp(200, svc.save_config(body).as_dict())
        if path == "/metrics":
            return _resp(200, svc.metrics())
        return _resp(404, {"error": f"no route for {method} {path}"})
    except Exception as exc:
        print(json.dumps({"level": "error", "msg": "api error", "path": path,
                          "error": f"{type(exc).__name__}: {exc}"}))
        return _resp(500, {"error": f"{type(exc).__name__}: {exc}"})
