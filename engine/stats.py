"""Small, robust statistics helpers implemented with the standard library only.

These are deliberately simple and auditable: every detection decision in
AquaSentinel can be traced back to one of these functions, which makes the
system explainable to a caretaker and testable in CI.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def median(xs: Iterable[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (same convention as numpy's default)."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    if n == 1:
        return s[0]
    q = min(max(q, 0.0), 1.0)
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def mad(xs: Sequence[float]) -> float:
    """Median absolute deviation, scaled to be comparable with a std-dev.

    Robust to the single wild outlier a splashing ultrasonic sensor produces,
    which is exactly why we prefer it over a plain standard deviation.
    """
    if not xs:
        return 0.0
    med = median(xs)
    return 1.4826 * median([abs(x - med) for x in xs])


def robust_z(x: float, xs: Sequence[float], floor: float = 1e-6) -> float:
    """Deviation of ``x`` from the body of ``xs``, in robust sigmas."""
    if not xs:
        return 0.0
    scale = mad(xs)
    if scale < floor:
        scale = max(stdev(xs), floor)
    return (x - median(xs)) / scale


def median_filter(xs: Sequence[float], window: int = 5) -> list[float]:
    """Causal median filter: output[i] uses only samples up to i.

    Causality matters -- a centred filter would let the dashboard "see" the
    future and would understate our true detection delay.
    """
    if window < 1:
        return list(xs)
    out: list[float] = []
    for i in range(len(xs)):
        lo = max(0, i - window + 1)
        out.append(median(xs[lo : i + 1]))
    return out


def ewma(xs: Sequence[float], alpha: float = 0.3) -> list[float]:
    out: list[float] = []
    acc: float | None = None
    for x in xs:
        acc = x if acc is None else alpha * x + (1 - alpha) * acc
        out.append(acc)
    return out


class Slope:
    """Ordinary-least-squares slope with an uncertainty estimate.

    ``value`` is in units of y per unit of x (we always pass seconds for x and
    convert at the call site), ``stderr`` is the standard error of that slope
    and ``r2`` tells us how linear the trend actually was.  The dashboard shows
    all three so that "water is rising at 2 cm/min" can be qualified by how
    well a straight line actually fitted the data.
    """

    __slots__ = ("value", "stderr", "r2", "n", "intercept")

    def __init__(self, value: float, stderr: float, r2: float, n: int, intercept: float):
        self.value = value
        self.stderr = stderr
        self.r2 = r2
        self.n = n
        self.intercept = intercept

    def as_dict(self) -> dict:
        return {
            "value": round(self.value, 6),
            "stderr": round(self.stderr, 6),
            "r2": round(self.r2, 4),
            "n": self.n,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Slope(value={self.value:.4g}, stderr={self.stderr:.3g}, r2={self.r2:.2f}, n={self.n})"


def ols_slope(xs: Sequence[float], ys: Sequence[float]) -> Slope:
    """Least-squares fit of y = a + b*x, returning b with its standard error."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return Slope(0.0, 0.0, 0.0, n, ys[0] if ys else 0.0)
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return Slope(0.0, 0.0, 0.0, n, my)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    b = sxy / sxx
    a = my - b * mx
    ss_res = max(syy - b * sxy, 0.0)
    r2 = 1.0 - ss_res / syy if syy > 0 else 1.0
    stderr = math.sqrt(ss_res / (n - 2) / sxx) if n > 2 else 0.0
    return Slope(b, stderr, r2, n, a)


def solve_ridge(x_rows: Sequence[Sequence[float]], y: Sequence[float], l2: float = 1e-3,
                intercept_col: int | None = 0) -> list[float]:
    """Closed-form ridge regression via Gaussian elimination on (X'X + l2 I) w = X'y.

    Used by the demand forecaster.  Pure Python, but the feature count is tiny
    (<= 8), so this is both fast enough and far easier to audit than pulling a
    machine-learning dependency into a Lambda package.

    ``intercept_col`` names the bias column, which is left **unpenalised**.
    Shrinking the intercept pulls the whole fit toward predicting zero at the
    origin, and the other coefficients then grow to compensate -- in testing,
    raising l2 from 1e-9 to 100 made the slope coefficient go *up*, from 2.00
    to 2.12, which is the opposite of what regularisation is for.  Pass None
    to penalise every column.
    """
    if not x_rows:
        return []
    k = len(x_rows[0])
    a = [[0.0] * (k + 1) for _ in range(k)]
    for i in range(k):
        penalty = 0.0 if i == intercept_col else l2
        for j in range(k):
            a[i][j] = sum(r[i] * r[j] for r in x_rows) + (penalty if i == j else 0.0)
        a[i][k] = sum(r[i] * t for r, t in zip(x_rows, y))
    # Gaussian elimination with partial pivoting.
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            continue
        a[col], a[piv] = a[piv], a[col]
        pv = a[col][col]
        for j in range(col, k + 1):
            a[col][j] /= pv
        for r in range(k):
            if r == col:
                continue
            f = a[r][col]
            if f:
                for j in range(col, k + 1):
                    a[r][j] -= f * a[col][j]
    return [a[i][k] for i in range(k)]


def mae(pred: Sequence[float], truth: Sequence[float]) -> float:
    if not pred:
        return 0.0
    return sum(abs(p - t) for p, t in zip(pred, truth)) / len(pred)


def rmse(pred: Sequence[float], truth: Sequence[float]) -> float:
    if not pred:
        return 0.0
    return math.sqrt(sum((p - t) ** 2 for p, t in zip(pred, truth)) / len(pred))


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def lerp_curve(curve: Sequence[Sequence[float]], x: float) -> float:
    """Piecewise-linear interpolation over a sorted [[x, y], ...] lookup table.

    This is how an irregular tank's level is mapped to a volume: you dip the
    tank at known levels, record the volume, and the curve does the rest.
    """
    if not curve:
        return 0.0
    pts = sorted((float(p[0]), float(p[1])) for p in curve)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]
