"""Where the virtual node sends its records.

Four transports, all with the same `send(payload) -> bool` contract:

  * `stdout`  -- print the JSON, for eyeballing the firmware output
  * `http`    -- POST to the ingest endpoint (local dev server or API Gateway)
  * `iot`     -- publish over MQTT to AWS IoT Core, the production path
  * `null`    -- discard, used when the engine is driven in-process

`send` returning False means "the link is down": the node then buffers, which
is what makes the connectivity-loss test meaningful.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Protocol


class Transport(Protocol):
    def send(self, payload: dict) -> bool: ...
    def close(self) -> None: ...


class StdoutTransport:
    def __init__(self, pretty: bool = False):
        self.pretty = pretty
        self.count = 0

    def send(self, payload: dict) -> bool:
        self.count += 1
        sys.stdout.write(json.dumps(payload, indent=2 if self.pretty else None) + "\n")
        return True

    def close(self) -> None:
        sys.stdout.flush()


class NullTransport:
    def __init__(self) -> None:
        self.count = 0

    def send(self, payload: dict) -> bool:
        self.count += 1
        return True

    def close(self) -> None:
        pass


class HttpTransport:
    """POST to /ingest. Works against the local server and API Gateway alike."""

    def __init__(self, url: str, api_key: str | None = None, timeout: float = 6.0):
        self.url = url.rstrip("/")
        if not self.url.endswith("/ingest"):
            self.url += "/ingest"
        self.api_key = api_key or os.environ.get("AQUASENTINEL_API_KEY")
        self.timeout = timeout
        self.count = 0
        self.failures = 0

    def send(self, payload: dict) -> bool:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("x-api-key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            self.failures += 1
            return False
        if ok:
            self.count += 1
        else:
            self.failures += 1
        return ok

    def close(self) -> None:
        pass


class IotCoreTransport:
    """Publish to AWS IoT Core over MQTT with mutual TLS.

    This is the production uplink: it is what an ESP32 running the AWS IoT
    Device SDK would do, authenticating with its own X.509 certificate rather
    than a shared API key.  It needs `awsiotsdk` and a provisioned device
    certificate; `scripts/provision_device.sh` creates both.
    """

    def __init__(self, endpoint: str, cert: str, key: str, ca: str,
                 client_id: str, topic: str = "aquasentinel/telemetry"):
        try:
            from awscrt import io, mqtt  # noqa: F401
            from awsiot import mqtt_connection_builder
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "AWS IoT transport needs the device SDK: pip install awsiotsdk"
            ) from exc

        self.topic = topic
        self.count = 0
        self.failures = 0
        self._conn = mqtt_connection_builder.mtls_from_path(
            endpoint=endpoint,
            cert_filepath=cert,
            pri_key_filepath=key,
            ca_filepath=ca,
            client_id=client_id,
            clean_session=False,
            keep_alive_secs=30,
        )
        self._conn.connect().result(timeout=15)

    def send(self, payload: dict) -> bool:
        from awscrt import mqtt

        try:
            # QoS 1: the broker must acknowledge. Telemetry we are willing to
            # lose is telemetry we should not have alerted on.
            fut, _ = self._conn.publish(
                topic=f"{self.topic}/{payload.get('device_id', 'unknown')}",
                payload=json.dumps(payload),
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
            fut.result(timeout=10)
        except Exception:
            self.failures += 1
            return False
        self.count += 1
        return True

    def close(self) -> None:
        try:
            self._conn.disconnect().result(timeout=10)
        except Exception:
            pass


def build(kind: str, **kw) -> Transport:
    kind = (kind or "stdout").lower()
    if kind == "stdout":
        return StdoutTransport(pretty=bool(kw.get("pretty")))
    if kind == "null":
        return NullTransport()
    if kind == "http":
        url = kw.get("url") or os.environ.get("AQUASENTINEL_API_URL")
        if not url:
            raise SystemExit("http transport needs --url or AQUASENTINEL_API_URL")
        return HttpTransport(url, kw.get("api_key"))
    if kind == "iot":
        endpoint = kw.get("endpoint") or os.environ.get("AWS_IOT_ENDPOINT")
        cert = kw.get("cert") or os.environ.get("AWS_IOT_CERT")
        key = kw.get("key") or os.environ.get("AWS_IOT_KEY")
        ca = kw.get("ca") or os.environ.get("AWS_IOT_CA")
        missing = [n for n, v in (("endpoint", endpoint), ("cert", cert), ("key", key), ("ca", ca)) if not v]
        if missing:
            raise SystemExit(
                "iot transport needs " + ", ".join(missing)
                + " (run scripts/provision_device.sh, then source .env.iot)"
            )
        return IotCoreTransport(endpoint, cert, key, ca,
                                client_id=kw.get("client_id") or "aquasentinel-sim",
                                topic=kw.get("topic") or "aquasentinel/telemetry")
    raise SystemExit(f"unknown transport {kind!r}")
