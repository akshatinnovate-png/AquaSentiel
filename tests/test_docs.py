"""The published numbers must match the measured ones.

Documentation drifts silently: a detector improves, the report regenerates, and
the README keeps quoting last week's figure. On this build that happened twice
within an hour of each other, so it is now a test.

These skip rather than fail when `eval/results/metrics.json` is absent, so a
fresh clone can run the suite before running the harness.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

METRICS = ROOT / "eval" / "results" / "metrics.json"


def load():
    if not METRICS.exists():
        raise unittest.SkipTest("no metrics yet; run `make eval`")
    data = json.loads(METRICS.read_text())
    return data, {s["scenario"]: s for s in data["scenarios"]}


def delay_min(scenario: dict, module: str):
    for d in scenario["detections"]:
        if d["module"] == module:
            return d["delay_min"]
    return None


class TestPublishedFiguresMatchMeasurements(unittest.TestCase):
    def setUp(self):
        self.data, self.by = load()
        self.readme = (ROOT / "README.md").read_text()
        self.algorithms = (ROOT / "docs" / "ALGORITHMS.md").read_text()

    def assertQuoted(self, text: str, needle: str, where: str):
        # The failure message deliberately does not include the document: a
        # multi-kilobyte diff buries the one fact that matters.
        self.assertTrue(needle in text,
                        f"{where} no longer quotes {needle!r} -- regenerate the docs "
                        f"after `make eval`")

    def assertNumberQuoted(self, text: str, value: float, unit: str, where: str):
        """Accept any sensible 2-decimal rendering of a measured value.

        29.985 formats to '29.98' under float repr and to '29.99' under
        round-half-up. Both are honest; pinning one makes the test about
        formatting rather than about accuracy.
        """
        from decimal import Decimal, ROUND_HALF_UP

        candidates = {
            f"{value:.2f}{unit}",
            f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}{unit}",
        }
        self.assertTrue(any(c in text for c in candidates),
                        f"{where} should quote one of {sorted(candidates)} "
                        f"(measured {value}) -- regenerate the docs after `make eval`")

    def test_detection_delays_are_quoted_correctly(self):
        for scenario, module, quoted in [
            ("flood", "flood", "0.8 min"),
            ("leak", "leak", "30.5 min"),
            ("depletion", "tank", "52.3 min"),
            ("outage", "flood", "8.3 min"),
            ("longhaul", "demand", "4.0 min"),
        ]:
            measured = delay_min(self.by[scenario], module)
            self.assertIsNotNone(measured, f"{scenario}/{module} was not detected at all")
            rounded = f"{measured:.1f} min"
            self.assertAlmostEqual(
                measured, float(quoted.split()[0]), delta=0.06,
                msg=f"{scenario}/{module} measured {rounded}, README says {quoted}")
            self.assertQuoted(self.readme, quoted, f"README ({scenario}/{module})")

    def test_sample_counts_are_quoted_correctly(self):
        total = sum(s["samples_ingested"] for s in self.data["scenarios"])
        self.assertQuoted(self.readme, f"{total:,}", "README (total samples)")
        self.assertQuoted(self.readme, f"{self.by['baseline']['samples_ingested']:,}",
                          "README (quiet-site samples)")

    def test_forecast_comparison_is_quoted_correctly(self):
        v = self.by["longhaul"]["demand_forecast"]
        self.assertTrue(v["adopted"], "the 14-day run no longer adopts the model")
        for value, label in ((v["baseline_mae_lph"], "baseline"), (v["model_mae_lph"], "model")):
            self.assertNumberQuoted(self.readme, value, " L/h", f"README (longhaul {label} MAE)")
        self.assertNumberQuoted(self.algorithms, v["model_mae_lph"], " L/h",
                                "ALGORITHMS.md (longhaul model MAE)")
        improvement = 100 * (v["baseline_mae_lph"] - v["model_mae_lph"]) / v["baseline_mae_lph"]
        self.assertQuoted(self.readme, f"{improvement:.1f}%", "README (improvement)")

    def test_the_headline_claim_of_zero_false_alarms_still_holds(self):
        total = sum(s["false_alarm_episodes"] for s in self.data["scenarios"])
        self.assertEqual(total, 0,
                         "the README claims zero false alarms across every scenario")
        missed = [m for s in self.data["scenarios"] for m in s.get("missed", [])]
        self.assertEqual(missed, [], "the README claims zero missed detections")

    def test_no_module_errored_during_measurement(self):
        errors = [e for s in self.data["scenarios"] for e in s.get("module_errors", [])]
        self.assertEqual(errors, [], f"a detection module failed during measurement: {errors}")

    def test_tank_accuracy_range_is_quoted_correctly(self):
        pcts = [s["tank_volume_error"]["mae_pct_of_capacity"]
                for s in self.data["scenarios"] if "tank_volume_error" in s]
        self.assertTrue(pcts)
        self.assertNumberQuoted(self.readme, min(pcts), "", "README (best volume accuracy)")
        self.assertNumberQuoted(self.readme, max(pcts), "", "README (worst volume accuracy)")


class TestDocumentsExistAndLink(unittest.TestCase):
    """Every document the README promises must actually be there."""

    def test_all_linked_documents_exist(self):
        readme = (ROOT / "README.md").read_text()
        for link in set(re.findall(r"\]\((docs/[A-Za-z0-9_./-]+\.md)\)", readme)):
            self.assertTrue((ROOT / link).exists(), f"README links to a missing {link}")

    def test_the_results_report_is_committed(self):
        self.assertTrue((ROOT / "eval" / "results" / "REPORT.md").exists(),
                        "the measured-results report must be committed, not just generated")

    def test_limitations_names_the_hardware_gap(self):
        text = (ROOT / "docs" / "LIMITATIONS.md").read_text().lower()
        for phrase in ("no physical hardware", "not a leak", "not a heatwave declaration"):
            self.assertIn(phrase, text, f"LIMITATIONS.md must state: {phrase}")


if __name__ == "__main__":
    unittest.main()
