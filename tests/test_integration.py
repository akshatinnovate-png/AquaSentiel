"""End-to-end: payload in, alert out, through the same service the Lambdas use.

These exercise the real path -- `Service.ingest` -> engine -> store -> the API
shapes the dashboard consumes -- so a change that only breaks the wiring
between working parts still fails a test.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import DEFAULT_SITE, SiteConfig
from services.common.app import Notifier, Service
from services.common.repo import MemoryRepo, _clean, _unclean

T0 = 1760000000.0


def service(clock_start=T0):
    cfg = SiteConfig.from_dict(DEFAULT_SITE.as_dict())
    cfg.calibrated = True
    box = {"t": clock_start}
    svc = Service(repo=MemoryRepo(), cfg=cfg, notifier=Notifier(), clock=lambda: box["t"])
    return svc, box


class TestIngestPath(unittest.TestCase):
    def test_a_reading_becomes_queryable_state(self):
        svc, box = service()
        for i in range(10):
            ts = T0 + i * 15
            box["t"] = ts
            out = svc.ingest({"device_id": "aquasentinel-node-01", "timestamp": ts,
                              "readings": {"tank_level_cm": 120, "flow_lpm": 1.5,
                                           "temperature_c": 31, "humidity_pct": 48},
                              "seq": i + 1, "rssi": -60}, now=ts)
            self.assertTrue(out["ok"])
            self.assertFalse(out["module_errors"], out["module_errors"])

        ov = svc.overview()
        self.assertEqual(len(ov["devices"]), 1)
        self.assertFalse(ov["devices"][0]["stale"])
        detail = svc.device_detail("aquasentinel-node-01")
        self.assertIn("tank_level_cm", detail["series"])
        self.assertEqual(len(detail["series"]["flow_lpm"]), 10)

    def test_rejected_values_never_reach_a_chart(self):
        svc, box = service()
        box["t"] = T0
        svc.ingest({"device_id": "n1", "timestamp": T0,
                    "readings": {"flow_lpm": 1.0, "temperature_c": 999}}, now=T0)
        detail = svc.device_detail("n1")
        self.assertNotIn("temperature_c", detail["series"],
                         "an implausible value must not be plotted")
        self.assertIn("flow_lpm", detail["series"])

    def test_state_survives_a_round_trip_through_the_store(self):
        """Lambda reloads device state from DynamoDB on every invocation, so
        anything the engine learns must survive serialisation."""
        svc, box = service()
        for i in range(40):
            ts = T0 + i * 15
            box["t"] = ts
            svc.ingest({"device_id": "n1", "timestamp": ts,
                        "readings": {"flow_lpm": 0.5, "tank_level_cm": 100,
                                     "temperature_c": 30, "humidity_pct": 50}}, now=ts)
        item = svc.repo.get_device("n1")
        # The store must hold JSON-serialisable state and nothing exotic.
        json.dumps(item["engine_state"])
        self.assertGreater(item["samples"], 30)
        self.assertTrue(item["engine_state"]["series"])


class TestFreshness(unittest.TestCase):
    def test_a_node_that_goes_quiet_becomes_stale(self):
        svc, box = service()
        box["t"] = T0
        svc.ingest({"device_id": "n1", "timestamp": T0,
                    "readings": {"flow_lpm": 1.0}}, now=T0)
        self.assertFalse(svc.overview()["devices"][0]["stale"])
        box["t"] = T0 + 3600          # an hour later, nothing has arrived
        ov = svc.overview()
        self.assertTrue(ov["devices"][0]["stale"])
        self.assertIn("n1", ov["stale_devices"])

    def test_a_stale_node_does_not_set_the_site_severity(self):
        """Last known state is not current state."""
        svc, box = service()
        for i in range(20):
            ts = T0 + i * 15
            box["t"] = ts
            # A critically low tank.
            svc.ingest({"device_id": "n1", "timestamp": ts,
                        "readings": {"tank_level_cm": 5, "flow_lpm": 4.0,
                                     "temperature_c": 30, "humidity_pct": 50}}, now=ts)
        self.assertGreater(svc.overview()["overall_severity"], 0)
        box["t"] = T0 + 7200
        self.assertEqual(svc.overview()["overall_severity"], 0,
                         "a node we have not heard from cannot be the reason for an alarm")

    def test_clock_skew_is_distinguished_from_staleness(self):
        """A node with a wrong clock is still reporting."""
        svc, box = service()
        wall = T0 + 100000
        box["t"] = wall
        svc.ingest({"device_id": "n1", "timestamp": T0,   # device clock far behind
                    "readings": {"flow_lpm": 1.0}}, now=T0)
        dev = svc.overview()["devices"][0]
        self.assertFalse(dev["stale"], "it just reported; it is not stale")
        self.assertTrue(dev["clock_skew_warning"], "but its clock is wrong, and we say so")


class TestAlertLifecycle(unittest.TestCase):
    def _drain_tank(self, svc, box, n=90):
        level, res = 20.0, None
        for i in range(n):
            ts = T0 + i * 15
            box["t"] = ts
            level = max(1.0, level - 0.05)
            res = svc.ingest({"device_id": "n1", "timestamp": ts,
                              "readings": {"tank_level_cm": level, "flow_lpm": 6.0,
                                           "temperature_c": 32, "humidity_pct": 45}}, now=ts)
        return res

    def test_an_alert_is_raised_stored_and_notified(self):
        svc, box = service()
        self._drain_tank(svc, box)
        active = svc.repo.list_alerts(state="active")
        self.assertTrue(active, "a tank draining to empty must raise something")
        self.assertTrue(svc.notifier.sent, "an alert must be published")
        a = active[0]
        for key in ("alert_id", "module", "severity_label", "reason", "evidence", "confidence"):
            self.assertIn(key, a)
        self.assertTrue(a["evidence"], "an alert must carry its evidence")

    def test_acknowledging_sticks(self):
        svc, box = service()
        self._drain_tank(svc, box)
        alert_id = svc.repo.list_alerts(state="active")[0]["alert_id"]
        out = svc.acknowledge(alert_id, "caretaker")
        self.assertTrue(out["ok"])
        self.assertEqual(svc.repo.get_alert(alert_id)["state"], "acknowledged")
        # A later re-raise of the same episode must not silently un-acknowledge it.
        self._drain_tank(svc, box, n=20)
        self.assertEqual(svc.repo.get_alert(alert_id)["state"], "acknowledged")

    def test_a_replayed_reading_does_not_page_anyone(self):
        """A back-filled record describes the past."""
        svc, box = service()
        box["t"] = T0
        before = len(svc.notifier.sent)
        level = 6.0
        for i in range(60):
            ts = T0 + i * 15
            box["t"] = ts
            svc.ingest({"device_id": "n1", "timestamp": ts, "buffered": True,
                        "readings": {"tank_level_cm": level, "flow_lpm": 6.0,
                                     "temperature_c": 32, "humidity_pct": 45}}, now=ts)
        raised = svc.repo.list_alerts()
        self.assertTrue(raised, "the alert is still recorded")
        self.assertEqual(len(svc.notifier.sent), before,
                         "but nobody is paged about a condition from the past")


class TestApiShapes(unittest.TestCase):
    """The dashboard reads these keys; a rename must fail a test, not a demo."""

    def test_overview_contract(self):
        svc, box = service()
        box["t"] = T0
        svc.ingest({"device_id": "aquasentinel-node-01", "timestamp": T0,
                    "readings": {"tank_level_cm": 90, "flow_lpm": 1.0,
                                 "temperature_c": 30, "humidity_pct": 50}}, now=T0)
        ov = svc.overview()
        for key in ("site", "generated_at", "overall_severity", "overall_label",
                    "devices", "inspection_queue", "active_alerts", "stale_devices"):
            self.assertIn(key, ov)
        for key in ("site_id", "name", "calibrated", "calibration_note", "stale_after_s"):
            self.assertIn(key, ov["site"])
        dev = ov["devices"][0]
        for key in ("device_id", "overall_severity", "overall_label", "modules",
                    "supply_outlook", "inspection_queue", "stale", "age_s",
                    "clock_skew_s", "clock_skew_warning", "label"):
            self.assertIn(key, dev)

    def test_every_signal_declares_its_provenance(self):
        """The dashboard's honesty badge depends on this being universal."""
        svc, box = service()
        for i in range(20):
            ts = T0 + i * 15
            box["t"] = ts
            svc.ingest({"device_id": "n1", "timestamp": ts,
                        "readings": {"tank_level_cm": 90, "flow_lpm": 1.0,
                                     "street_water_cm": 2.0, "rain_rate_mmhr": 1.0,
                                     "temperature_c": 30, "humidity_pct": 50}}, now=ts)
        allowed = {"measured", "derived", "projected", "forecast"}
        modules = svc.overview()["devices"][0]["modules"]
        self.assertTrue(modules)
        for name, mod in modules.items():
            for s in mod["signals"]:
                self.assertIn(s["kind"], allowed, f"{name}.{s['name']} has kind {s['kind']!r}")
                self.assertIsNotNone(s.get("name"))

    def test_module_result_contract(self):
        svc, box = service()
        box["t"] = T0
        svc.ingest({"device_id": "n1", "timestamp": T0,
                    "readings": {"tank_level_cm": 90, "flow_lpm": 1.0}}, now=T0)
        for mod in svc.overview()["devices"][0]["modules"].values():
            for key in ("module", "severity", "severity_label", "state", "title",
                        "reason", "recommendation", "confidence", "signals", "evidence"):
                self.assertIn(key, mod)


