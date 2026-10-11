# Testing and measurement

Two separate things, and the distinction matters:

- **`make test`** — 104 unit and integration tests. Does the code do what it says?
- **`make eval`** — drives the real engine over simulated scenarios and scores
  it against the simulator's own ground truth. Does the *detector* work, and
  how well?

The second is what produced every number in the README.

## Reproducing the published figures

```bash
make eval
cat eval/results/REPORT.md
```

The harness pins a **fixed reference date**, so a run today and a run next
month produce identical numbers. The whole suite takes under a minute.

## What is scored, and how

| Measure | Definition |
|---|---|
| **Detection delay** | Simulated seconds from the moment a condition *genuinely* begins to the first alert at or above the expected severity. |
| **Missed detection** | A truth window that produced no qualifying alert. |
| **False alarm** | An alert episode raised while no such condition was present — counted per episode, not per sample. |
| **Early alert** | An alert raised while a looser *precursor* condition held. Counted separately: neither a detection nor a false alarm. |
| **Volume error** | Reported litres against the simulator's true volume, as a percentage of **tank capacity** (how level sensors are specified). |
| **ETA error** | Reported hours-to-empty against how long the tank truly had left — scored **only while a meaningful draw exists**, because the estimate explicitly assumes the current rate continues. |
| **Forecast skill** | Held-out MAE of the weather-aware model against the seasonal-naive baseline it must beat. |

A figure this harness will never produce is an accuracy percentage with no
denominator. Every number carries its sample count.

## Ground truth comes from the simulator, not from a guess

Two kinds of truth window:

- **Injected** — a condition we switched on (a leak, a heat offset). We know
  exactly when it started.
- **Observed** — a `predicate` over the simulator's *internal physical state*
  (water actually ponding, a tank actually below its configured low threshold).

The predicate form matters. Hand-guessing *"ponding starts at 14 minutes"* and
then measuring the delay against that guess measures the guess. Reading the
simulator's true depth and timing the alert against the moment it genuinely
crossed the threshold measures the detector.

Predicates read the **physical model**, never the sensor output, so sensor
noise cannot move the ground truth. There is a test for exactly this:
disconnect the sensor while the water is still there, and the predicate stays
true.

### Precursors

A truth window may declare a looser `precursor`. Water at 2 cm and climbing is
a legitimate thing to warn about even though the delay-measurement window opens
at 6 cm; counting that as a false alarm would penalise the system for warning
*sooner*.

## The scenarios

| Scenario | What it is | What passing it entitles us to claim |
|---|---|---|
| `baseline` / `baseline_short` | A quiet, uneventful site with a working float-switch pump | **Any alert here is a false alarm, and is counted as one.** |
| `flood` | 35 min of heavy rain (peak 45 mm/h) onto a 75% blocked drain | Detection of rising water at an instrumented point, with a measured delay. Not a flood forecast. |
| `leak` | A 0.6 L/min leak starting in the idle hours | Detection of sustained unexplained flow. The alert says "possible loss" and must be verified. |
| `depletion` | From 06:00, a 42% tank and no refill | Calibrated volume and a depletion ETA with a stated range. |
| `heatwave` | A +7.5 °C spell from hour 12 against a 60% tank | Heat index against a local advisory level, plus a demand forecast scored against a baseline. |
| `outage` | A 12-minute uplink outage mid-event | No readings lost; replayed records labelled as back-filled. |
| `sensor_fault` | Tank sensor disconnected, then street sensor frozen | A failed sensor reads "unavailable", never zero. |
| `longhaul` | 14 days, a different heat offset each day | A forecast comparison with enough history to mean something. |
| `demo` | One continuous 30 h day: leak → no refill → heat → downpour | All four modules on one real timeline, each in the regime it was designed for. |

The quiet-site run exists so that any alert it produces counts *against* us.
It is the first thing to look at in the report.

### The warm-up is not cheating, and it is not free

Scenarios run after a `baseline_short` warm-up, carrying over **only** the
learned baselines — not the rolling series, latches or alerts. A leak detector
with no idea what normal looks like is not a detector, and pretending otherwise
would flatter the numbers. Equally, carrying the whole state interleaved two
timelines in the same buffers and made the consumption integral double-count.

## A module that crashes fails the run

An earlier version of the pipeline swallowed module exceptions into a harmless
INFO result. The tank module then failed on **every sample of a 14-hour run**
while the dashboard stayed green and the harness scored around it.

Now: exceptions surface on `IngestResult.module_errors`, the engine reports the
module as unavailable at WARNING, and `run_eval.py` **exits non-zero** if any
module errored. A crashing detector is a hard failure, not a scoring footnote.

## The test suite

```bash
make test        # 104 tests, under a second
```

| File | Covers |
|---|---|
| `tests/test_stats.py` | The arithmetic every decision rests on: robust statistics, causal filtering, slope with uncertainty, ridge, dip-chart interpolation |
| `tests/test_engine.py` | Parsing and the trust boundary, latch dynamics, serialisation round-trips, level→volume, heat index against published NWS values, **and one regression test per bug found** |
| `tests/test_integration.py` | The real path: payload → engine → store → the API shapes the dashboard consumes |
| `tests/test_sim.py` | The simulator itself — conservation of volume, runoff units, drain behaviour, turbine cut-in, firmware buffering and replay |

Every regression test is **named for the symptom a user would have seen**, so a
future failure is self-explanatory:

```
test_sensor_noise_is_not_reported_as_a_refill
test_a_projection_does_not_outrank_the_measurement
test_rain_on_a_dry_channel_is_not_an_alert
test_an_ordinary_hot_afternoon_is_not_a_heat_alert
test_a_module_crash_is_loud_not_silent
test_a_dropped_sample_does_not_remove_a_module
test_a_held_severity_carries_the_reason_that_justified_it
test_a_frozen_sensor_is_detected
test_a_genuinely_steady_zero_is_not_called_frozen
test_a_clear_drain_copes_with_heavy_rain
test_an_ordinary_day_does_not_reach_extreme_danger
test_a_replayed_reading_does_not_page_anyone
test_a_stale_node_does_not_set_the_site_severity
test_clock_skew_is_distinguished_from_staleness
```

Several also pin properties that are easy to break silently: that every signal
declares its provenance, that every module has an alert threshold, and that
alert IDs are stable across a replay so ingestion is idempotent.

## Testing with real hardware, later

The scenarios and the scoring are hardware-independent. Replacing the simulator
with a bench rig means:

1. recording ground truth by hand (a stopwatch and a measuring jug);
2. feeding readings through `--transport http` or `--transport iot`;
3. scoring with the same harness.

The figures would change — real sensors are messier than modelled ones — but
the method, and the obligation to publish the sample count, would not.
