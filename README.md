# AquaSentinel

**Smart water intelligence network** — flooding, water loss, stored supply and
heat-driven demand, on one operational dashboard.

> Four risks. One network.

AquaSentinel turns raw sensor readings into *earlier, more actionable* warnings
for the person who actually has to do something about them: the caretaker of a
school, a community water manager, a municipal maintenance team.

It is a working prototype, and it is careful about the difference between what
it **measures**, what it **detects**, and what it **predicts**. Every number on
the dashboard carries that label, and every detection figure in this README
comes from a reproducible measurement, not an estimate.

---

## Run it in one command

No AWS account, no `pip install`, no build step. Python 3.11+ and nothing else:

```bash
make demo
# then open http://127.0.0.1:8080
```

That starts the backend, runs a warm-up so the detectors learn what *normal*
looks like, then plays a 30-hour scenario through the real detection engine:
a leak starting at midnight, a refill that never arrives, a hot afternoon, and
a downpour onto a blocked drain. You will watch all four modules fire in order.

```bash
make eval     # measure the detectors; writes eval/results/REPORT.md
make test     # 103 unit and integration tests
make deploy   # push the whole thing to AWS (100 resources, 17 services)
```

---

## What it actually does

| Module | Measures | Detects | Predicts |
|---|---|---|---|
| **A — Flood** | water depth at a surveyed low point, rainfall intensity | rising water, rate of rise | time to the surveyed danger depth *at the current rate* |
| **B — Leak** | flow on the service line | minimum night flow, flow sustained above the learned normal | volume lost above baseline |
| **C — Supply** | tank level | low stored volume, abnormal drain, failed refill | time to depletion, with a range |
| **D — Heat demand** | temperature, humidity | heat index above a local advisory level **and** above this site's own normal peak | water demand for the next 6 hours |

And the part that makes it one product rather than four tabs: **Module D's
forecast feeds Module C's projection.** The dashboard can tell a caretaker
*"at today's heat you run dry before the evening, not tomorrow morning"* —
a number neither module can produce alone.

### What it does not claim

- It is **not a flood forecast.** It detects rising water at one instrumented
  point. Predicting where water goes next needs drainage topology, catchment
  data and validation this system does not have.
- A flow anomaly is **"possible loss"**, never "a leak". A burst pipe and a tap
  left running are identical to a flow meter.
- Heat bands are **local advisory levels**, not a heatwave declaration. Only a
  meteorological agency (the IMD, in India) can declare one.
- Thresholds are **site configuration**, not public-safety limits. They must be
  surveyed per location before any real deployment.

---

## Measured results

From `make eval`, which drives the real engine over simulated scenarios and
scores it against the simulator's own ground truth. Full report with sample
counts: **[eval/results/REPORT.md](eval/results/REPORT.md)**.

| Scenario | Module | Detection delay | False alarms | Samples |
|---|---|---|---|---|
| quiet site, 24 h | — *(nothing should fire)* | — | **0** | 11,522 |
| heavy rain, blocked drain | flood | **0.8 min** | 0 | 1,082 |
| 0.6 L/min overnight leak | leak | **30.5 min** | 0 | 4,802 |
| refill never arrives | tank | **52.3 min** | 0 | 6,722 |
| heat spell | demand | **1.0 min** | 0 | 17,282 |
| heat spell | tank | **48.3 min** | 0 | 17,282 |
| 14 days of operation | demand | **4.0 min** | 0 | 40,322 |
| 12-min uplink outage | flood | **8.3 min** | 0 | 722 |
| sensor disconnected | device health | **0.5 min** | 0 | 542 |
| full 30 h demo | all four | 2.0 / 2.5 / 31.0 / 39.5 min | 0 | 7,202 |

**Zero missed detections and zero false alarms across all nine scenarios and
90,198 ingested samples.**

**Estimate accuracy**

- Tank volume: **MAE 6.6–10.5 L**, which is **0.13–0.21% of the 5,000 L
  capacity** (n = 181–20,161 per scenario).
- Depletion ETA: **median error 0.37 h — 12.8% of the true time remaining**
  (n = 1,284), scored only while there is a meaningful draw, because the
  estimate explicitly assumes the current rate continues.

**Does the machine learning earn its place?**

| History available | Baseline MAE | Weather-aware model MAE | Verdict |
|---|---|---|---|
| 30 h (39 train / 14 holdout) | 118.46 L/h | 120.54 L/h | **baseline kept** |
| 36 h (44 train / 15 holdout) | 13.88 L/h | 18.12 L/h | **baseline kept** |
| 14 days (269 train / 90 holdout) | 29.99 L/h | **23.85 L/h** | **model adopted, −20.4%** |

The weather-aware model is used **only when it beats the seasonal-naive
baseline on data it has never seen.** On one or two days of history it does
not — twice — and the dashboard says so. On fourteen days it cuts held-out
error by a fifth. That gate is in the code, not in this README.

---

## Architecture

```
  ESP32 node  ──MQTT/mTLS, QoS 1──▶  AWS IoT Core
  (or HTTP POST)                        │
                                        ├──▶ Lambda: ingest ──▶ DynamoDB  (devices, readings+TTL, alerts)
                                        │          │
                                        │          ├──▶ SNS ──▶ email / SMS
                                        │          └──▶ EventBridge ──┬──▶ Lambda: explain (Bedrock)
                                        │                             └──▶ Step Functions: escalation
                                        └──▶ Firehose ──▶ S3 lake ──▶ Glue / Athena

  EventBridge (every 5 min) ──▶ Lambda: sweep        detects nodes that went silent
  API Gateway (HTTP API)    ──▶ Lambda: api          dashboard reads
  S3 + CloudFront           ──▶ the dashboard
  CloudWatch                ──▶ alarms, dashboard, X-Ray traces
```

