# Limitations

Written before anyone asks, because a prototype that states its own limits is
more trustworthy than one that waits to be caught.

## The honest summary

AquaSentinel **measures** a handful of quantities, **detects** conditions in
them against thresholds that must be surveyed per site, and **predicts** two
things over short horizons with stated error. It does not forecast floods, it
does not confirm leaks, and it does not declare heatwaves.

## No physical hardware

There is no bench rig. `sim/` is a virtual ESP32 with physics-based sensors.

**What that still demonstrates:** the detection logic, the cloud architecture,
the dashboard, the firmware behaviour (edge validation, offline buffering,
replay), and detection delays measured against a known ground truth.

**What it does not:** real sensor drift over weeks, electrical noise from a
nearby pump, condensation on an ultrasonic face, insects in a tipping bucket,
the actual mounting problem. Those are discovered on a roof, not in software.

**How close the models are.** Noise, resolution, dropout, outlier spikes and
turbine cut-in are modelled on datasheet behaviour for HC-SR04-class ultrasonic,
YF-S201-class flow and DHT22-class climate sensors. They are honest
approximations and not measurements of specific units.

## Per module

### Flood
- Detects rising water **at one instrumented point**. Says nothing about the
  next street.
- The watch/warn/danger depths are **configuration**. The defaults describe a
  demonstration rig. They are not public-safety limits and must be surveyed for
  any real location.
- The time-to-danger projection assumes the current rate of rise continues.
  Rain stops; drains unblock; the projection does not know that.
- Rainfall corroborates but never raises severity alone — which also means a
  rapid rise with a *failed* rain gauge is still detected, but with less
  supporting evidence.
- A single sensor has no redundancy. One frozen ultrasonic is one blind
  location. The design calls for a second sensor at a different height; it is
  not implemented.

### Leak
- **A flow anomaly is not a leak.** A burst pipe, a tap left running, a filling
  cistern and a genuine change in usage are identical to a flow meter. The
  alert says "possible loss" and lists the evidence so one visit can settle it.
- Minimum night flow requires the site to *have* idle hours. A 24-hour facility
  would need a different detector.
- The learned baseline needs history. Confidence is capped accordingly, and
  with a day of data the system does not claim to know what normal looks like.
- A leak present from the very first day gets learned as normal. Nothing here
  solves that; it needs an independent reference, such as comparing an inlet
  meter against the sum of outlets.
- Turbine meters under-read at very low flow — exactly the regime small leaks
  live in. Modelled, not solved.

### Supply
- Volume accuracy depends entirely on calibration. A wrong tank height or an
  un-surveyed irregular shape makes every litre figure wrong, confidently.
- The depletion ETA assumes consumption continues at the current rate. Reported
  with a range from the trend's own uncertainty, but the range does not cover
  "everyone goes home at 4pm".
- A float-switch refill that happens between samples can be missed.
- Measured error is against a *simulated* tank. A real tank adds sloshing,
  temperature-dependent speed of sound, and sensor mounting error.

### Heat and demand
- **Not a heatwave declaration.** The bands are local advisory levels aligned
  with NOAA categories. Only the IMD can declare a heat event in India, and any
  public warning should come from them.
- The anomaly gate needs at least one previous day. With none, the system
  deliberately alerts only on a value dangerous in absolute terms.
- The forecast assumes today's heat anomaly persists across the horizon,
  because there is no external weather feed wired in. Stated in the signal's
  own metadata.
- The weather-aware model beat the baseline on **one 14-day simulated run**.
  That is one experiment, on synthetic data, at one site. It is not evidence
  that it would beat a baseline at a real site, and the adoption gate exists
  precisely so that it is re-tested per device rather than assumed.
- No external forecast feed, no rainfall forecast, no humidity forecast.

## System-level

- **Single site.** Multi-tenant isolation, per-site auth and an operator
  console are not built.
- **No authentication on the dashboard.** The CDK stack puts CloudFront in
  front of a private bucket, but the API is open. A real deployment needs
  Cognito or an API key at minimum. This is the largest gap between prototype
  and product.
- **No alert routing or rosters.** SNS fans out to whoever subscribed. There is
  no on-call schedule, no per-module routing, no SMS fallback ordering.
- **Escalation is one step.** Notify, wait 30 minutes, escalate once.
- **Firmware is modelled, not written.** No C++, no power management, no OTA
  update path, no watchdog. A solar-powered node needs all four.
- **Thresholds are global per site**, not per node. Two low points with
  different danger depths would need the per-device override that the config
  shape allows but the code does not yet read.
- **The engine assumes one node reports a given metric.** Two tank sensors on
  one site would not be reconciled.

## What would change these

Roughly in order of how much they would improve the system per unit effort:

1. **A real rig for two weeks.** Everything in the hardware section above.
2. **Authentication.** The honest blocker to anyone else using this.
3. **An inlet-versus-outlet meter pair.** Turns "possible loss" into a defensible
   water balance, and solves the leak-present-from-day-one problem.
4. **A real weather feed.** Removes the persistence assumption from the forecast.
5. **Per-node thresholds and a calibration UI.** Today calibration is a JSON
   POST, which is a barrier to the exact people the system is for.
6. **A second sensor at each critical point.** Redundancy for the blind-location
   failure mode.
