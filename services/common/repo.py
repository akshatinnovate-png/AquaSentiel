"""Storage, behind one interface with two implementations.

`DynamoRepo` is the AWS path. `MemoryRepo` is the same contract backed by
dictionaries, which is what lets the entire product run offline with no AWS
account -- the local dev server, the tests and the evaluation harness all use
it. Writing to one interface is what keeps those two paths honest with each
other: there is no "local mode" branch inside the business logic.

Tables (single-table-adjacent, but kept as four for clarity under time pressure):

  devices  PK device_id                     -- metadata, last-seen, engine state
  readings PK device_id, SK ts              -- time series, with a TTL
  alerts   PK alert_id, GSI device_id+ts    -- alert episodes and their lifecycle
  config   PK site_id                       -- site calibration
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any, Iterable


def _clean(obj: Any) -> Any:
    """Make a value safe for DynamoDB: floats become Decimal, NaN is dropped."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None or k in ("value",)}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _unclean(obj: Any) -> Any:
    """Reverse of `_clean`, so callers never see a Decimal."""
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    if isinstance(obj, dict):
        return {k: _unclean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_unclean(v) for v in obj]
    return obj


class MemoryRepo:
    """In-process store with the same contract as the DynamoDB one."""

    def __init__(self, readings_cap: int = 20000):
        self.devices: dict[str, dict] = {}
        self.readings: dict[str, list[dict]] = {}
        self.alerts: dict[str, dict] = {}
        self.config: dict[str, dict] = {}
        self.readings_cap = readings_cap

    # -- devices ---------------------------------------------------------
    def get_device(self, device_id: str) -> dict | None:
        return self.devices.get(device_id)

    def put_device(self, item: dict) -> None:
        self.devices[item["device_id"]] = item

    def list_devices(self) -> list[dict]:
        return sorted(self.devices.values(), key=lambda d: d.get("device_id", ""))

    # -- readings --------------------------------------------------------
    def put_reading(self, device_id: str, ts: float, payload: dict) -> None:
        arr = self.readings.setdefault(device_id, [])
        arr.append({"device_id": device_id, "ts": ts, **payload})
        if len(arr) > self.readings_cap:
            del arr[0 : len(arr) - self.readings_cap]

    def query_readings(self, device_id: str, since: float | None = None,
                       limit: int = 500) -> list[dict]:
        arr = self.readings.get(device_id, [])
        if since is not None:
            arr = [r for r in arr if r["ts"] >= since]
        return arr[-limit:]

    # -- alerts ----------------------------------------------------------
    def put_alert(self, item: dict) -> None:
        existing = self.alerts.get(item["alert_id"])
        if existing and existing.get("state") == "acknowledged" and item.get("state") == "active":
            # Never silently un-acknowledge: a caretaker already saw this one.
            item = {**item, "state": "acknowledged",
                    "acknowledged_by": existing.get("acknowledged_by")}
        self.alerts[item["alert_id"]] = item

    def get_alert(self, alert_id: str) -> dict | None:
        return self.alerts.get(alert_id)

    def list_alerts(self, device_id: str | None = None, state: str | None = None,
                    limit: int = 200) -> list[dict]:
        out = list(self.alerts.values())
        if device_id:
            out = [a for a in out if a.get("device_id") == device_id]
        if state:
            out = [a for a in out if a.get("state") == state]
        out.sort(key=lambda a: a.get("ts", 0), reverse=True)
        return out[:limit]

    def update_alert(self, alert_id: str, **fields) -> dict | None:
        a = self.alerts.get(alert_id)
        if not a:
            return None
        a.update(fields)
        return a

    # -- config ----------------------------------------------------------
    def get_config(self, site_id: str) -> dict | None:
        return self.config.get(site_id)

    def put_config(self, site_id: str, cfg: dict) -> None:
        self.config[site_id] = cfg


