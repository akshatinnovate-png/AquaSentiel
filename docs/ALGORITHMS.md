# The detection algorithms

Every decision traces back to arithmetic in `engine/stats.py`. Nothing here
needs a GPU, a framework, or a trust-me.

## Module A — Flood (`engine/flood.py`)

**Smooth.** A causal median filter over 5 samples. Causal matters: a centred
filter would let the system see the future and understate the measured
detection delay.

**Rate of rise.** Least squares over a 180 s window, reported with its standard
error and r². The fit quality decides whether the trend is believed at all
(`min_r2_for_trend = 0.5`), so noise on a still channel is not a "rise".

**Severity** is the maximum of three assessments — depth against surveyed
thresholds, rate of rise, and rainfall intensity — with one combination rule:
water that is both deep *and* still rising escalates one level, because those
two together are worse than either alone.

Rainfall is **corroborating only**. It cannot raise anything above WATCH, and
it requires measurable water first. Heavy rain with a dry channel means the
drain is coping, which is good news; reporting it as a watch produced the
nonsense line *"water is present at 0.0 cm, rainfall 9 mm/h"*.

**Projection.** Time to the surveyed danger depth at the current rate:

```
eta_min = (danger_cm − depth_cm) / rate_cm_per_min
```

labelled `projected`, with the assumption stated in its own metadata.

Measured: **0.8 min** detection delay, 0 false alarms (n = 1,082).

## Module B — Leak (`engine/leak.py`)

We use what water utilities use rather than inventing something: **minimum
night flow**. During hours when a site should be idle, the lowest sustained
flow it still draws is the best available proxy for loss, because legitimate
demand goes to zero and a leak does not.

Three detectors, weighted by how *specific* each is to true loss:

| Detector | Weight | Fires when |
|---|---|---|
| minimum night flow | 0.55 | the lowest flow in 30 min during an idle hour exceeds the threshold |
| continuous flow | 0.30 | flow has not returned to zero for 45 min **through an idle hour** |
| baseline excess | 0.25 | flow stays above 2.5× the learned p95 for this hour-of-week for 20 min |

The idle-hour gate on continuous flow matters: a campus draws water
continuously from morning to evening, and an ungated version fired for most of
every school day — it once reported *721 minutes* of continuous flow as
evidence of a leak.

**Learning normal.** Flow is banked per hour-of-week into bounded reservoirs
and summarised as a median and p95. Samples the engine already suspects are
**excluded**, so a leak running for a week cannot quietly become the new normal.

**Confidence is capped by history.** With one day of data the system does not
claim to know what normal looks like.

**Volume lost** integrates `flow − baseline_p50` over the event, labelled
`projected`.

The alert says **"possible water loss"** and lists which detectors fired. A
flow meter cannot tell a burst pipe from a tap left running.

Measured: **30.5 min** to detect a 0.6 L/min overnight leak, 0 false alarms
(n = 4,802).

## Module C — Supply (`engine/tank.py`)

**Level → volume.** Three mappings: vertical cylinder, rectangular, or — for
the real-world irregular case — a measured **dip chart** interpolated
piecewise-linearly. A sensor reading only becomes litres once you know the tank.

**Consumption rate**, from two estimators, preferring the better one:

1. **Flow meter** — integrate measured outflow. Direct, immune to level noise.
2. **Level trend** — least squares on the volume series. The only option with
   no meter fitted, and dangerous: on a 5,000 L / 180 cm tank, one centimetre
   of ultrasonic noise is ~28 L, so a short window can manufacture a draw of
   over 1,000 L/h out of nothing. The level trend is only trusted once it spans
   ≥ 900 s, has ≥ 10 samples, and produces a slope exceeding 1.96 × its own
   standard error.

Refills are excluded either way — a rising level would read as negative
consumption and produce a nonsense ETA. A refill itself is detected from a
*sustained, significant* rise over 5 minutes, not from a single sample pair.

**Depletion ETA** carries a range, propagated from the slope's standard error:

```
eta_h = remaining_l / rate_lph,  range from rate ± 1.96 × stderr
```

**A projection cannot outrank the measurement behind it.** Unless the level is
itself getting low, a short ETA during a demand peak is capped at WATCH: the
peak passes and the refill arrives.

