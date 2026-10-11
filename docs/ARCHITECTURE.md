# Architecture

## The one decision everything else follows from

**The detection engine is a dependency-free Python package.**

`engine/` imports nothing outside the standard library — no boto3, no numpy, no
pandas, no scikit-learn. Every consequence below falls out of that:

| Because the engine has no dependencies | …this is true |
|---|---|
| it runs unchanged in Lambda | no layers, no container, no build step; the deploy asset is the repo |
| it runs unchanged on a laptop | `make demo` needs no AWS account and no `pip install` |
| it runs unchanged in the test suite | 99 tests exercise the code that actually ships |
| it runs unchanged in the eval harness | the published detection figures describe production code |

The alternative — a cloud implementation and a separate "demo mode" — produces
two systems that drift apart, and measurements that describe neither. There is
no `if LOCAL:` branch anywhere in the business logic. The only thing that
differs between a laptop and AWS is which `Repo` class is constructed, and the
two satisfy the same interface.

## Layers

```
   Reading  (what we measured)
      │  validate against physical ranges; reject impossible values
      ▼
   DeviceState  (bounded rolling memory, fully serialisable)
      │
      ├──▶ Module A: flood      ─┐
      ├──▶ Module B: leak        │  each returns a ModuleResult:
      ├──▶ Module C: tank        │  severity + state + signals + evidence
      ├──▶ Module D: demand     ─┘
      │
      ▼
   Latches  (dwell, hysteresis, cooldown)   ──▶ Alert episodes
      │
      ▼
   Fusion  (supply outlook, co-occurrence, inspection queue)
      │
      ▼
   Situation report  ──▶ dashboard, notifications
```

### Reading → Signal → Alert

Three types, deliberately separate, and the most important modelling decision
in the project:

- a **Reading** is what a sensor said;
- a **Signal** is something we computed, tagged `measured`, `derived`,
  `projected` or `forecast`;
- an **Alert** is a decision, carrying the evidence that produced it.

The dashboard renders each kind differently. That is what makes it structurally
hard to show a six-hour forecast as though it were a thermometer reading.

### DeviceState

All memory lives in one serialisable object per node. The detection modules are
stateless functions over it. This matters because Lambda is stateless: the
state round-trips through DynamoDB on every invocation, so every buffer is
bounded by **both age and count** (`MAX_POINTS = 480`, `MAX_AGE_S = 7200`) and a
device item can never approach DynamoDB's 400 KB limit.

Learned baselines are kept as bounded per-hour reservoirs rather than raw
history — enough for a robust median and 95th percentile, bounded in size.

### Latches: why alerts do not flicker

A raw per-sample severity is far too noisy to show anyone. Every module's
severity passes through a `Latch`:

- **dwell** — a condition must persist for *N* consecutive samples before it
  escalates;
- **hysteresis** — a falling level must drop clearly *below* the band it was in
  before clearing;
- **clear dwell** — the condition must be absent for several samples before the
  alert clears.

Two refinements that came from watching it misbehave:

1. A latch stores the **reason that justified its severity**. Without that, a
   held WARNING gets captioned with whatever the latest sample said — the
   dashboard once displayed *"WARNING — node online, 5,788 samples received,
   0 gaps"*. It now says the condition is no longer present and that the alert
   has not yet cleared.
2. A module in `ERROR` or `NO_DATA` **bypasses dwell entirely**. Dwell exists to
   smooth a noisy *measurement*; a module that threw an exception is a fact.

### Alert thresholds are per module

```python
ALERT_FROM_SEVERITY = {"flood": 2, "leak": 2, "tank": 3, "demand": 2, "device": 2}
```

Flood and node health page at WATCH, because they are measurements of something
that can hurt someone or that invalidates other readings. The tank pages only at
WARNING, because its WATCH state is a *projection* — and a projection that pages
every lunchtime teaches people to ignore the system. The dashboard still shows
the WATCH state; it just does not send anyone a message about it.

## Fusion: why this is one product

Three jobs that no single module can do:

**1. Supply outlook.** Re-project tank depletion using Module D's *forecast*
demand instead of Module C's *current* draw, walking the forecast hour by hour
so an afternoon peak is not averaged away. This is the number a caretaker acts
on, and it exists only because the modules share a dashboard.

If the tank survives the whole forecast window, the outlook says exactly that
rather than extrapolating. An earlier version divided the remaining volume by
the final forecast hour's rate and reported **2,156 hours** — three months —
because that hour happened to be the middle of the night. A six-hour forecast
cannot support a three-month claim.

**2. Co-occurrence, never causation.** When high heat and a low tank coincide,
the dashboard says both are present and that this is *"a co-occurrence worth
planning around, not evidence that one caused the other."* The wording is in
the source, in a table, so it cannot drift.

**3. One inspection queue.** A caretaker has one pair of hands, so every module's
findings merge into a single ranked list:

```
priority = (severity / 4) × confidence × impact
```

where `impact` is how much a visit would change the outcome — flood 1.0, leak
0.8, tank 0.75, demand 0.4.

## The cloud path