class TestStoreEncoding(unittest.TestCase):
    def test_floats_survive_the_dynamodb_encoding(self):
        payload = {"a": 1.5, "b": [1.0, 2.25], "c": {"d": 0.125}}
        self.assertEqual(_unclean(_clean(payload)), payload)

    def test_nan_is_dropped_rather_than_stored(self):
        self.assertIsNone(_clean(float("nan")))
        self.assertIsNone(_clean(float("inf")))


if __name__ == "__main__":
    unittest.main()


class TestSupersededAlerts(unittest.TestCase):
    """A module that escalates must close its previous episode.

    The dashboard once showed a WARNING saying "4.5 h left" sitting beside a
    CRITICAL saying "2.0 h left" for the same tank, and a caretaker had to work
    out which one was current.
    """

    def test_escalating_closes_the_earlier_episode(self):
        svc, box = service()
        level = 22.0
        for i in range(300):
            ts = T0 + i * 15
            box["t"] = ts
            level = max(0.4, level - 0.06)
            svc.ingest({"device_id": "n1", "timestamp": ts,
                        "readings": {"tank_level_cm": level, "flow_lpm": 7.0,
                                     "temperature_c": 32, "humidity_pct": 45}}, now=ts)
        active = [a for a in svc.repo.list_alerts(state="active") if a["module"] == "tank"]
        self.assertLessEqual(len(active), 1,
                             f"one tank condition must not leave {len(active)} alerts open")
        all_tank = [a for a in svc.repo.list_alerts() if a["module"] == "tank"]
        self.assertTrue(any(a["state"] == "superseded" for a in all_tank),
                        "the earlier, less severe episode should be marked superseded")

    def test_an_empty_tank_is_described_plainly(self):
        svc, box = service()
        for i in range(40):
            ts = T0 + i * 15
            box["t"] = ts
            svc.ingest({"device_id": "n1", "timestamp": ts,
                        "readings": {"tank_level_cm": 0.0, "flow_lpm": 0.0,
                                     "temperature_c": 30, "humidity_pct": 50}}, now=ts)
        note = svc.overview()["devices"][0]["supply_outlook"]["note"]
        self.assertIn("empty", note.lower())
        self.assertNotIn("0 L to spare", note)


class TestOutlookWording(unittest.TestCase):
    def test_a_near_zero_draw_does_not_claim_a_precise_eta(self):
        """A 169.9 h figure off an overnight trickle implies an accuracy the
        measurement cannot support."""
        svc, box = service()
        for i in range(60):
            ts = T0 + i * 15
            box["t"] = ts
            svc.ingest({"device_id": "n1", "timestamp": ts,
                        "readings": {"tank_level_cm": 90.0 + (i % 3) * 0.01,
                                     "flow_lpm": 0.02,
                                     "temperature_c": 24 + (i % 5) * 0.1,
                                     "humidity_pct": 70 + (i % 4) * 0.1}}, now=ts)
        note = svc.overview()["devices"][0]["supply_outlook"]["note"]
        import re
        big = [float(m) for m in re.findall(r"estimate is ([0-9.]+) h", note)]
        self.assertFalse([v for v in big if v > 48],
                         f"outlook quotes an over-precise ETA: {note}")
