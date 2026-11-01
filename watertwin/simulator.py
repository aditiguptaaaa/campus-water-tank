"""Simulated feed: tank physics forward in time, with injectable faults.

Written from the mass balance, not from the detector's assumptions, so that
detection measured against it means something. Every fault records its true
onset, which is the ground truth the validation harness scores against.
"""
import math
import random
from dataclasses import dataclass, field
from typing import Iterator

from .interface import DataSource, Reading, TankSpec

MINUTE = 60

# Litres per minute by hour of day for a hostel block: morning and evening
# peaks, a quiet night. Daily total ~5,600 L, about 50 residents.
HOSTEL_PROFILE = [
    0.8, 0.5, 0.4, 0.4, 0.5, 1.5, 4.0, 9.0, 10.0, 7.0, 4.0, 3.0,
    4.0, 4.5, 3.0, 2.5, 3.0, 4.0, 5.5, 7.0, 7.5, 6.0, 3.5, 1.5,
]

# An academic block: one broad daytime plateau, near-zero at night.
ACADEMIC_PROFILE = [
    0.2, 0.2, 0.1, 0.1, 0.1, 0.3, 1.0, 2.5, 5.0, 6.0, 6.5, 6.0,
    7.0, 6.5, 6.0, 5.5, 5.0, 3.5, 2.0, 1.2, 0.8, 0.6, 0.4, 0.3,
]


@dataclass
class Fault:
    """A fault to inject, with the ground truth needed to score detection."""
    kind: str               # leak | stuck_float | pump_fail |
                            # dropout | demand_spike
    onset_ts: int
    end_ts: int = 0         # 0 = runs to the end of the simulation
    rate_lpm: float = 0.0   # leak rate, or extra demand for a spike

    def active(self, ts: int) -> bool:
        return ts >= self.onset_ts and (self.end_ts == 0 or ts < self.end_ts)


@dataclass
class SimConfig:
    """Everything that shapes a run. Seeded, so a run is reproducible."""
    spec: TankSpec
    start_ts: int
    days: float = 7.0
    step_s: int = MINUTE
    profile: list[float] = field(default_factory=lambda: list(HOSTEL_PROFILE))
    weekend_factor: float = 1.10
    noise_sigma: float = 0.18        # multiplicative, on demand
    burst_chance: float = 0.004      # per step: a short legitimate surge
    pump_lpm: float = 60.0
    pump_on_frac: float = 0.35       # start filling below this fraction
    pump_off_frac: float = 0.95      # stop filling at this fraction
    level_quantum_m: float = 0.0     # 0 = ideal; 0.01 = 1 cm sensor (Phase 2)
    seed: int = 1
    faults: list[Fault] = field(default_factory=list)


class SimulatedSource(DataSource):
    """A DataSource that integrates the mass balance forward in time."""

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.overflow_l = 0.0
        self.leaked_l = 0.0
        self.dry_minutes = 0

    def tanks(self) -> list[TankSpec]:
        return [self.cfg.spec]

    def _demand_lpm(self, ts: int, rng: random.Random) -> float:
        """Legitimate consumption: profile, weekday shape, noise, bursts."""
        c = self.cfg
        tm = _civil(ts)
        base = c.profile[tm.hour]
        if tm.weekday >= 5:
            # Later, flatter weekend morning: shift the peak two hours on.
            base = c.profile[(tm.hour - 2) % 24] * c.weekend_factor
        d = base * max(0.0, 1.0 + rng.gauss(0.0, c.noise_sigma))
        if rng.random() < c.burst_chance:
            d += rng.uniform(4.0, 14.0)     # someone fills a drum
        return d

    def readings(self) -> Iterator[Reading]:
        c = self.cfg
        rng = random.Random(c.seed)
        spec = c.spec
        volume = spec.capacity_l * 0.80
        pump = False
        steps = int(c.days * 24 * 3600 / c.step_s)
        dt_min = c.step_s / 60.0

        for i in range(steps):
            ts = c.start_ts + i * c.step_s
            stuck = any(f.kind == "stuck_float" and f.active(ts) for f in c.faults)
            dead = any(f.kind == "pump_fail" and f.active(ts) for f in c.faults)

            # --- pump control (open loop, as on a real roof) ---
            if pump:
                if dead or (volume >= spec.capacity_l * c.pump_off_frac
                            and not stuck):
                    pump = False
            elif volume <= spec.capacity_l * c.pump_on_frac and not dead:
                pump = True

            # --- flows over this step ---
            demand = self._demand_lpm(ts, rng)
            for f in c.faults:
                if f.kind == "demand_spike" and f.active(ts):
                    demand += f.rate_lpm
            leak = sum(f.rate_lpm for f in c.faults
                       if f.kind == "leak" and f.active(ts))
            inflow = c.pump_lpm if pump else 0.0

            volume += (inflow - demand - leak) * dt_min
            self.leaked_l += leak * dt_min

            if volume > spec.capacity_l:        # over the top and down the wall
                self.overflow_l += volume - spec.capacity_l
                volume = spec.capacity_l
            if volume < 0.0:
                volume = 0.0
            if volume <= 0.0:
                self.dry_minutes += dt_min

            if any(f.kind == "dropout" and f.active(ts) for f in c.faults):
                continue                        # sensor silent: no reading

            level = spec.level_m(volume)
            if c.level_quantum_m > 0:
                level = math.floor(level / c.level_quantum_m) * c.level_quantum_m
            yield Reading(spec.tank_id, ts, level, pump)


@dataclass(frozen=True)
class _Civil:
    hour: int
    weekday: int


def _civil(ts: int) -> _Civil:
    """Hour and weekday in UTC. Deliberately not locale-dependent."""
    days, rem = divmod(ts, 86400)
    return _Civil(hour=rem // 3600, weekday=int((days + 4) % 7))


def hour_of_week(ts: int) -> int:
    """0..167 -- the baseline's slot index."""
    tm = _civil(ts)
    return tm.weekday * 24 + tm.hour