Measured: volume MAE **0.13–0.21% of capacity**; depletion-ETA median error
**0.37 h — 12.8% of the true time remaining** (n = 1,284) on the depletion
scenario, rising to 32.5% on the heatwave scenario (n = 1,547) where demand
varies far more within the day. Both are in `eval/results/REPORT.md`.

## Module D — Heat and demand (`engine/demand.py`)

**Heat index** via the published NOAA/Rothfusz regression, computed in
Fahrenheit (as published, so it is checkable against NWS tables) with both
standard adjustments. Verified in the test suite: 90 °F/70% → 106 °F,
104 °F/40% → 119 °F.

**Two gates, both required.** An absolute threshold alone is a bad alarm in a
hot climate — an October afternoon in much of India sits in the high 30s and
nobody needs telling. So, as IMD does, we also require a **departure from
normal**:

- absolute heat index above a local advisory band (41 / 45 / 50 °C, aligned
  with the NWS categories), **and**
- today's peak above the median of previous days' peaks by ≥ 2 °C.

Comparing an instantaneous reading against its own clock-hour does not work: at
11:00 the temperature is climbing steeply, so a sample late in the hour sits
2–3 °C above the hour's median and every ordinary morning looks anomalous. IMD
compares the day's *maximum* against the normal maximum, so we do too.

With no previous days to compare against, the system deliberately does **not**
alert on the advisory bands — only on a value dangerous in absolute terms
(≥ 47 °C). "I have no history" is not the same as "this is anomalous".

Adding this gate took the quiet-site false-alarm count from **32 to 0**.

### The forecast, and the gate it has to pass

- **Baseline (seasonal-naive):** the median litres this site used at this hour
  of the week. Hard to beat, and the thing to beat.
- **Candidate (ridge regression):** closed-form, 8 features, solved by Gaussian
  elimination in pure Python. The intercept is left unpenalised — penalising it
  pulls the fit toward predicting zero at the origin and the other coefficients
  grow to compensate.

The heat features are **interactions**, not an additive term:

```
[1, baseline_l, hi_anom, hi_anom × baseline/100, hi_anom × daytime,
 sin(2πh/24), cos(2πh/24), is_weekend]
```

because people use more water in the heat *during the day*, and the effect
scales with how much they were using anyway. This is not a guess: a first
version used a bare `hi_anom` term and **lost** to the baseline over fourteen
days (32.7 vs 30.0 L/h held-out MAE). The measurement said the term was the
wrong shape, and the interaction terms were added in response.

**The adoption gate.** Both models are scored on a held-out tail of the record.
The weather-aware model is used only if it beats the baseline by at least 5% of
its MAE — otherwise the baseline is shown, and the dashboard says so.

| History | Baseline MAE | Model MAE | Outcome |
|---|---|---|---|
| 30 h (39 / 14) | 118.46 L/h | 120.54 L/h | baseline kept |
| 36 h (44 / 15) | 13.88 L/h | 18.12 L/h | baseline kept |
| 14 days (269 / 90) | 29.99 L/h | **23.85 L/h** | **model adopted, −20.4%** |

Two of the three runs keep the baseline. That is the gate working, not the
model failing: with a day or two of history there is roughly one observation
per hour-of-week bucket, and nothing for either model to learn.

Training rows are **one per completed local hour**, not one per sample. An
earlier version kept the last 600 samples, which at a 15 s cadence is two and a
half hours — it could not have learned a daily shape if it tried.

## Node health (`engine/pipeline.py`)

- **Implausible values** rejected at the trust boundary against declared
  physical ranges, flagged rather than dropped so a faulty sensor is visible.
  A *fault*, though, is a rate: four rejections of the same metric within ten
  minutes, not a single dropped sample. A DHT22 drops roughly one reading in a
  hundred, and treating each as a fault produced an alert history made almost
  entirely of node-health entries that cleared seconds later.
- **Stale readings** — freshness on the server's clock, at read time.
- **Clock skew** — reported separately from staleness.
- **Transmission gaps** — counted, and back-filled records flagged `buffered`.
- **Frozen sensors** — the dangerous failure, because a sensor stuck at a
  plausible value keeps reporting and nothing looks wrong. Detected by variance
  collapsing to *exactly* zero over 10 minutes: a working sensor always jitters
  at its own resolution. A genuinely steady zero (a dry channel, a closed tap)
  is exempt.
