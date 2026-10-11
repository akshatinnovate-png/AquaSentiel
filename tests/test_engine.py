"""Engine behaviour, including a regression test for every bug the
evaluation harness caught during development.

The regression tests are named for the symptom rather than the function, so a
future failure says what a user would have seen.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import ALERT_FROM_SEVERITY, DEFAULT_SITE, SiteConfig
from engine.demand import heat_index_c
from engine.model import PLAUSIBLE_RANGE, Quality, Reading, Severity
from engine.pipeline import Engine
from engine.state import Buckets, DeviceState, Latch, Series
from engine.tank import depth_from_distance, level_to_litres

T0 = 1760000000.0


def cfg_for_tests() -> SiteConfig:
    c = SiteConfig.from_dict(DEFAULT_SITE.as_dict())
    c.calibrated = True
    return c


def feed(engine, state, n, *, start=T0, step=15.0, **metrics):
    """Push n identical-ish readings and return the last result."""
    res = None
    for i in range(n):
        ts = start + i * step
        res = engine.ingest(
            {"device_id": state.device_id, "timestamp": ts,
             "readings": {k: (v(i) if callable(v) else v) for k, v in metrics.items()},
             "seq": i + 1},
            state, now=ts,
        )
    return res


class TestReadingParsing(unittest.TestCase):
    def test_accepts_epoch_millis_and_iso(self):
        a = Reading.from_payload({"device_id": "n", "timestamp": "2026-10-10T10:30:00Z",
                                  "readings": {"flow_lpm": 1.0}})
        b = Reading.from_payload({"device_id": "n", "timestamp": a.ts * 1000,
                                  "readings": {"flow_lpm": 1.0}})
        self.assertAlmostEqual(a.ts, b.ts, places=1)

    def test_rejects_physically_impossible_values(self):
        r = Reading.from_payload({"device_id": "n", "timestamp": T0,
                                  "readings": {"temperature_c": 999, "humidity_pct": 50}})
        self.assertIsNone(r.get("temperature_c"), "an impossible value must not reach the engine")
        self.assertEqual(r.quality["temperature_c"], int(Quality.IMPLAUSIBLE))
        self.assertEqual(r.get("humidity_pct"), 50)

    def test_rejects_nan_and_infinity(self):
        for bad in (float("nan"), float("inf"), "not-a-number"):
            r = Reading.from_payload({"device_id": "n", "timestamp": T0,
                                      "readings": {"flow_lpm": bad}})
            self.assertIsNone(r.get("flow_lpm"), f"{bad!r} must be rejected")

    def test_every_metric_has_a_declared_plausible_range(self):
        from engine.model import METRICS

        for name in METRICS:
            self.assertIn(name, PLAUSIBLE_RANGE)


class TestLatch(unittest.TestCase):
    def test_dwell_prevents_a_single_sample_from_firing(self):
        latch = Latch()
        sev, escalated, _ = latch.update(3, T0, dwell=3)
        self.assertFalse(escalated)
        latch.update(3, T0, dwell=3)
        sev, escalated, _ = latch.update(3, T0, dwell=3)
        self.assertTrue(escalated)
        self.assertEqual(sev, 3)

    def test_clearing_requires_sustained_absence(self):
        latch = Latch()
        for _ in range(3):
            latch.update(3, T0, dwell=1)
        for _ in range(2):
            _, _, cleared = latch.update(0, T0, dwell=1, clear_dwell=3)
            self.assertFalse(cleared)
        _, _, cleared = latch.update(0, T0, dwell=1, clear_dwell=3)
        self.assertTrue(cleared)

    def test_a_flapping_signal_does_not_re_fire(self):
        latch = Latch()
        fires = 0
        for proposed in [3, 3, 0, 3, 0, 3, 3, 0, 3]:
            _, escalated, _ = latch.update(proposed, T0, dwell=2, clear_dwell=3)
            fires += 1 if escalated else 0
        self.assertLessEqual(fires, 2, "a flapping signal must not page repeatedly")


class TestSerialisation(unittest.TestCase):
    def test_device_state_round_trips(self):
        st = DeviceState("n1")
        for i in range(30):
            st.s("flow_lpm").push(T0 + i * 15, i * 0.1)
        st.flow_baseline.add(Buckets.key(3, 1), 0.4)
        st.latches["leak"].update(3, T0, dwell=1)
        st.hi_day_max = 41.2
        st.seen_modules = ["leak", "tank"]
        d = st.as_dict()
        self.assertEqual(DeviceState.from_dict(d).as_dict(), d)

    def test_series_is_bounded(self):
        s = Series(max_points=10, max_age_s=1e9)
        for i in range(500):
            s.push(T0 + i, float(i))
        self.assertLessEqual(len(s), 10)

    def test_series_evicts_by_age(self):
        s = Series(max_points=10000, max_age_s=100)
        for i in range(200):
            s.push(T0 + i, float(i))
        self.assertTrue(all(t >= T0 + 199 - 100 for t, _ in s.points))

    def test_out_of_order_arrival_is_sorted(self):
        """A node replaying its offline buffer sends older records."""
        s = Series()
        s.push(T0 + 100, 1.0)
        s.push(T0 + 50, 2.0)
        s.push(T0 + 150, 3.0)
        ts = [t for t, _ in s.points]
        self.assertEqual(ts, sorted(ts))


class TestTankConversion(unittest.TestCase):
    def test_distance_becomes_depth(self):
        tc = cfg_for_tests().tank
        # Sensor 195 cm above the floor, 60 cm of air gap => 135 cm of water.
        self.assertAlmostEqual(depth_from_distance(60, tc), 135.0)
        # Never negative, never above the tank.
        self.assertEqual(depth_from_distance(300, tc), 0.0)
        self.assertEqual(depth_from_distance(0, tc), tc.height_cm)

    def test_depth_becomes_litres(self):
        tc = cfg_for_tests().tank
        self.assertAlmostEqual(level_to_litres(tc.height_cm, tc), tc.capacity_l)
        self.assertAlmostEqual(level_to_litres(0, tc), 0.0)
        self.assertAlmostEqual(level_to_litres(tc.height_cm / 2, tc), tc.capacity_l / 2)

    def test_dip_chart_beats_the_prismatic_assumption(self):
        """An irregular tank needs a measured curve, not a ratio."""
        c = cfg_for_tests()
        c.tank.shape = "curve"
        c.tank.level_to_litres = [[0, 0], [90, 1000], [180, 5000]]
        # Half-height is 1000 L on this chart, not 2500 L.
        self.assertAlmostEqual(level_to_litres(90, c.tank), 1000.0)
        self.assertAlmostEqual(level_to_litres(135, c.tank), 3000.0)


class TestHeatIndex(unittest.TestCase):
    def test_matches_published_nws_values(self):
        # 90 F at 70% RH is 106 F on the NWS chart; 104 F at 40% is 119 F.
        self.assertAlmostEqual(heat_index_c(32.2, 70) * 9 / 5 + 32, 106, delta=1.5)
        self.assertAlmostEqual(heat_index_c(40.0, 40) * 9 / 5 + 32, 119, delta=1.5)

    def test_low_range_uses_the_simple_form(self):
        # Below 80 F the regression is not used and HI is close to the air temp.
        self.assertAlmostEqual(heat_index_c(22.0, 50), 22.0, delta=2.0)

    def test_humidity_raises_it(self):
        self.assertGreater(heat_index_c(35, 70), heat_index_c(35, 30))


class TestRegressions(unittest.TestCase):
    """One test per bug the evaluation harness found. Named for the symptom."""

    def setUp(self):
        self.cfg = cfg_for_tests()
        self.engine = Engine(self.cfg)

    def test_sensor_noise_is_not_reported_as_a_refill(self):
        """A 0.3 cm ultrasonic jitter is 8 L; across 15 s that looked like
        32 L/min of inflow, so a draining tank reported 'refilling' on every
        upward flicker."""
        import random

        rng = random.Random(4)
        st = DeviceState("n1")
        res = None
        level = 90.0
        for i in range(80):
            ts = T0 + i * 15
            level -= 0.01                      # genuinely draining, slowly
            res = self.engine.ingest(
                {"device_id": "n1", "timestamp": ts,
                 "readings": {"tank_level_cm": level + rng.gauss(0, 0.3),
                              "flow_lpm": 2.0, "temperature_c": 30, "humidity_pct": 50}},
                st, now=ts)
        self.assertEqual(res.modules["tank"].value("refilling"), 0.0,
                         "noise on a draining tank must not read as a refill")

    def test_a_real_refill_is_still_detected(self):
        """The guard above must not make the detector blind."""
        st = DeviceState("n1")
        res = None
        level = 40.0
        for i in range(60):
            ts = T0 + i * 15
            level += 60.0 * 0.25 / (self.cfg.tank.capacity_l / self.cfg.tank.height_cm)
            res = self.engine.ingest(
                {"device_id": "n1", "timestamp": ts,
                 "readings": {"tank_level_cm": level, "flow_lpm": 1.0,
                              "temperature_c": 30, "humidity_pct": 50}},
                st, now=ts)
        self.assertEqual(res.modules["tank"].value("refilling"), 1.0,
                         "a 60 L/min pump must be detected")

    def test_consumption_rate_stays_physical_under_sensor_noise(self):
        """Level noise once manufactured a 1466 L/h draw on a full tank."""
        import random

        rng = random.Random(9)
        st = DeviceState("n1")
        res = feed(self.engine, st, 60,
                   tank_level_cm=lambda i: 150.0 + rng.gauss(0, 0.3),
                   flow_lpm=0.0, temperature_c=30, humidity_pct=50)
        rate = res.modules["tank"].value("consumption_lph")
        self.assertLess(rate, 60.0, f"a still tank must not report a {rate:.0f} L/h draw")

    def test_a_projection_does_not_outrank_the_measurement(self):
        """A short ETA at a demand peak once produced 'tank is low at 85%'."""
        st = DeviceState("n1")
        res = feed(self.engine, st, 80,
                   tank_level_cm=lambda i: 153.0 - i * 0.02,   # 85% and falling fast
                   flow_lpm=8.0, temperature_c=30, humidity_pct=50)
        tank = res.modules["tank"]
        self.assertLessEqual(int(tank.severity), int(Severity.WATCH),
                             "a full tank must not reach WARNING on a projection alone")
        self.assertNotIn("is low at 8", tank.reason)
        self.assertNotIn("critically low at 8", tank.reason)

    def test_rain_on_a_dry_channel_is_not_an_alert(self):
        """This once produced 'water is present at 0.0 cm, rainfall 9 mm/h'."""
        st = DeviceState("n2")
        res = feed(self.engine, st, 40, street_water_cm=0.0, rain_rate_mmhr=30.0)
        flood = res.modules["flood"]
        self.assertEqual(int(flood.severity), int(Severity.OK))
        self.assertNotIn("water is present at 0.0", flood.reason)
        self.assertIn("drain is coping", flood.reason)

    def test_an_ordinary_hot_afternoon_is_not_a_heat_alert(self):
        """An absolute threshold alone fired on every normal afternoon."""
        st = DeviceState("n1")
        # Three days whose peak heat index is identical: nothing is anomalous.
        res = None
        ts = T0
        for day in range(3):
            for hour in range(24):
                for _ in range(4):
                    temp = 24 + 11 * max(0.0, (1 - abs(hour - 15) / 9.0))
                    rh = max(18.0, 85.0 - 3.45 * (temp - 22))
                    res = self.engine.ingest(
                        {"device_id": "n1", "timestamp": ts,
                         "readings": {"temperature_c": temp, "humidity_pct": rh}},
                        st, now=ts)
                    ts += 900
        self.assertEqual(int(res.modules["demand"].severity), int(Severity.OK),
                         "a day identical to the last two is not anomalous")

    def test_a_module_crash_is_loud_not_silent(self):
        """The tank module once failed on every sample of a 14 h run while the
        dashboard stayed green."""
        from engine import tank as tank_mod

        original = tank_mod.evaluate
        tank_mod.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            st = DeviceState("n1")
            res = self.engine.ingest(
                {"device_id": "n1", "timestamp": T0,
                 "readings": {"tank_level_cm": 90, "flow_lpm": 1.0}}, st, now=T0)
        finally:
            tank_mod.evaluate = original
        self.assertTrue(res.module_errors, "a crashing module must be reported")
        self.assertEqual(res.modules["tank"].state, "ERROR")
        self.assertGreaterEqual(int(res.modules["tank"].severity), int(Severity.WARNING))
        self.assertIn("unmonitored", res.modules["tank"].reason)

    def test_a_dropped_sample_does_not_remove_a_module(self):
        """One missing temperature reading made the whole heat panel vanish and
        claim the site had no climate sensor."""
        st = DeviceState("n1")
        feed(self.engine, st, 10, temperature_c=30.0, humidity_pct=50.0)
        res = self.engine.ingest(
            {"device_id": "n1", "timestamp": T0 + 200, "readings": {"humidity_pct": 50.0}},
            st, now=T0 + 200)
        self.assertIn("demand", res.modules)
        self.assertEqual(res.modules["demand"].state, "NO_DATA")
        self.assertIn("not reported", res.modules["demand"].reason)

    def test_a_held_severity_carries_the_reason_that_justified_it(self):
        """A latched WARNING was once captioned 'node online, nothing wrong'."""
        st = DeviceState("n1")
        # Trigger a device problem: an implausible reading.
        for i in range(3):
            ts = T0 + i * 15
            self.engine.ingest({"device_id": "n1", "timestamp": ts,
                                "readings": {"temperature_c": 999, "humidity_pct": 50}},
                               st, now=ts)
        res = self.engine.ingest({"device_id": "n1", "timestamp": T0 + 45,
                                  "readings": {"temperature_c": 30, "humidity_pct": 50}},
                                 st, now=T0 + 45)
        dev = res.modules["device"]
        if int(dev.severity) > int(Severity.OK):
            self.assertNotIn("Node online,", dev.reason,
                             "a held severity must not be captioned as healthy")
            self.assertIn("not present in the latest reading", dev.reason)

    def test_a_frozen_sensor_is_detected(self):
        """A sensor stuck at a plausible value keeps reporting, so nothing
        looks wrong -- the dangerous failure mode."""
        st = DeviceState("n2")
        res = feed(self.engine, st, 60, street_water_cm=7.3)   # identical every time
        self.assertGreaterEqual(int(res.modules["device"].severity), int(Severity.WARNING))
        self.assertIn("frozen", res.modules["device"].reason)

    def test_a_genuinely_steady_zero_is_not_called_frozen(self):
        """A dry channel really does read 0.0 cm forever."""
        st = DeviceState("n2")
        res = feed(self.engine, st, 60, street_water_cm=0.0)
        self.assertEqual(int(res.modules["device"].severity), int(Severity.OK))


class TestAlertPolicy(unittest.TestCase):
    def test_every_module_has_an_alert_threshold(self):
        for module in ("flood", "leak", "tank", "demand", "device"):
            self.assertIn(module, ALERT_FROM_SEVERITY)

    def test_safety_modules_page_earlier_than_projections(self):
        self.assertLessEqual(ALERT_FROM_SEVERITY["flood"], ALERT_FROM_SEVERITY["tank"])

    def test_alert_ids_are_stable_across_a_replay(self):
        """Ingestion must be idempotent: a retried message is the same alert."""
        from engine.pipeline import alert_id

        self.assertEqual(alert_id("n1", "flood", 1000.0), alert_id("n1", "flood", 1000.4))
        self.assertNotEqual(alert_id("n1", "flood", 1000.0), alert_id("n1", "flood", 2000.0))


if __name__ == "__main__":
    unittest.main()


class TestSensorNoiseIsNotAFault(unittest.TestCase):
    """One dropped reading in a hundred is a working sensor, not a fault.

    Treating each as a WARNING filled the alert history with dozens of
    "node health, cleared" entries and buried the alerts that mattered.
    """

    def setUp(self):
        self.engine = Engine(cfg_for_tests())

    def test_an_isolated_bad_reading_is_noted_not_alerted(self):
        st = DeviceState("n1")
        feed(self.engine, st, 10, temperature_c=30.0, humidity_pct=50.0)
        # Contiguous with the previous sample: a time jump here would be a
        # transmission gap, which is a different condition with its own alert.
        ts = T0 + 10 * 15
        res = self.engine.ingest(
            {"device_id": "n1", "timestamp": ts,
             "readings": {"temperature_c": 999, "humidity_pct": 50}}, st, now=ts)
        dev = res.modules["device"]
        self.assertLessEqual(int(dev.severity), int(Severity.INFO),
                             "a single dropout must not raise a device alert")
        self.assertIn("isolated", dev.reason)

    def test_evenly_spaced_dropouts_never_alert(self):
        """The deterministic guarantee: isolated dropouts, however many, are
        never a fault as long as they do not cluster."""
        st = DeviceState("n1")
        alerts = 0
        for i in range(400):
            ts = T0 + i * 15
            # Jitter the good readings. A real sensor never reports an
            # identical float 400 times running -- and if it did, the
            # flatline detector would be right to flag it.
            temp = 999.0 if i % 40 == 0 else 30.0 + (i % 7) * 0.1
            res = self.engine.ingest(
                {"device_id": "n1", "timestamp": ts,
                 "readings": {"temperature_c": temp,
                              "humidity_pct": 50.0 + (i % 5) * 0.1}}, st, now=ts)
            alerts += sum(1 for a in res.alerts if a.module == "device" and a.state == "active")
        self.assertEqual(alerts, 0, f"evenly spaced dropouts raised {alerts} device alerts")

    def test_repeated_bad_readings_are_a_fault(self):
        st = DeviceState("n1")
        res = None
        for i in range(8):
            ts = T0 + i * 15
            res = self.engine.ingest(
                {"device_id": "n1", "timestamp": ts,
                 "readings": {"temperature_c": 999, "humidity_pct": 50}}, st, now=ts)
        dev = res.modules["device"]
        self.assertGreaterEqual(int(dev.severity), int(Severity.WARNING),
                                "a sensor rejecting every reading is faulty")
        self.assertIn("repeatedly", dev.reason)

    def test_a_realistic_dropout_rate_is_nearly_silent(self):
        """A 1% dropout rate must produce almost no device alerts.

        Not *zero*: dropouts are random, and four landing inside one ten-minute
        window genuinely does look like a failing sensor -- that is ten times
        the expected rate, and the detector is right to say so. The guarantee
        is that this is rare. Across ten seeds and 4,000 samples the old
        per-sample rule raised hundreds of alerts; this asserts single digits.
        """
        import random

        total = 0
        for seed in range(10):
            rng = random.Random(seed)
            st = DeviceState("n1")
            for i in range(400):
                ts = T0 + i * 15
                temp = 999.0 if rng.random() < 0.01 else 30.0 + rng.gauss(0, 0.2)
                res = self.engine.ingest(
                    {"device_id": "n1", "timestamp": ts,
                     "readings": {"temperature_c": temp,
                                  "humidity_pct": 50.0 + rng.gauss(0, 0.5)}}, st, now=ts)
                total += sum(1 for a in res.alerts
                             if a.module == "device" and a.state == "active")
        self.assertLessEqual(total, 5,
                             f"a 1% dropout rate raised {total} device alerts over 4,000 samples")
