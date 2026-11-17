"""Detection: a leak is a sustained excess in drain rate, not a low tank.

The leak detector accumulates the residual between observed and expected
drain rate with a one-sided CUSUM. A single high reading means nothing --
demand is noisy -- but a small persistent excess accumulates until it is
unambiguous. That is what finds a 1 L/min leak that no per-reading
threshold would ever trip.
"""
from __future__ import annotations
from dataclasses import dataclass

from .interface import TankSpec
from .simulator import hour_of_week
from .twin import Baseline, RateSample, Twin


@dataclass
class Detection:
    """One detector step. Logged every window, firing only sometimes."""
    ts: int
    expected_lpm: float
    residual_lpm: float
    cusum: float
    firing: bool
    onset_ts: int
    magnitude_lpm: float
    confidence: float


class LeakDetector:
    """One-sided CUSUM on the drain-rate residual.

        S_t = max(0, S_{t-1} + (r_t - k) * minutes)

    k is slack: the excess we refuse to treat as evidence, set just above
    the slot's own noise. h is the threshold, in litres of accumulated
    unexplained loss. Expected detection time for a leak of q L/min is
    roughly h / (q - k) minutes.
    """

    def __init__(self, baseline: Baseline, k_lpm: float = 0.5,
                 h_litres: float = 45.0, k_mad_mult: float = 1.0):
        self.baseline = baseline
        self.k_lpm = k_lpm
        self.h = h_litres
        self.k_mad_mult = k_mad_mult
        self.cusum = 0.0
        self.onset_ts = 0
        self._excess_l = 0.0        # unexplained litres since onset
        self._minutes = 0.0
        self._firing = False

    def slack(self, ts: int) -> float:
        """Refuse to count noise: slack is the floor or the slot's spread."""
        return max(self.k_lpm, self.k_mad_mult * self.baseline.spread(ts))

    def reset(self) -> None:
        self.cusum = 0.0
        self.onset_ts = 0
        self._excess_l = 0.0
        self._minutes = 0.0
        self._firing = False

    def update(self, s: RateSample) -> Detection:
        expected = self.baseline.expected(s.ts)

        # Inflow dominates while the pump runs, and a gap leaves the window
        # unassessable. Neither is evidence either way: hold, do not reset.
        if s.pumping or s.gap:
            return Detection(s.ts, expected, 0.0, self.cusum, self._firing,
                             self.onset_ts, self.magnitude(), self.confidence())

        residual = s.drain_lpm - expected
        prev = self.cusum
        self.cusum = max(0.0, self.cusum + (residual - self.slack(s.ts)) * s.minutes)

        if prev <= 0.0 and self.cusum > 0.0:
            self.onset_ts = s.ts            # the excess starts accumulating here
            self._excess_l = 0.0
            self._minutes = 0.0
        if self.cusum <= 0.0:
            self.onset_ts = 0
            self._excess_l = 0.0
            self._minutes = 0.0
            self._firing = False
        else:
            self._excess_l += residual * s.minutes
            self._minutes += s.minutes

        if self.cusum > self.h:
            self._firing = True

        return Detection(s.ts, expected, residual, self.cusum, self._firing,
                         self.onset_ts, self.magnitude(), self.confidence())

    def magnitude(self) -> float:
        """Estimated loss rate: unexplained litres over the time they took."""
        return self._excess_l / self._minutes if self._minutes > 0 else 0.0

    def confidence(self) -> float:
        if self.cusum <= self.h:
            return 0.0
        return min(0.99, 0.5 + 0.4 * (self.cusum - self.h) / self.h)


@dataclass
class Forecast:
    """Projected trajectory with the pump left off -- a 'if you do nothing'."""
    dry_ts: int | None              # when the reserve is crossed
    lead_minutes: float | None
    points: list[tuple[int, float]]  # (ts, litres)


def forecast_shortfall(twin: Twin, baseline: Baseline, leak_lpm: float = 0.0,
                       horizon_min: int = 720, step_min: int = 10) -> Forecast:
    """Integrate expected demand forward from the tank's present state.

    The pump is assumed off, which makes this the useful question: how long
    until this tank cannot serve the building unless somebody acts.
    """
    v = twin.volume_l
    ts = twin.ts
    pts: list[tuple[int, float]] = [(ts, v)]
    dry: int | None = None
    for _ in range(max(1, horizon_min // step_min)):
        ts += step_min * 60
        v -= (baseline.expected(ts) + leak_lpm) * step_min
        v = max(0.0, v)
        pts.append((ts, v))
        if dry is None and v <= twin.spec.reserve_l:
            dry = ts
    lead = (dry - twin.ts) / 60.0 if dry else None
    return Forecast(dry, lead, pts)


@dataclass
class OverflowSignal:
    firing: bool
    minutes_to_full: float | None
    reason: str


def check_overflow(twin: Twin, baseline: Baseline,
                   last: RateSample | None = None,
                   near_full: float = 0.98) -> OverflowSignal:
    """Overflow needs no learned model of demand: full, water still coming in.

    The test is against the level where this pump's float normally cuts out,
    learned the same way as everything else. Filling well past that point is
    the stuck float -- the exact failure that defeats the mechanical control
    this system is meant to back up. While the pump runs the drain rate is
    negative, so -drain is the net fill rate and time to the brim follows.
    """
    if not twin.pump_on:
        return OverflowSignal(False, None, "")

    mins = None
    if last is not None and last.pumping and last.drain_lpm < -0.5:
        mins = (twin.spec.capacity_l - twin.volume_l) / (-last.drain_lpm)

    if twin.fill_fraction >= near_full:
        return OverflowSignal(True, mins,
                              "at capacity with the pump still running")

    stop = baseline.pump_stop_l
    if stop > 0 and twin.volume_l > max(stop * 1.02, stop + 50.0):
        reason = (f"filling {twin.volume_l - stop:.0f} L past the {stop:.0f} L "
                  f"level where this pump usually stops")
        if mins is not None:
            reason += f", full in {mins:.0f} min"
        return OverflowSignal(True, mins, reason)

    return OverflowSignal(False, None, "")
