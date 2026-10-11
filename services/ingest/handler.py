"""Ingest Lambda.

Reached two ways:
  * AWS IoT Core topic rule -- the production path, one invocation per MQTT
    message, which is how a real ESP32 fleet reports;
  * API Gateway POST /ingest -- for a node with no device certificate, and
    for the simulator when driving a deployed stack over HTTP.

The handler is deliberately thin: validate, hand to the service, return. All
the judgement lives in `engine/`, which is also what the tests exercise.
"""
from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.common.app import Service

_service: Service | None = None


def _svc() -> Service:
    # Built once per container, not per invocation: it caches the site config
    # and the boto3 clients, which is most of a cold start.
    global _service
    if _service is None:
        _service = Service()
    return _service


def _records(event: dict) -> list[dict]:
    """Pull telemetry payloads out of whichever shape the event arrived in."""
    # 1. IoT Core rule with `SELECT * FROM ...` delivers the payload directly.
    if "device_id" in event or "deviceId" in event:
        return [event]
    # 2. API Gateway (HTTP API or REST) wraps it in `body`.
    if "body" in event:
        body = event["body"]
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else [parsed]
    # 3. A batch, from Kinesis/SQS fan-in or a bulk replay.
    if isinstance(event.get("records"), list):
        return [r for r in event["records"] if isinstance(r, dict)]
    if isinstance(event.get("Records"), list):
        out = []
        for rec in event["Records"]:
            raw = rec.get("body") or rec.get("Sns", {}).get("Message")
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out
    return []


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json",
                    "access-control-allow-origin": "*"},
        "body": json.dumps(body),
    }


def handler(event, context):  # noqa: ARG001 - Lambda signature
    records = _records(event)
    if not records:
        print(json.dumps({"level": "warn", "msg": "no telemetry in event",
                          "keys": sorted(event.keys()) if isinstance(event, dict) else str(type(event))}))
        return _response(400, {"ok": False, "error": "no telemetry records in event"})

    svc = _svc()
    results, failures = [], []
    for rec in records:
        try:
            results.append(svc.ingest(rec))
        except Exception as exc:
            # One bad record must not reject a whole batch, and the failure has
            # to be visible rather than returned as a cheerful 200.
            print(json.dumps({"level": "error", "msg": "ingest failed",
                              "device_id": rec.get("device_id"), "error": f"{type(exc).__name__}: {exc}"}))
            failures.append({"device_id": rec.get("device_id"), "error": str(exc)})

    raised = [a for r in results for a in r.get("alerts_raised", [])]
    print(json.dumps({"level": "info", "msg": "ingested", "count": len(results),
                      "failed": len(failures), "alerts_raised": raised}))

    if failures and not results:
        return _response(500, {"ok": False, "failures": failures})
    return _response(200, {"ok": True, "ingested": len(results),
                           "results": results, "failures": failures})
