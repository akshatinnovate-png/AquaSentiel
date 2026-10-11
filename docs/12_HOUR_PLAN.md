# The 12-hour plan

You have twelve hours, not a week, and no hardware. This is what to do with
them, in order, with the decision rule for when to abandon something.

**The governing rule:** at every hour mark you must still have something you
could record a video of. Never be more than one hour from a demo.

---

## Hours 0–1 · The engine's skeleton

Data model (`Reading` / `Signal` / `Alert`), site config, rolling device state,
robust statistics.

> **Why first:** every later decision depends on these shapes, and changing
> them at hour 8 is expensive. Keep the engine **dependency-free** — that one
> constraint is what later lets the same code run in Lambda, on a laptop, in
> tests and in the measurement harness.

**Done when:** a payload parses, an impossible value is rejected, and state
round-trips through a dict.

## Hours 1–3 · The four modules and fusion

Flood, leak, tank, demand, then the fusion layer.

> Write the *baseline* algorithm for each — a threshold, a median, a
> least-squares slope. Resist anything clever. You have no data yet to justify
> clever, and every module needs to exist before any one needs to be good.

**Done when:** one synthetic reading produces four module results.

## Hours 3–4 · The simulator

Physics-based sensors, the virtual ESP32, and the scenarios **with their ground
truth declared**.

> **Do not skip the ground truth.** A scenario that doesn't state when the
> condition genuinely began can't be scored, and an unscored detector is a
> screenshot.

**Done when:** `python -m sim.run --list` shows scenarios and `--transport
stdout` emits plausible payloads.

## Hours 4–5 · The measurement harness ← **the highest-value hour**

Drive the engine over every scenario; score detection delay, false alarms and
estimate error; write `metrics.json` and a markdown report.

> **This is the single most valuable hour of the twelve, and the one everyone
> skips.** On this build the first run found four real bugs in twenty seconds:
> a unit error that made flood detection impossible, 78 false alarms on a quiet
> site, an absurd consumption rate from sensor noise, and an alert that said
> "water is present at 0.0 cm". None of those would have been visible in a demo
> until a judge asked.
>
> Every hour after this one is cheaper because of it.

**Done when:** `make eval` prints a delay and a false-alarm count per scenario.

## Hours 5–7 · Fix what the harness found

Do not build anything new. Read the report, fix the top failure, re-run.
Repeat.

> Target: **zero missed detections and zero false alarms** across every
> scenario. If a false alarm turns out to be correct behaviour, the *scenario*
> is wrong — fix the scenario and say so.

**Done when:** `make eval` reports 0 missed, 0 false alarms, 0 module errors.

## Hours 7–8 · The backend and the local server

A repository with two implementations (memory and DynamoDB), the application
service, and a stdlib HTTP server exposing the same routes the deployed API
will.

> **Build the local server before the cloud.** It is your demo insurance: a
> version that works with no network, no credentials and no deploy. If the
> cloud goes wrong at hour 11, you still have a video to record.

**Done when:** `curl localhost:8080/overview` returns a situation report.

## Hours 8–10 · The dashboard

Six views, live polling, hand-rolled SVG charts.

> No framework, no build step. A build step at hour 9 is a liability. Spend the
> saved time on the thing that actually distinguishes this: **badging every
> number** `measured` / `derived` / `projected` / `forecast`, and showing the
> evidence behind every alert.
>
> Screenshot it in a real browser and look at it. The layout bugs you will find
> — a duplicated accent bar, two labels drawn on top of each other, "rising
> −0.09 cm/min" — are invisible from the code.

**Done when:** `make demo` shows live data and all four modules fire.

## Hours 10–11 · AWS

CDK stack, `cdk synth`, `cdk deploy`.

> **`cdk synth` early and often.** A stack that doesn't synthesise is a
> diagram. Deploy once you have synth clean, then point the simulator at the
> real endpoint over MQTT.
>
> If the deploy fights you, **stop at 11:00 sharp** and record against the
> local server. A working local demo beats a half-deployed cloud.

**Done when:** `make synth` is clean and the outputs give you a dashboard URL.

## Hours 11–12 · Record and write

Rehearse once, record once, cut. Then the README with the measured numbers in
it.

> Write the limitations section. It takes fifteen minutes and it is the
> cheapest credibility in the whole build.

---

## What to cut, in order

If you are behind, cut in exactly this order. Each cut costs the least of what
remains.

1. **Dark mode and visual polish.** Nobody scores it.
2. **Bedrock explanations.** The template fallback already reads fine.
3. **Step Functions escalation.** SNS alone still notifies.
4. **Firehose, Glue and the S3 lake.** DynamoDB alone still works.
5. **CloudFront hosting.** Serve the dashboard from the local server and show
   the API in the cloud.
6. **The demand model.** Ship the seasonal-naive baseline and say so — the
   adoption gate means that is already an honest outcome, not a failure.
7. **One whole module.** Three modules that are measured beat four that are
   claimed.

## What never to cut

- **The measurement harness.** Without it you have assertions, not results.
- **The local server.** It is the demo that cannot fail.
- **The provenance badges.** They are the cheapest differentiator you have.
- **The limitations document.** Fifteen minutes, enormous credibility.
- **A quiet-site false-alarm run.** The most convincing number in the whole
  submission is a zero with eleven thousand samples behind it.

## The traps

| Trap | What it costs | Avoid by |
|---|---|---|
| Building the UI before the harness | You tune against screenshots, not evidence | Harness at hour 4 |
| A dependency-heavy engine | Lambda packaging at hour 10 | Standard library only |
| Thresholds invented at the keyboard | Alerts nobody believes | Put every threshold in a config file and label it as calibration |
| "99% accuracy" with no denominator | The one claim a judge will test | Publish the sample count beside every figure |
| Deploying for the first time at hour 11 | No video | `cdk synth` from hour 10, deploy by 10:30 |
| Silently swallowing exceptions | A module dead for hours behind a green dashboard | Make errors fail the eval run |
