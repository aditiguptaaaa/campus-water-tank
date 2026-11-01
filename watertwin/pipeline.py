"""The running system for one tank: feed in readings, get out alerts.

Both the live API and the validation harness drive this same object, so
what is measured in validation is exactly what runs in the demo.
"""
from dataclasses import dataclass, field

from .alerts import LEAK, OVERFLOW, SHORTFALL, Alert, AlertManager
from .detect import LeakDetector, check_overflow, forecast_shortfall
from .interface import Reading, TankSpec
from .store import Store
from .twin import Baseline, DEFAULT_WINDOW_MIN, RateSample, Twin


@dataclass
class Params:
    """Everything the threshold sweep tunes, in one place."""
    k_lpm: float = 0.5
    h_litres: float = 45.0
    k_mad_mult: float = 1.0
    window_min: int = DEFAULT_WINDOW_MIN
    cooloff_s: int = 6 * 3600
    shortfall_warn_min: float = 180.0   # the lead time SC-4 asks for
    # Fraction of the learned pump-start level below which a refill counts
    # as overdue. It must sit under the pump's real cut-in, which the
    # learned median overshoots by about one step of inflow -- the level is
    # recorded at the first reading that shows the pump already running.
    # Higher means more warning, so it is set just under that bound, not
    # far under: the warning time is (trigger - reserve) / demand, so the
    # margin given away here is lead time the maintenance team never gets.
    refill_overdue_frac: float = 0.92
    horizon_min: int = 720


class Pipeline:
    def __init__(self, spec: TankSpec, baseline: Baseline,
                 params: Params | None = None, store: Store | None = None):
        self.spec = spec
        self.baseline = baseline
        self.p = params or Params()
        self.store = store
        self.twin = Twin(spec, self.p.window_min)
        self.leak = LeakDetector(baseline, self.p.k_lpm, self.p.h_litres,
                                 self.p.k_mad_mult)
        self.alerts = AlertManager(self.p.cooloff_s)
        self.last_sample: RateSample | None = None
        self.last_forecast = None
        self.raised: list[Alert] = []

    # --- the one entry point -------------------------------------------
    def feed(self, r: Reading) -> list[Alert]:
        new: list[Alert] = []
        sample = self.twin.update(r)

        new += self._overflow()
        if sample is not None:
            self.last_sample = sample
            new += self._leak(sample)
            new += self._shortfall()

        self.raised += new
        if self.store is not None:
            for a in new:
                a.id = self.store.put_alert(a)
        return new

    # --- the three conditions ------------------------------------------
    def _leak(self, s: RateSample) -> list[Alert]:
        d = self.leak.update(s)
        if self.store is not None:
            self.store.put_rate(self.spec.tank_id, s.ts, s.drain_lpm,
                                d.expected_lpm, d.residual_lpm, d.cusum,
                                s.pumping)
        if not d.firing:
            self.alerts.clear(self.spec.tank_id, LEAK)
            return []
        a = self.alerts.raise_(self.spec.tank_id, LEAK, d.onset_ts, s.ts,
                               d.magnitude_lpm, d.confidence,
                               f"accumulated {d.cusum:.0f} L unexplained")
        return [a] if a else []

    def _shortfall(self) -> list[Alert]:
        leak_lpm = self.leak.magnitude() if self.leak._firing else 0.0
        f = forecast_shortfall(self.twin, self.baseline, leak_lpm,
                               self.p.horizon_min, self.p.window_min)
        self.last_forecast = f

        # Only a refill that should have happened and did not is a shortfall.
        # Learned pump-start level, so this needs no configured set point.
        # Meaningfully below the level where the pump normally starts --
        # the refill is overdue, not merely approaching.
        trigger = self.baseline.pump_start_l * self.p.refill_overdue_frac
        refill_overdue = (self.baseline.pump_start_l > 0
                          and self.twin.volume_l < trigger)
        # Fire as soon as the refill is overdue, not once the forecast has
        # counted down to the warning window. Waiting for the countdown
        # caps the warning at the window length and throws away the lead
        # time the alert exists to provide.
        firing = (not self.twin.pump_on and refill_overdue
                  and f.lead_minutes is not None
                  and f.lead_minutes <= self.p.horizon_min)
        if not firing:
            self.alerts.clear(self.spec.tank_id, SHORTFALL)
            return []
        detail = (f"At {self.twin.volume_l:.0f} L, below the {self.baseline.pump_start_l:.0f} L "
                  f"level where this pump usually starts.")
        a = self.alerts.raise_(self.spec.tank_id, SHORTFALL, self.twin.ts,
                               self.twin.ts, f.lead_minutes, 0.8, detail)
        return [a] if a else []

    def _overflow(self) -> list[Alert]:
        sig = check_overflow(self.twin, self.baseline, self.last_sample)
        if not sig.firing:
            self.alerts.clear(self.spec.tank_id, OVERFLOW)
            return []
        a = self.alerts.raise_(self.spec.tank_id, OVERFLOW, self.twin.ts,
                               self.twin.ts, sig.minutes_to_full or 0.0,
                               0.95, sig.reason)
        return [a] if a else []
