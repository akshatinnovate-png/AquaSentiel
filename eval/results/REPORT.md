# AquaSentinel -- measured results

Generated 2026-10-11T09:03:04Z by `eval/run_eval.py` against engine v1.0.0.

Every figure below comes from driving the simulated rig through the real detection
engine and comparing against the simulator's ground truth. Reproduce with `make eval`.

## Detection delay and false alarms

| Scenario | Module | Detected | Delay | False-alarm episodes | Samples |
|---|---|---|---|---|---|
| baseline | _(none expected)_ | - | - | **0** | 11522 |
| flood | flood | yes | 0.8 min | 0 | 1082 |
| leak | leak | yes | 30.5 min | 0 | 4802 |
| depletion | tank | yes | 52.2 min | 0 | 6722 |
| heatwave | demand | yes | 1.0 min | 0 | 17282 |
| heatwave | tank | yes | 48.2 min | 0 | 17282 |
| outage | flood | yes | 8.3 min | 0 | 722 |
| sensor_fault | device | yes | 0.5 min | 0 | 542 |
| longhaul | demand | yes | 4.0 min | 0 | 40322 |
| demo | demand | yes | 2.0 min | 0 | 7202 |
| demo | flood | yes | 2.5 min | 0 | 7202 |
| demo | leak | yes | 31.0 min | 0 | 7202 |
| demo | tank | yes | 39.5 min | 0 | 7202 |

## Estimate accuracy

- **baseline** tank volume: MAE 8.2 L (0.164% of the 5000 L capacity), worst 345.7 L (6.915%), n=5761.
- **baseline** depletion ETA: the tank did not run dry within the scenario, so no ETA could be scored.
- **flood** tank volume: MAE 10.5 L (0.21% of the 5000 L capacity), worst 341.6 L (6.831%), n=541.
- **leak** tank volume: MAE 9.0 L (0.18% of the 5000 L capacity), worst 343.4 L (6.868%), n=2401.
- **leak** depletion ETA: the tank did not run dry within the scenario, so no ETA could be scored.
- **depletion** tank volume: MAE 6.6 L (0.133% of the 5000 L capacity), worst 344.4 L (6.888%), n=3361.
- **depletion** depletion ETA: median error 0.37 h (12.8% of the true time remaining), MAE 2.49 h, 90th percentile 8.93 h, n=1284, scored when reported draw >= 50 L/h (the tank truly ran dry at 5.68 h).
- **heatwave** tank volume: MAE 6.7 L (0.134% of the 5000 L capacity), worst 348.6 L (6.971%), n=8641.
- **heatwave** depletion ETA: median error 0.73 h (32.5% of the true time remaining), MAE 3.93 h, 90th percentile 10.28 h, n=1547, scored when reported draw >= 50 L/h (the tank truly ran dry at 13.07 h).
- **outage** tank volume: MAE 9.2 L (0.183% of the 5000 L capacity), worst 341.6 L (6.831%), n=361.
- **sensor_fault** tank volume: MAE 9.1 L (0.183% of the 5000 L capacity), worst 325.0 L (6.501%), n=181.
- **longhaul** tank volume: MAE 8.3 L (0.166% of the 5000 L capacity), worst 350.5 L (7.01%), n=20161.
- **longhaul** depletion ETA: the tank did not run dry within the scenario, so no ETA could be scored.
- **demo** tank volume: MAE 7.1 L (0.143% of the 5000 L capacity), worst 343.2 L (6.863%), n=3601.
- **demo** depletion ETA: median error 2.38 h (68.1% of the true time remaining), MAE 11.56 h, 90th percentile 33.84 h, n=776, scored when reported draw >= 50 L/h (the tank truly ran dry at 16.91 h).

## Demand forecast vs baseline

- **baseline**: baseline MAE None L/h vs model MAE None L/h (train n=0, holdout n=0). Adopted: **False**. Not enough history to test a weather-aware model (23/48 samples). Showing the baseline.
- **flood**: baseline MAE None L/h vs model MAE None L/h (train n=0, holdout n=0). Adopted: **False**. Not enough history to test a weather-aware model (25/48 samples). Showing the baseline.
- **leak**: baseline MAE None L/h vs model MAE None L/h (train n=0, holdout n=0). Adopted: **False**. Not enough history to test a weather-aware model (33/48 samples). Showing the baseline.
- **depletion**: baseline MAE None L/h vs model MAE None L/h (train n=0, holdout n=0). Adopted: **False**. Not enough history to test a weather-aware model (37/48 samples). Showing the baseline.
- **heatwave**: baseline MAE 13.878 L/h vs model MAE 18.117 L/h (train n=44, holdout n=15). Adopted: **False**. Baseline retained: the weather-aware model did not beat it on held-out data (18.12 vs 13.88 L/h).
- **outage**: baseline MAE None L/h vs model MAE None L/h (train n=0, holdout n=0). Adopted: **False**. Not enough history to test a weather-aware model (24/48 samples). Showing the baseline.
- **sensor_fault**: baseline MAE None L/h vs model MAE None L/h (train n=0, holdout n=0). Adopted: **False**. Not enough history to test a weather-aware model (24/48 samples). Showing the baseline.
- **longhaul**: baseline MAE 29.985 L/h vs model MAE 23.854 L/h (train n=269, holdout n=90). Adopted: **True**. Weather-aware model adopted: held-out MAE 23.85 L/h vs baseline 29.99 L/h.
- **demo**: baseline MAE 118.455 L/h vs model MAE 120.542 L/h (train n=39, holdout n=14). Adopted: **False**. Baseline retained: the weather-aware model did not beat it on held-out data (120.54 vs 118.45 L/h).

## Robustness

- **baseline**: 53 implausible readings rejected.
- **flood**: 8 implausible readings rejected.
- **leak**: 26 implausible readings rejected.
- **depletion**: 36 implausible readings rejected.
- **heatwave**: 86 implausible readings rejected.
- **outage**: 6 implausible readings rejected; 144 buffered records replayed after the outage.
- **sensor_fault**: 96 implausible readings rejected; 48.1% of faulted samples flagged as degraded (87/181).
- **longhaul**: 196 implausible readings rejected.
- **demo**: 34 implausible readings rejected.

## What each scenario entitles us to claim

- **baseline**: Any alert raised here is a false alarm, and is counted as one.
- **flood**: Detection of rising water at an instrumented point, with a measured delay. Not a flood forecast.
- **leak**: Detection of sustained unexplained flow. The alert says 'possible loss' and must be verified.
- **depletion**: Calibrated volume and a depletion ETA with a stated range. Accuracy is measured, not claimed.
- **heatwave**: Heat index against a local advisory level, plus a demand forecast scored against a baseline.
- **outage**: No readings are lost across an outage, and replayed records are labelled as back-filled.
- **sensor_fault**: A failed sensor is reported as unavailable. It is never silently treated as a zero reading.
- **longhaul**: A forecast comparison with enough history to mean something: a seasonal-naive baseline against a weather-aware ridge model, scored on held-out hours.
- **demo**: All four modules on one real timeline, each in the regime it was designed for. Playback is sped up for the video; the physics and the detection logic are not.