### Why IoT Core rather than an HTTP endpoint

Each node gets its own X.509 certificate, and the IoT policy restricts a
certificate to connecting as its own thing name — so one node cannot
impersonate another, and a compromised node is revoked individually instead of
rotating a shared key across a fleet. The HTTP route still exists, because a
device that cannot do mutual TLS should still be able to report.

### Why both DynamoDB and S3

They answer different questions.

- **DynamoDB** answers *"what is happening now"* in single-digit milliseconds,
  and expires raw telemetry after 30 days via a TTL attribute, so the hot store
  cannot grow without bound or without anyone noticing.
- **S3**, fed by Firehose from the same topic rule, keeps the long history for
  tuning thresholds — at roughly a thousandth of the cost per GB, partitioned
  by date so Athena prunes instead of scanning.

### Why a Step Functions state machine for escalation

A critical alert that nobody acknowledges has to be raised again. "Notify, wait
thirty minutes, check whether a human acted, escalate" is a workflow, not a
function. In Lambda, those thirty minutes would be billed idle time and the
state of each pending alert would be invisible. In Step Functions it is a row in
the console you can point at.

### Why a scheduled sweep

Telemetry-driven evaluation can only react to a reading that *arrives*. Silence
is the one condition no incoming message will ever report, and a dashboard
showing the last known state as though it were current is worse than one showing
nothing. An EventBridge rule runs every five minutes and raises a
`device-offline-*` alert for any node that has missed several intervals.

### Where Bedrock sits, and where it does not

Amazon Bedrock writes the two-sentence plain-language summary attached to an
alert. It is given **only** the structured evidence the engine already produced,
and is told not to add facts, not to state a cause, and to keep every hedge.

It never sets a severity, never predicts a flood, never judges whether a flow
anomaly is a leak. Those come from arithmetic a person can check — because a
number a judge or a caretaker cannot audit is worth nothing in a safety-adjacent
tool. Its IAM role can invoke a model and nothing else: it reads no tables.

If Bedrock is unavailable, unconfigured, or returns something over the word
limit, a deterministic template renders the same evidence. The feature degrades
to plain English, never to nothing.

## Clocks

Three different times, and conflating any two of them caused a bug:

| | |
|---|---|
| **device timestamp** | what the node's clock said. Used to order a node's own history. |
| **received-at** | our clock when we accepted the reading. Used for *freshness*. |
| **clock skew** | the difference. Reported separately as its own condition. |

Freshness must be measured on our clock: a node with a wrong clock is still
reporting, and a node with a perfect clock can still be dead. Using the node's
timestamp made a healthy rig reporting every few seconds show up as stale.

The `Service` takes its clock as a constructor argument. Under an accelerated
simulation the rig's clock advances faster than wall time *by design*, so the
local server hands the service the simulated clock — keeping one honest code
path instead of adding a "simulation mode" branch.

## Decisions that were wrong first

Kept here because the reasoning is more useful than the conclusion, and every
one is covered by a regression test named for the symptom.

| Symptom | Cause | Fix |
|---|---|---|
| Flood never detected | runoff computed as m³/s and used as L/s — 1000× too small | mm × m² *is* litres; no conversion needed |
| 78 false alarms on a quiet site | absolute heat threshold fired every hot afternoon | require a departure from this site's own normal daily peak, as IMD does |
| "Tank is low at 85%" | a short ETA at a demand peak drove severity | a projection cannot outrank the measurement behind it |
| 1,466 L/h draw on a full tank | 0.3 cm of level noise ≈ 8 L, differenced over 15 s | prefer the flow meter; require span, samples and significance from the level trend |
| Tank "refilling" every other sample | same noise, differenced across consecutive samples | detect refills from a sustained, significant rise over 5 minutes |
| "Water is present at 0.0 cm, rainfall 9 mm/h" | rain alone could raise a watch | rainfall corroborates; it needs measurable water first |
| Tank module crashed on every sample of a 14 h run, silently | the pipeline swallowed module exceptions into an INFO result | errors surface on `IngestResult`, fail the eval run, and bypass dwell |
| "No climate sensor on this site" | one dropped DHT22 sample removed the whole panel | a module a node has produced before reports `NO_DATA`, not absence |
| Demand model trained on 2.5 hours | 600 training rows at a 15 s cadence | aggregate to one row per completed local hour — 720 rows is 30 days |
| Regularisation made a coefficient *larger* | ridge penalised the intercept | leave the bias column unpenalised |
| "Relative error 277%" | divided by a volume approaching zero | report as a percentage of tank capacity, as level sensors are specified |
| Stopped turbine read 0.03 L/min | noise applied after the cut-in check | a turbine below cut-in emits no pulses; the reading is exactly zero |
| 2,398 L/h draw in the live demo | warmup and live run shared device state, interleaving two timelines | carry the learned baselines across; reset the timeline |
| Every page reported "clock out by 362 days" | the simulator's epoch was a fixed date in the past | fixed epoch for reproducible evaluation; anchored to now for live runs |

Most of these were found by the evaluation harness, not by a human reading the
code — which is the argument for building the harness before the user interface.
