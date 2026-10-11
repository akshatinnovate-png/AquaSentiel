"""The virtual rig: physics that is actually physical, and firmware behaviour.

If the simulator is wrong, every measured number in the report is wrong, so it
gets tested as seriously as the engine does.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim import scenarios
from sim.node import BUFFER_CAPACITY, build_default_nodes
from sim.sensors import ClimateModel, FlowModel, RainModel, StreetWaterModel, TankModel


class TestTankPhysics(unittest.TestCase):
    def test_volume_is_conserved(self):
        t = TankModel(capacity_l=5000, height_cm=180, start_pct=50)
        t.outflow_lpm = 10.0
        start = t.volume_l
        for _ in range(60):
            t.step(60.0)           # 60 minutes at 10 L/min
        self.assertAlmostEqual(start - t.volume_l, 600.0, delta=1.0)

    def test_it_cannot_overfill_or_go_negative(self):
        t = TankModel(capacity_l=1000, height_cm=100, start_pct=99)
        t.inflow_lpm = 500.0
        for _ in range(100):
            t.step(60.0)
        self.assertLessEqual(t.volume_l, 1000.0)
        t.inflow_lpm, t.outflow_lpm = 0.0, 500.0
        for _ in range(100):
            t.step(60.0)
        self.assertGreaterEqual(t.volume_l, 0.0)

    def test_level_tracks_volume(self):
        t = TankModel(capacity_l=5000, height_cm=180, start_pct=25)
        self.assertAlmostEqual(t.true_level_cm, 45.0, places=3)
        self.assertAlmostEqual(t.true_pct, 25.0, places=3)


class TestStreetWaterPhysics(unittest.TestCase):
    def test_a_clear_drain_copes_with_heavy_rain(self):
        """A 45 mm/h storm on a working drain must not pond. An earlier unit
        error made the runoff a thousand times too small, and the flood was
        never detected; this is the test that would have caught it."""
        s = StreetWaterModel()
        s.blocked = 0.0
        for _ in range(3600):
            s.step(1.0, 45.0)
        self.assertLess(s.depth_cm, 6.0, "a clear drain should stay essentially dry")

    def test_a_blocked_drain_ponds(self):
        s = StreetWaterModel()
        s.blocked = 0.75
        for _ in range(1800):                       # 30 minutes of heavy rain
            s.step(1.0, 45.0)
        self.assertGreater(s.depth_cm, 6.0,
                           "half an hour of heavy rain on a blocked drain must pond")
        for _ in range(3600):                       # and it keeps rising
            s.step(1.0, 45.0)
        self.assertGreater(s.depth_cm, 18.0,
                           "sustained rain on a blocked drain reaches the danger depth")

    def test_it_drains_once_the_rain_stops(self):
        s = StreetWaterModel()
        s.blocked = 0.0
        s.depth_cm = 25.0
        for _ in range(1800):
            s.step(1.0, 0.0)
        self.assertLess(s.depth_cm, 2.0)

    def test_runoff_units_are_litres(self):
        """One mm of rain on one square metre is one litre."""
        s = StreetWaterModel()
        s.blocked = 1.0            # no outflow at all, so inflow is measurable
        s.contributing_m2, s.runoff_ratio, s.area_m2 = 1000.0, 1.0, 100.0
        s.depth_cm = 0.0
        s.step(3600.0, 10.0)       # 10 mm/h for one hour over 1000 m2 = 10000 L
        # 10000 L spread over 100 m2 is 10 cm.
        self.assertAlmostEqual(s.depth_cm, 10.0, delta=0.2)


class TestClimate(unittest.TestCase):
    def test_humidity_is_anticorrelated_with_temperature(self):
        c = ClimateModel()
        readings = []
        for h in range(24):
            c.step(3600, h * 3600)
            readings.append((c.true_temp_c, c.true_rh))
        hottest = max(readings)
        coolest = min(readings)
        self.assertLess(hottest[1], coolest[1],
                        "the hottest hour must be the driest, as in reality")

    def test_an_ordinary_day_does_not_reach_extreme_danger(self):
        """The model once produced 35 C at 68% RH -- a heat index of 50 C --
        on a routine afternoon, which made every normal day an emergency."""
        from engine.demand import heat_index_c

        c = ClimateModel()
        peak = 0.0
        for minute in range(0, 1440, 10):
            c.step(600, minute * 60)
            peak = max(peak, heat_index_c(c.true_temp_c, c.true_rh))
        self.assertLess(peak, 41.0, f"an ordinary day peaked at a heat index of {peak:.1f} C")

    def test_a_heat_offset_pushes_it_over_the_advisory_level(self):
        from engine.demand import heat_index_c

        c = ClimateModel()
        c.heat_offset_c = 9.0
        peak = 0.0
        for minute in range(0, 1440, 10):
            c.step(600, minute * 60)
            peak = max(peak, heat_index_c(c.true_temp_c, c.true_rh))
        self.assertGreater(peak, 41.0)


class TestFlowMeter(unittest.TestCase):
    def test_it_reads_zero_below_the_turbine_cut_in(self):
        f = FlowModel()
        f.true_lpm = 0.01
        self.assertEqual(f.read(0), 0.0)

    def test_it_quantises_to_whole_pulses(self):
        f = FlowModel(k_pulses_per_l=450.0)
        f.noise = 0.0
        f.true_lpm = 1.0
        v = f.read(0)
        self.assertAlmostEqual(v, round(v * 450 / 60) * 60 / 450, places=6)

    def test_it_never_reports_negative_flow(self):
        f = FlowModel()
        for lpm in (0.0, 0.02, 0.5, 5.0):
            f.true_lpm = lpm
            for _ in range(50):
                v = f.read(0)
                if v is not None:
                    self.assertGreaterEqual(v, 0.0)


class TestRainGauge(unittest.TestCase):
    def test_it_reports_in_discrete_tips(self):
        r = RainModel(tip_mm=0.2)
        r.intensity_mmhr = 36.0           # 0.6 mm per minute
        total = 0.0
        for _ in range(60):
            r.step(60.0)
            total += r.read_increment_mm(0)
        self.assertAlmostEqual(total, 36.0, delta=0.4)
        self.assertAlmostEqual(total / 0.2, round(total / 0.2), places=6)


class TestSensorFaults(unittest.TestCase):
    def test_disconnected_reports_nothing(self):
        t = TankModel()
        t.inject_fault("disconnected")
        self.assertIsNone(t.read(0))

    def test_stuck_holds_one_value(self):
        t = TankModel(start_pct=50)
        t.inject_fault("stuck")
        first = t.read(0)
        t.volume_l = t.capacity_l * 0.1
        self.assertEqual(t.read(1), first, "a frozen sensor keeps reporting the old value")


class TestFirmware(unittest.TestCase):
    def test_a_payload_carries_everything_the_cloud_needs(self):
        nodes = build_default_nodes()
        node = nodes["aquasentinel-node-01"]
        node.step_physics(scenarios.FIXED_EPOCH, 15)
        p = node.sample(scenarios.FIXED_EPOCH)
        for key in ("device_id", "timestamp", "readings", "quality", "seq",
                    "buffered", "fw_version", "rssi"):
            self.assertIn(key, p)
        self.assertEqual(set(p["readings"]) - set(p["quality"]), set())

    def test_an_outage_buffers_and_replays_in_order(self):
        nodes = build_default_nodes()
        node = nodes["aquasentinel-node-01"]
        delivered = []
        node.set_online(False)
        for i in range(20):
            node.step_physics(scenarios.FIXED_EPOCH + i * 15, 15)
            node.publish(node.sample(scenarios.FIXED_EPOCH + i * 15),
                         lambda p: (delivered.append(p), True)[1])
        self.assertEqual(delivered, [], "nothing is delivered while the link is down")
        self.assertEqual(len(node.buffer), 20)

        node.set_online(True)
        node.step_physics(scenarios.FIXED_EPOCH + 300, 15)
        node.publish(node.sample(scenarios.FIXED_EPOCH + 300),
                     lambda p: (delivered.append(dict(p)), True)[1])
        self.assertEqual(len(delivered), 21, "the backlog is drained, then the live record")
        self.assertTrue(all(d["buffered"] for d in delivered[:20]),
                        "replayed records must be labelled as back-filled")
        self.assertFalse(delivered[-1]["buffered"])
        seqs = [d["seq"] for d in delivered]
        self.assertEqual(seqs, sorted(seqs), "the backlog is replayed oldest first")

    def test_the_buffer_is_bounded(self):
        nodes = build_default_nodes()
        node = nodes["aquasentinel-node-01"]
        node.set_online(False)
        for i in range(BUFFER_CAPACITY + 50):
            node.step_physics(scenarios.FIXED_EPOCH + i * 15, 15)
            node.publish(node.sample(scenarios.FIXED_EPOCH + i * 15), lambda p: True)
        self.assertEqual(len(node.buffer), BUFFER_CAPACITY)
        self.assertGreater(node.dropped_records, 0, "overflow must be counted, not hidden")

    def test_an_implausible_value_is_flagged_not_silently_dropped(self):
        nodes = build_default_nodes()
        node = nodes["aquasentinel-node-01"]
        node.sensors["climate"].bias = 500.0      # force an impossible temperature
        node.step_physics(scenarios.FIXED_EPOCH, 15)
        p = node.sample(scenarios.FIXED_EPOCH)
        self.assertEqual(p["quality"]["temperature_c"], 3,
                         "the cloud must be able to see the sensor is faulty")


class TestScenarios(unittest.TestCase):
    def test_every_registered_scenario_builds_and_steps(self):
        for name in scenarios.REGISTRY:
            scen = scenarios.get(name)
            nodes = scen.build()
            t = scen.start_epoch
            for i in range(10):
                scen.apply(t, i * scen.interval_s, nodes)
                for node in nodes.values():
                    node.step_physics(t, scen.interval_s)
                    self.assertIsInstance(node.sample(t), dict)
                t += scen.interval_s

    def test_every_scenario_states_what_it_entitles_us_to_claim(self):
        for name in scenarios.REGISTRY:
            self.assertTrue(scenarios.get(name).claim, f"{name} has no claim")

    def test_the_quiet_site_has_no_truth_windows(self):
        """The false-alarm test must expect nothing, or it proves nothing."""
        self.assertEqual(scenarios.get("baseline").truth, [])

    def test_predicates_read_true_state_not_sensor_output(self):
        nodes = scenarios.get("flood").build()
        street = nodes["aquasentinel-node-02"].sensors["street"]
        pred = scenarios.ponding(6.0)
        street.depth_cm = 0.0
        self.assertFalse(pred(nodes))
        street.depth_cm = 10.0
        street.inject_fault("disconnected")      # sensor dead, water still there
        self.assertTrue(pred(nodes), "ground truth must not depend on a working sensor")

    def test_the_clock_can_be_fixed_or_anchored(self):
        fixed = scenarios.use_fixed_epoch()
        self.assertEqual(fixed, scenarios.FIXED_EPOCH)
        import time

        anchored = scenarios.anchor_to_now(6.0)
        self.assertAlmostEqual(anchored + 6 * 3600, time.time(), delta=5)
        scenarios.use_fixed_epoch()

    def test_demand_profile_is_diurnal_and_heat_sensitive(self):
        night = scenarios.school_demand_lpm(3.0)
        lunch = scenarios.school_demand_lpm(12.5)
        lunch_hot = scenarios.school_demand_lpm(12.5, heat_offset_c=8.0)
        self.assertLess(night, 0.1)
        self.assertGreater(lunch, 5.0)
        self.assertGreater(lunch_hot, lunch, "heat must genuinely raise demand")
        self.assertAlmostEqual(scenarios.school_demand_lpm(3.0, heat_offset_c=8.0), night,
                               places=6, msg="heat must not raise demand at 3 a.m.")


if __name__ == "__main__":
    unittest.main()
