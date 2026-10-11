"""The arithmetic every detection decision rests on."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.stats import (clamp, ewma, lerp_curve, mad, mae, median, median_filter,
                          ols_slope, quantile, robust_z, rmse, solve_ridge, stdev)


class TestRobustStats(unittest.TestCase):
    def test_median_handles_both_parities(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)
        self.assertEqual(median([]), 0.0)

    def test_quantile_matches_linear_interpolation(self):
        xs = [1, 2, 3, 4]
        self.assertAlmostEqual(quantile(xs, 0.0), 1.0)
        self.assertAlmostEqual(quantile(xs, 0.5), 2.5)
        self.assertAlmostEqual(quantile(xs, 1.0), 4.0)
        self.assertAlmostEqual(quantile(xs, 0.25), 1.75)

    def test_mad_ignores_a_single_wild_outlier(self):
        """The reason we prefer MAD: one splashy ultrasonic reading."""
        clean = [10, 10.1, 9.9, 10.2, 9.8]
        dirty = clean + [95.0]
        self.assertLess(abs(mad(dirty) - mad(clean)), 0.4)
        # A standard deviation, by contrast, is wrecked by it.
        self.assertGreater(stdev(dirty), 5 * stdev(clean))

    def test_robust_z_falls_back_when_mad_is_zero(self):
        # A constant series has MAD 0; the function must not divide by it.
        z = robust_z(5.0, [1.0, 1.0, 1.0, 1.0])
        self.assertTrue(math.isfinite(z))
        self.assertGreater(z, 0)


class TestFilters(unittest.TestCase):
    def test_median_filter_is_causal(self):
        """It must not see the future, or the measured delay is a lie."""
        xs = [1.0] * 5 + [100.0] + [1.0] * 5
        out = median_filter(xs, 5)
        self.assertEqual(len(out), len(xs))
        # The spike is suppressed...
        self.assertLess(out[5], 2.0)
        # ...and the output at index i depends only on inputs up to i.
        prefix = median_filter(xs[:6], 5)
        self.assertEqual(out[:6], prefix)

    def test_ewma_tracks_a_step(self):
        out = ewma([0.0] * 5 + [10.0] * 20, 0.3)
        self.assertLess(out[4], 0.1)
        self.assertGreater(out[-1], 9.0)


class TestSlope(unittest.TestCase):
    def test_slope_matches_the_worked_example(self):
        """4 cm to 10 cm over 3 minutes is 2 cm/min, per the design note."""
        ts = [0, 60, 120, 180]
        vs = [4, 6, 8, 10]
        s = ols_slope(ts, vs)
        self.assertAlmostEqual(s.value * 60, 2.0, places=6)
        self.assertAlmostEqual(s.r2, 1.0, places=6)
        self.assertAlmostEqual(s.stderr, 0.0, places=9)

    def test_noise_produces_a_large_relative_stderr(self):
        """This is what lets the tank module reject a trend that is noise."""
        import random

        rng = random.Random(1)
        ts = [i * 15.0 for i in range(40)]
        vs = [100 + rng.gauss(0, 8) for _ in ts]
        s = ols_slope(ts, vs)
        self.assertLess(abs(s.value), 1.96 * s.stderr)
        self.assertLess(s.r2, 0.3)

    def test_degenerate_inputs_do_not_raise(self):
        for ts, vs in ([], []), ([1.0], [1.0]), ([2.0, 2.0], [1.0, 3.0]):
            s = ols_slope(ts, vs)
            self.assertTrue(math.isfinite(s.value))


class TestRidge(unittest.TestCase):
    def test_recovers_a_known_linear_relationship(self):
        rows = [[1.0, float(x)] for x in range(20)]
        y = [3.0 + 2.0 * x for x in range(20)]
        w = solve_ridge(rows, y, 1e-9)
        self.assertAlmostEqual(w[0], 3.0, places=3)
        self.assertAlmostEqual(w[1], 2.0, places=3)

    def test_regularisation_shrinks_coefficients(self):
        rows = [[1.0, float(x)] for x in range(20)]
        y = [3.0 + 2.0 * x for x in range(20)]
        self.assertLess(solve_ridge(rows, y, 100.0)[1], solve_ridge(rows, y, 1e-9)[1])

    def test_singular_matrix_does_not_raise(self):
        # Two identical columns: the system is rank deficient.
        rows = [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]
        w = solve_ridge(rows, [1.0, 1.0, 1.0], 0.0)
        self.assertTrue(all(math.isfinite(v) for v in w))


class TestDipChart(unittest.TestCase):
    def test_interpolates_an_irregular_tank(self):
        curve = [[0, 0], [50, 100], [100, 400]]
        self.assertEqual(lerp_curve(curve, 0), 0)
        self.assertEqual(lerp_curve(curve, 50), 100)
        self.assertEqual(lerp_curve(curve, 75), 250)
        self.assertEqual(lerp_curve(curve, 100), 400)

    def test_clamps_outside_the_chart(self):
        curve = [[10, 5], [90, 95]]
        self.assertEqual(lerp_curve(curve, -5), 5)
        self.assertEqual(lerp_curve(curve, 200), 95)

    def test_unsorted_input_is_sorted(self):
        self.assertEqual(lerp_curve([[100, 400], [0, 0], [50, 100]], 75), 250)


class TestErrorMetrics(unittest.TestCase):
    def test_mae_and_rmse(self):
        self.assertAlmostEqual(mae([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertAlmostEqual(mae([1, 2, 3], [2, 3, 4]), 1.0)
        self.assertAlmostEqual(rmse([0, 0], [3, 4]), math.sqrt(12.5))

    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 1), 1)
        self.assertEqual(clamp(-5, 0, 1), 0)
        self.assertEqual(clamp(0.5, 0, 1), 0.5)


if __name__ == "__main__":
    unittest.main()