class DynamoRepo:
    """DynamoDB-backed store. Table names come from the environment."""

    def __init__(self, devices: str | None = None, readings: str | None = None,
                 alerts: str | None = None, config: str | None = None,
                 reading_ttl_days: int = 30):
        import boto3

        ddb = boto3.resource("dynamodb")
        self.t_devices = ddb.Table(devices or os.environ["DEVICES_TABLE"])
        self.t_readings = ddb.Table(readings or os.environ["READINGS_TABLE"])
        self.t_alerts = ddb.Table(alerts or os.environ["ALERTS_TABLE"])
        self.t_config = ddb.Table(config or os.environ["CONFIG_TABLE"])
        self.reading_ttl_days = int(os.environ.get("READING_TTL_DAYS", reading_ttl_days))

    # -- devices ---------------------------------------------------------
    def get_device(self, device_id: str) -> dict | None:
        r = self.t_devices.get_item(Key={"device_id": device_id})
        item = r.get("Item")
        return _unclean(item) if item else None

    def put_device(self, item: dict) -> None:
        self.t_devices.put_item(Item=_clean(item))

    def list_devices(self) -> list[dict]:
        # A site has a handful of nodes, so a scan is the right tool here; it
        # would not be at a thousand.
        out, kw = [], {}
        while True:
            r = self.t_devices.scan(**kw)
            out.extend(_unclean(i) for i in r.get("Items", []))
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return sorted(out, key=lambda d: d.get("device_id", ""))

    # -- readings --------------------------------------------------------
    def put_reading(self, device_id: str, ts: float, payload: dict) -> None:
        item = {
            "device_id": device_id,
            "ts": Decimal(str(round(ts, 3))),
            # Raw telemetry is bulky and only interesting for a month; the TTL
            # expires it automatically so storage does not grow without bound.
            "expires_at": int(ts) + self.reading_ttl_days * 86400,
            **payload,
        }
        self.t_readings.put_item(Item=_clean(item))

    def query_readings(self, device_id: str, since: float | None = None,
                       limit: int = 500) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        cond = Key("device_id").eq(device_id)
        if since is not None:
            cond = cond & Key("ts").gte(Decimal(str(round(since, 3))))
        r = self.t_readings.query(
            KeyConditionExpression=cond,
            Limit=limit,
            ScanIndexForward=False,   # newest first, then reverse for the UI
        )
        items = [_unclean(i) for i in r.get("Items", [])]
        items.reverse()
        return items

    # -- alerts ----------------------------------------------------------
    def put_alert(self, item: dict) -> None:
        existing = self.get_alert(item["alert_id"])
        if existing and existing.get("state") == "acknowledged" and item.get("state") == "active":
            item = {**item, "state": "acknowledged",
                    "acknowledged_by": existing.get("acknowledged_by")}
        self.t_alerts.put_item(Item=_clean(item))

    def get_alert(self, alert_id: str) -> dict | None:
        r = self.t_alerts.get_item(Key={"alert_id": alert_id})
        item = r.get("Item")
        return _unclean(item) if item else None

    def list_alerts(self, device_id: str | None = None, state: str | None = None,
                    limit: int = 200) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        if device_id:
            r = self.t_alerts.query(
                IndexName="by_device",
                KeyConditionExpression=Key("device_id").eq(device_id),
                ScanIndexForward=False,
                Limit=limit,
            )
            items = [_unclean(i) for i in r.get("Items", [])]
        else:
            r = self.t_alerts.scan(Limit=limit)
            items = [_unclean(i) for i in r.get("Items", [])]
            items.sort(key=lambda a: a.get("ts", 0), reverse=True)
        if state:
            items = [a for a in items if a.get("state") == state]
        return items[:limit]

    def update_alert(self, alert_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_alert(alert_id)
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": _clean(v) for i, v in enumerate(fields.values())}
        expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        r = self.t_alerts.update_item(
            Key={"alert_id": alert_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return _unclean(r.get("Attributes"))

    # -- config ----------------------------------------------------------
    def get_config(self, site_id: str) -> dict | None:
        r = self.t_config.get_item(Key={"site_id": site_id})
        item = r.get("Item")
        if not item:
            return None
        raw = item.get("config")
        if isinstance(raw, str):
            return json.loads(raw)
        return _unclean(raw)

    def put_config(self, site_id: str, cfg: dict) -> None:
        # Stored as a JSON string: the config is nested and read whole, and
        # this sidesteps float/Decimal conversion entirely.
        self.t_config.put_item(Item={
            "site_id": site_id,
            "config": json.dumps(cfg),
            "updated_at": int(time.time()),
        })


def build_repo() -> "MemoryRepo | DynamoRepo":
    """Pick the store from the environment: DynamoDB in AWS, memory locally."""
    if os.environ.get("DEVICES_TABLE"):
        return DynamoRepo()
    return MemoryRepo()
