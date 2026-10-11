# Submission checklist

## Before anything else — verify the rules yourself

Nothing below substitutes for the competition's own published terms. Confirm,
from the official source, on the day:

- [ ] Your student profile is verified on AWS Builder Center.
- [ ] Current eligibility criteria and the submission deadline.
- [ ] **What exactly satisfies the AWS requirement** — a deployed service, an
      eligible open-source AWS tool, or either. This determines whether
      `make deploy` is mandatory or merely strong.
- [ ] Video length, format and hosting requirements.
- [ ] Whether the write-up must be published on Builder Center and linked.

Prize and interview opportunities described in competition material are
benefits, not guaranteed outcomes.

## The build

- [x] Four detection modules, each with a baseline algorithm
- [x] Cross-module fusion — forecast demand feeds the depletion projection
- [x] Virtual hardware: physics-based sensors + ESP32 firmware behaviour
- [x] Offline buffering and replay across a connectivity outage
- [x] Sensor-fault handling: disconnected, frozen, out-of-range
- [x] Dashboard, six views, every value badged by provenance
- [x] AWS deployment — 17 services, 100 resources, CDK, clean synth
- [x] 99 tests, including one regression test per bug found
- [x] Measurement harness with ground truth and published sample counts
- [x] Limitations documented

## Before you record

```bash
make test     # must be green
make eval     # 0 missed, 0 false alarms, 0 module errors
make synth    # clean
make demo     # the dashboard you will film
```

- [ ] `eval/results/REPORT.md` regenerated and committed
- [ ] The README's numbers match that report
- [ ] A caption on screen stating the data is simulated

## The video — the three things that must be in it

1. **A sensor reading reaching the backend.** MQTT test client, or the local
   server log. Proof it is a pipeline, not a mock-up.
2. **The supply outlook gap.** "4.5 hours at the current rate, 2.1 under
   forecast demand." This is the one number no single module can produce, and
   it is the whole argument for the product.
3. **A measured result with its sample count.** "Zero false alarms across
   11,522 samples on a quiet-site run." Specific, checkable, and the opposite
   of what most submissions claim.

Shot list: [DEMO.md](DEMO.md).

## The write-up

- [ ] Lead with the problem, not the architecture.
- [ ] State plainly that the hardware is simulated, and why that still
      demonstrates the thing being claimed.
- [ ] Separate measured / detected / predicted explicitly.
- [ ] Put the measured results table in, with sample counts.
- [ ] Include the forecast-adoption result **including the case where the
      baseline won** — it is more persuasive than only reporting the win.
- [ ] Link the limitations document. Do not bury it.

## Deploying, if the rules require it

```bash
cd infra
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
npx cdk bootstrap
npx cdk deploy -c alert_email=you@example.com
./scripts/provision_device.sh aquasentinel-node-01
pip install awsiotsdk && source .env.iot
python -m sim.run --transport iot --scenario demo --speed 60
```

- [ ] Confirm the SNS email subscription (it needs a click)
- [ ] Screenshot the IoT MQTT test client with messages arriving
- [ ] Screenshot the CloudWatch dashboard
- [ ] Note the region in the write-up
- [ ] **`make destroy` after recording** unless you need it live for judging

## The questions you will be asked

| Question | Where the answer is |
|---|---|
| "How do you know it works?" | `eval/results/REPORT.md` — delays and sample counts |
| "What's your false-positive rate?" | 0 episodes over 11,522 samples on a quiet site, and 0 across all nine scenarios and 90,198 samples; the method is in TESTING.md |
| "Is this really AI?" | Honest answer: the detectors are statistics, deliberately, because they must be auditable. There is a validated ML forecaster with an adoption gate, and Bedrock explains but never decides. |
| "Why no hardware?" | 12 hours. The simulator models the sensor behaviours the detectors must cope with; swapping in real hardware changes the transport only. |
| "What happens when a sensor fails?" | Demonstrated: `make demo SCENARIO=sensor_fault`. It reads "unavailable", never zero. |
| "Could this predict a flood?" | No, and the system says so on screen. It detects rising water at an instrumented point. |
| "What would you do next?" | The ordered list at the end of LIMITATIONS.md. |
