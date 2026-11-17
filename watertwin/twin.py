"""The twin core: mass balance plus the learned expectation of behaviour.

The twin holds what the tank IS (state, from the feed) and what it SHOULD BE
(the baseline). Everything downstream works on the difference between them.
"""
from __future__ import annotations
import statistics
from dataclasses import dataclass
from typing import Iterable

from .interface import Reading, TankSpec
from .simulator import hour_of_week

SLOTS = 168                 # hours in a week
DEFAULT_WINDOW_MIN = 10     # rates are measured over a window, not per reading


@dataclass(frozen=True)
class RateSample:
    """Drain rate over one window. Positive means the tank is emptying."""
    ts: int
    drain_lpm: float
    pumping: bool           # pump ran at any point in the window
    minutes: float
    gap: bool               # window stretched by missing readings


class Baseline:
    """Expected behaviour, learned from history.

    Consumption by hour of week as a median, not a mean: one overflow or
    data gap should not move the expectation. The spread is a MAD, which
    sets the detector's slack. The pump's own start level is learned the
    same way, so a refill that fails to happen is visible as such.
    """

    def __init__(self) -> None:
        self._obs: list[list[float]] = [[] for _ in range(SLOTS)]
        self._pump_starts: list[float] = []
        self._pump_stops: list[float] = []
        self.slot_median: list[float] = [0.0] * SLOTS
        self.slot_mad: list[float] = [0.0] * SLOTS
        self.global_median = 0.0
        self.global_mad = 0.0
        self.pump_start_l = 0.0     # 0 = never seen the pump start
        self.pump_stop_l = 0.0      # where the float normally cuts it
        self.trained = False

    def observe(self, s: RateSample) -> None:
        """Only clean, pump-free windows teach the consumption baseline."""
        if s.pumping or s.gap:
            return
        self._obs[hour_of_week(s.ts)].append(s.drain_lpm)

    def observe_pump_start(self, volume_l: float) -> None:
        self._pump_starts.append(volume_l)

    def observe_pump_stop(self, volume_l: float) -> None:
        self._pump_stops.append(volume_l)

    def fit(self) -> "Baseline":
        everything = [v for slot in self._obs for v in slot]
        if not everything:
            raise ValueError("baseline has no observations to fit")
        self.global_median = statistics.median(everything)
        self.global_mad = _mad(everything, self.global_median)
        for i, slot in enumerate(self._obs):
            if len(slot) >= 3:
                self.slot_median[i] = statistics.median(slot)
                self.slot_mad[i] = _mad(slot, self.slot_median[i])
            else:
                # Thin slot: fall back rather than trust two points.
                self.slot_median[i] = self.global_median
                self.slot_mad[i] = self.global_mad
        if self._pump_starts:
            self.pump_start_l = statistics.median(self._pump_starts)
        if self._pump_stops:
            self.pump_stop_l = statistics.median(self._pump_stops)
        self.trained = True
        return self

    def expected(self, ts: int) -> float:
        return self.slot_median[hour_of_week(ts)]

    def spread(self, ts: int) -> float:
        return max(self.slot_mad[hour_of_week(ts)], 1e-6)

    def coverage(self) -> float:
        """Share of slots backed by real observations, not the fallback."""
        return sum(1 for s in self._obs if len(s) >= 3) / SLOTS


def _mad(xs: list[float], centre: float) -> float:
    """Median absolute deviation, scaled to compare with a standard deviation."""
    if len(xs) < 2:
        return 0.0
    return 1.4826 * statistics.median([abs(x - centre) for x in xs])


class Twin:
    """Live state of one tank, and the rate samples the detectors consume."""

    def __init__(self, spec: TankSpec, window_min: int = DEFAULT_WINDOW_MIN):
        self.spec = spec
        self.window_min = window_min
        self.ts = 0
        self.level_m = 0.0
        self.volume_l = 0.0
        self.pump_on = False
        self.prev_pump: bool | None = None
        self._w_ts = 0
        self._w_vol = 0.0
        self._w_pump = False
        self._started = False

    @property
    def fill_fraction(self) -> float:
        return self.volume_l / self.spec.capacity_l

    def update(self, r: Reading) -> RateSample | None:
        """Absorb one reading. Returns a rate sample once a window closes."""
        v = self.spec.volume_l(r.level_m)
        self.prev_pump = self.pump_on if self._started else None
        self.ts, self.level_m, self.volume_l = r.ts, r.level_m, v
        self.pump_on = r.pump_on

        if not self._started:
            self._w_ts, self._w_vol, self._w_pump = r.ts, v, r.pump_on
            self._started = True
            return None

        self._w_pump = self._w_pump or r.pump_on
        minutes = (r.ts - self._w_ts) / 60.0
        if minutes < self.window_min:
            return None

        # Mass balance over the window: what left the tank, per minute.
        drain = (self._w_vol - v) / minutes
        gap = minutes > self.window_min * 3
        s = RateSample(r.ts, drain, self._w_pump, minutes, gap)
        self._w_ts, self._w_vol, self._w_pump = r.ts, v, r.pump_on
        return s

    @property
    def pump_just_started(self) -> bool:
        return self.prev_pump is False and self.pump_on

    @property
    def pump_just_stopped(self) -> bool:
        return self.prev_pump is True and not self.pump_on


def train_baseline(spec: TankSpec, readings: Iterable[Reading],
                   window_min: int = DEFAULT_WINDOW_MIN) -> Baseline:
    """Fit a baseline from a history of readings. Used before detection runs."""
    b = Baseline()
    twin = Twin(spec, window_min)
    for r in readings:
        s = twin.update(r)
        if twin.pump_just_started:
            b.observe_pump_start(twin.volume_l)
        if twin.pump_just_stopped:
            b.observe_pump_stop(twin.volume_l)
        if s is not None:
            b.observe(s)
    return b.fit()