**17 AWS services, 100 resources**, all in `infra/` as CDK. See
[docs/AWS_SERVICES.md](docs/AWS_SERVICES.md) for what each one earns its place
doing — and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the decisions
behind it.

### The one design rule

> The same detection code runs in Lambda, in the local server, in the
> evaluation harness and in the tests.

`engine/` is a **dependency-free Python package** — no boto3, no numpy, nothing.
That is why `make demo` needs no cloud and `make deploy` needs no build step,
and why the numbers in the table above describe the code that actually runs in
production. There is no "local mode" branch anywhere in the business logic.

```
engine/     detection and prediction — pure Python, no dependencies
sim/        the virtual rig: physics-based sensors and ESP32 firmware behaviour
services/   Lambda handlers, thin wrappers over engine/
local/      a stdlib server exposing the same API offline
web/        the dashboard — no build step, no external script or font
infra/      AWS CDK stack
eval/       the measurement harness that produced the table above
tests/      103 tests, including one regression test per bug found
```

---

## No hardware? That is deliberate

There is no physical rig. Instead `sim/` is a **virtual ESP32 with
physics-based sensors**, which is a stronger basis for the numbers above than a
bench rig would have been in the time available:

- the tank integrates net flow, so level responds as a real tank does;
- the street channel fills from runoff (mm × m² = litres) and drains through an
  orifice at √h, so ponding has the right shape — fast rise, slow tail;
- the flow meter quantises to whole turbine pulses and reports **exactly zero**
  below its cut-in speed;
- temperature and humidity are anti-correlated, so the heat index is physical;
- sensors have noise, resolution limits, dropouts, outlier spikes, and
  injectable **stuck** and **disconnected** faults.

The firmware model does what firmware does: samples, rejects impossible values
at the edge, timestamps, evaluates cheap local thresholds, buffers across an
outage and replays oldest-first with a `buffered` flag.

**Swapping in real hardware changes one thing: the transport.** `sim/run.py
--transport iot` already speaks MQTT to AWS IoT Core with a per-device X.509
certificate — the same path an ESP32 would use.

---

## Honesty, as a feature

This is a safety-adjacent tool, so the dashboard is built to make
over-claiming hard:

- **Every value is badged** `measured`, `derived`, `projected` or `forecast`.
- **Every alert shows its evidence** — which signal, what value, which
  threshold, why it counts.
- **A stale node is never shown as live.** Freshness is measured on the
  server's clock at read time, and clock skew is reported separately, because a
  node with a wrong clock is still reporting and a node with a perfect clock
  can still be dead.
- **A dead sensor reads "unavailable", never zero** — including one that
  freezes at a plausible value, caught by its variance collapsing to exactly
  zero.
- **A fault is a rate, not an event.** A sensor dropping one reading in a
  hundred is working normally; alerting on each one filled the history with
  dozens of "node health, cleared" entries and buried the alerts that
  mattered. A fault now needs four rejections in ten minutes — which costs
  30 seconds of detection delay on a disconnected sensor, and is worth it.
- **A projection cannot outrank the measurement it is built on.** A half-full
  tank does not reach WARNING because a lunchtime peak implies a short ETA.
- **Amazon Bedrock explains, it never decides.** Severities come from
  arithmetic a person can check. The model only turns that evidence into two
  plain sentences, and falls back to a template if it is unavailable.

---

## Deploying to AWS

```bash
cd infra
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
npx cdk bootstrap                        # once per account/region
npx cdk deploy -c alert_email=you@example.com
```

Outputs give you the dashboard URL, the API endpoint and the IoT topic. Then
point the virtual rig at the real cloud:

```bash
./scripts/provision_device.sh aquasentinel-node-01   # issues an X.509 cert
pip install awsiotsdk
source .env.iot
python -m sim.run --transport iot --scenario demo --speed 60
```

Watch the messages arrive in **IoT Core → MQTT test client**, subscribed to
`aquasentinel/telemetry/#`.

Everything is pay-per-request or free-tier-shaped (DynamoDB on-demand, Lambda,
a 5 min EventBridge rule). `make destroy` removes it all.

---

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works and why, including the decisions that were wrong first |
| [docs/AWS_SERVICES.md](docs/AWS_SERVICES.md) | Every AWS service and what it earns its place doing |
| [docs/ALGORITHMS.md](docs/ALGORITHMS.md) | The detection maths, module by module |
| [docs/TESTING.md](docs/TESTING.md) | How the numbers above were produced, and how to reproduce them |
| [docs/DEMO.md](docs/DEMO.md) | The three-minute video, shot by shot |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What this does not do, in detail |
| [docs/12_HOUR_PLAN.md](docs/12_HOUR_PLAN.md) | The compressed build plan, and what to cut first |
| [docs/SUBMISSION.md](docs/SUBMISSION.md) | Competition checklist |

---

*A monitoring prototype, not a certified safety device. Use official
meteorological alerts for any public heat or flood warning.*
