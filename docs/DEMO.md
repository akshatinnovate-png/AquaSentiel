# The three-minute video, shot by shot

The judges will not see a live demo, so this has to be recorded. Budget about
90 minutes: 30 to rehearse, 30 to record, 30 to cut.

## Before you record

```bash
make test        # must be green
make eval        # regenerates eval/results/REPORT.md
make demo        # the dashboard you will film
```

Open `http://127.0.0.1:8080`, make the browser window 1440×900, and hide your
bookmarks bar. Record the screen at 1080p.

Have these ready in separate tabs:
1. the dashboard
2. `eval/results/REPORT.md` rendered
3. the AWS console — IoT Core → MQTT test client, subscribed to
   `aquasentinel/telemetry/#`
4. the AWS console — DynamoDB → the readings table
5. the CloudFront dashboard URL (the deployed copy)

**Say out loud, on camera, that the data is simulated.** Put it on screen as a
caption too. You lose nothing by it — the physics and the detection logic are
real, and only the sensors are modelled — and a judge who discovers it
themselves will discount everything else you said.

---

## 0:00 – 0:30 · The problem

> "A school caretaker already knows the tank is low. What they don't know is
> whether it's low because of the heat, or because something is leaking. Those
> are different problems with different fixes, and today they're watched by
> different people, on different days, if at all."

**On screen:** the Overview page, all four tiles green.

Point at the four tiles: water at the low point, line flow, stored supply, heat
index. One screen, four risks, one network.

---

## 0:30 – 1:15 · The hardware, and what replaced it

> "There's no bench rig here, and I want to be straight about that. Instead
> there's a virtual ESP32 with physics-based sensors."

**On screen:** split — `sim/sensors.py` on one side, the dashboard on the other.

Show, briefly:

- the tank integrating net flow, so the level responds as a real tank does;
- the street channel filling from runoff and draining through an orifice at
  `√h` — fast rise, slow tail;
- the flow meter quantising to whole turbine pulses and reading **exactly
  zero** below its cut-in speed.

> "The firmware model does what firmware does: it rejects impossible values at
> the edge, buffers across an outage and replays oldest-first with a flag. When
> real hardware arrives, one thing changes — the transport. It already speaks
> MQTT to IoT Core with its own certificate."

---

## 1:15 – 2:15 · The intelligence

This is the heart of it. Let the demo scenario run and narrate each alert as it
fires. Do not rush — show the *evidence*, not just the colour.

**Leak (around 00:40 in the scenario).**
> "Minimum night flow. This is what water utilities actually use: during the
> hours a site should be idle, the lowest flow it still draws is the best proxy
> for loss, because legitimate demand goes to zero and a leak doesn't."

Expand **Show the evidence behind this**. Point at the signal, the value, the
threshold and the note.

> "And notice what it says: *possible* water loss. A flow meter cannot tell a
> burst pipe from a tap someone left running. So it lists which detectors fired
> and lets one visit settle it."

**Heat (around 13:00).**
> "Heat index 45, which is the NOAA calculation from temperature and humidity —
> not the thermometer alone. But the absolute number isn't what triggers this.
> It also has to be above this site's own normal daily peak, the way IMD defines
> a heat event. Adding that second gate took the false-alarm count on a quiet
> day from 32 to zero."

**Supply — and this is the money shot.**

Go to **Tanks**. Point at the three tiles side by side:

> "Time to empty at the current rate: 4.5 hours. Under forecast demand: 2.1.
> That gap is the whole product. Neither module can produce that number alone —
> the heat model's forecast is feeding the tank's projection. For a caretaker
> that's the difference between 'refill tomorrow' and 'refill now'."

**Flood (around 17:00).**
> "Rising water at the north gate. Rate of rise from a least-squares fit, with
> the r² shown, so you can see whether the trend is real or just noise. And it
> projects when the surveyed danger depth is reached — at the current rate,
> which it says on the tile."

Then, deliberately:

> "What it is *not* doing is forecasting a flood. That needs drainage topology
> and catchment data I don't have. This detects rising water at one instrumented
> point, and says so."

Point at a `PROJECTED` badge.

> "Every number on here is badged: measured, derived, projected or forecast. An
> estimate can't dress up as a reading."

---

## 2:15 – 3:00 · Proof that it runs

**The cloud path (15 s).** Switch to the AWS console, MQTT test client, live
messages arriving on `aquasentinel/telemetry/#`. Then DynamoDB with the same
reading stored. Then the CloudFront URL — the same dashboard, deployed.

> "Seventeen AWS services, a hundred resources, all in CDK. IoT Core with
> per-device certificates, Lambda, DynamoDB, Firehose to an S3 lake, Step
> Functions for escalation, Bedrock for the plain-language summary."

**The measurements (25 s).** Switch to **Devices & history**.

> "And here's what I can actually prove. Flood detected in 0.8 minutes. A
> 0.6 litre-a-minute overnight leak in 30 minutes. Tank volume to within
> 0.2% of capacity. Zero false alarms on a 24-hour quiet-site run — eleven and
> a half thousand samples."

> "The demand model is only used when it beats a seasonal-naive baseline on
> held-out data. On two days of history it doesn't, and the dashboard says so.
> On fourteen days it cuts the error by a fifth. That gate is in the code."

**Close (10 s).**
> "Four risks, one network. It tells a caretaker what happened, why it thinks
> so, and what to check first — and it's honest about which of those is a
> measurement and which is an estimate."

---

## If you have to cut

In this order:

1. The `sim/sensors.py` code walk-through (describe it instead).
2. The DynamoDB console shot (the MQTT client already proves the cloud path).
3. The flood module (it is the most visually obvious, but supply + demand is
   the stronger *story*, because it is the one neither module can tell alone).

**Never cut:** the supply-outlook gap (2.15), the evidence expansion, or the
measured results. Those are what separate this from a dashboard mock-up.

## Common ways this goes wrong

| Problem | Fix |
|---|---|
| Nothing has fired yet | Lower `SPEED`: `make demo SPEED=1200`. The demo scenario is 30 simulated hours. |
| Everything is already critical | Restart. `make demo` begins at 18:00 with a 48% tank. |
| Only want one module | `make demo SCENARIO=leak` (or `flood`, `depletion`, `heatwave`). |
| You want to show an outage | `make demo SCENARIO=outage` — the node buffers and replays. |
| Dashboard shows "cannot reach the API" | The server isn't up. `make run` in another terminal. |
| You want a quiet site to contrast | `make demo SCENARIO=baseline SPEED=3600` — nothing should fire. That *is* the demo. |
