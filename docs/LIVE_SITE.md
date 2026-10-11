# The live site

**<https://akshatinnovate-png.github.io/AquaSentiel/>**

Published by GitHub Actions on every push. There is no server behind it —
GitHub Pages serves static files only — so the dashboard carries its own data
in two independent ways, and you can switch between them in the bar under the
tabs.

## 1. Recorded run (the default)

Every scenario is driven through the **real detection engine** during the
build, and the resulting timeline is baked into the site as JSON. The page
replays it: charts fill, alerts fire, the inspection queue reorders, exactly
as they would against a live backend.

This is a recording of the real thing, not a reconstruction. Each situation
report came out of the same `Engine.ingest` a deployed Lambda calls. What you
cannot do is change anything — it is a video.

It loads in well under a second (40–140 KB gzipped per scenario) and needs
nothing but static files, which is why it is the default.

## 2. Live engine

Click **Live engine** and the page loads CPython compiled to WebAssembly and
runs `engine/` and `sim/` *unmodified*, in your browser. No server, no API.

That is only possible because the engine imports nothing outside the standard
library. There are no wheels to resolve, no compiled extensions, no
`micropip` — Pyodide unpacks a 61 KB zip of the source and imports it. The
dependency-free constraint was chosen so the same code could run in Lambda, on
a laptop and in the tests; running in a browser tab is the same property
paying off a fourth time.

Once it is up, the **Thresholds** sliders on the *Live engine* tab are live.
Move one, press *Apply*, and real detection re-runs from that moment with the
new settings. Three things worth trying are listed on that tab; the most
instructive is setting **Departure from normal** to 0, which removes the
IMD-style second gate and reproduces the naive absolute-threshold detector —
the change that took the measured false-alarm count from 32 to zero, run
backwards.

### Why the runtime is self-hosted

The Pyodide runtime (~13 MB) is downloaded during the build and served from
the site itself rather than from a public CDN. A CDN is one more thing that
can be blocked, throttled or down on the morning someone is judging this, and
this is the headline feature. Self-hosting also means the site works on a
locked-down network and offline once cached.

If a build cannot fetch the runtime, the site still publishes: the live-engine
button then falls back to the CDN, and if that fails too the page stays on the
recorded run and says why. A failed upgrade is never a broken page.

## 3. A real backend

If you deploy the CDK stack, it writes a `config.js` into its own dashboard
bucket pointing at the API Gateway endpoint. The same `index.html` then talks
to real DynamoDB-backed data instead. Nothing else changes — the three sources
produce the same shapes, so there is one rendering path and no "demo mode"
branch anywhere in the UI.

## The workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | every push and PR | Tests, then **re-measures the detectors** — `eval/run_eval.py` exits non-zero on a missed detection, a false alarm or a crashed module. Then checks the README's published figures still match the metrics it just produced, builds the site, and synthesises the CDK stack. |
| `pages.yml` | pushes touching the engine, simulator, web assets or the builder | Re-runs the measurement, builds the site, vendors the runtime, deploys to Pages. |

CI fails on a quietly degraded detector, which is the point: the numbers in the
README are a build artifact, not a claim someone typed.

`pages.yml` passes `enablement: true` to `actions/configure-pages`, so the
first successful run turns Pages on for the repository. If your organisation
restricts that, enable it once by hand at **Settings → Pages → Source: GitHub
Actions** and re-run the workflow.

## Building it locally

```bash
make site       # build into site/
make site-serve # build and serve on http://127.0.0.1:8000
```

The first build downloads the runtime and caches it in `.cache/`. Pass
`--no-pyodide` to skip that and build only the recorded runs:

```bash
python scripts/build_site.py --out site --no-pyodide
```

## What the site does *not* do

- **Acknowledging an alert is local.** A recording has no server to tell, so
  the acknowledgement lives in the tab and is gone on reload. Against a real
  backend it persists.
- **Threshold changes need the live engine.** In the recording the sliders are
  read-only, and the page says so rather than pretending.
- **Scenarios are simulated.** The physics is modelled and the detection is
  real. The site says this on the calibration banner, and
  [LIMITATIONS.md](LIMITATIONS.md) says it at length.
