"""Plain-language alert explanations, via Amazon Bedrock.

Where a language model belongs in this system, and where it does not.

**Does not:** decide anything. It never sets a severity, never predicts a
flood, never judges whether a flow anomaly is a leak. Those decisions come
from `engine/`, from arithmetic a person can check, because a number a judge
or a caretaker cannot audit is worth nothing in a safety-adjacent tool.

**Does:** turn the engine's evidence into two or three sentences a caretaker
can act on without knowing what a 95th percentile is. The model is given only
the structured evidence the engine already produced, and is explicitly told
not to add facts. If Bedrock is unavailable, unconfigured, or returns
something unusable, a deterministic template renders the same evidence -- so
the feature degrades to plain English rather than to nothing.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.common.app import Service

# Inference profile or model id. Anthropic models on Bedrock are addressed
# through a cross-region inference profile in most regions.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "apac.anthropic.claude-sonnet-4-5-20250929-v1:0")

SYSTEM = """You write one short notification for the caretaker of a school water system.

You will be given a monitoring alert as structured JSON: what was measured, which
thresholds were crossed, and the system's own confidence.

Rules, all of which matter more than style:
- Use ONLY the numbers in the JSON. Do not add a fact, a cause or a number that is not there.
- Never state a cause. The data shows what happened together, not why. Say "while", not "because".
- Keep every hedge the JSON carries. If it says "possible water loss", do not write "leak".
- If confidence is below 0.5, say plainly that the reading needs checking on site.
- Plain language, no jargon: no "p95", no "anomaly score", no "rate of rise" without units.
- At most 3 sentences, under 60 words. Say what happened, then what to do first.
- No greeting, no sign-off, no markdown."""


def _template(alert: dict) -> str:
    """The fallback, and the floor on quality: always correct, never fluent."""
    bits = [alert.get("reason") or alert.get("title") or "An alert was raised."]
    ev = alert.get("evidence") or []
    if ev:
        parts = []
        for e in ev[:3]:
            frag = f"{str(e.get('signal', '')).replace('_', ' ')} was {e.get('value')}"
            if e.get("threshold") is not None:
                frag += f" against a threshold of {e['threshold']}"
            parts.append(frag)
        bits.append("Evidence: " + "; ".join(parts) + ".")
    if alert.get("recommendation"):
        bits.append(str(alert["recommendation"]))
    try:
        if float(alert.get("confidence") or 1.0) < 0.5:
            bits.append("Confidence in this estimate is low; please verify on site.")
    except (TypeError, ValueError):
        pass
    return " ".join(bits)


def explain(alert: dict) -> tuple[str, str]:
    """Return (text, source) where source is 'bedrock' or 'template'."""
    if os.environ.get("DISABLE_BEDROCK"):
        return _template(alert), "template"
    try:
        import boto3

        client = boto3.client("bedrock-runtime")
        payload = {
            "title": alert.get("title"),
            "module": alert.get("module"),
            "severity": alert.get("severity_label"),
            "confidence": alert.get("confidence"),
            "finding": alert.get("reason"),
            "evidence": alert.get("evidence"),
            "suggested_action": alert.get("recommendation"),
        }
        resp = client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [{"text": json.dumps(payload, default=str)}]}],
            inferenceConfig={"maxTokens": 220, "temperature": 0.2},
        )
        text = "".join(
            block.get("text", "")
            for block in resp["output"]["message"]["content"]
        ).strip()
        if not text:
            return _template(alert), "template"
        # A model that ignored the word limit has probably ignored the other
        # rules too, so fall back rather than ship it.
        if len(text.split()) > 90:
            print(json.dumps({"level": "warn", "msg": "bedrock reply too long, using template",
                              "words": len(text.split())}))
            return _template(alert), "template"
        return text, "bedrock"
    except Exception as exc:
        print(json.dumps({"level": "warn", "msg": "bedrock unavailable, using template",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return _template(alert), "template"


_service: Service | None = None


def _svc() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


def handler(event, context):  # noqa: ARG001
    """Triggered by EventBridge on a new alert, or invoked directly.

    Writes the explanation back onto the alert so the dashboard and the
    notification can both use it.
    """
    alert = (event or {}).get("detail") or (event or {}).get("alert") or event or {}
    if not alert.get("alert_id"):
        return {"ok": False, "error": "no alert in event"}

    svc = _svc()
    stored = svc.repo.get_alert(alert["alert_id"]) or alert
    text, source = explain(stored)
    svc.repo.update_alert(alert["alert_id"], explanation=text, explanation_source=source)
    print(json.dumps({"level": "info", "msg": "explained", "alert_id": alert["alert_id"],
                      "source": source}))
    return {"ok": True, "alert_id": alert["alert_id"], "explanation": text, "source": source}
