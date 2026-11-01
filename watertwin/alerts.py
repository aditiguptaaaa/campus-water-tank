"""Alert management: turning detector output into something worth reading.

A detector firing every window is not an alert stream, it is noise. A
maintenance team that gets paged ten times for one leak stops reading the
pages, and then detection accuracy stops mattering. This module is what
keeps the system usable: one condition, one alert, with the evidence a
technician needs to start looking.
"""
import time
from dataclasses import dataclass, field

LEAK = "leak"
SHORTFALL = "shortfall"
OVERFLOW = "overflow"

DEFAULT_COOLOFF_S = 6 * 3600


@dataclass
class Alert:
    tank_id: str
    kind: str
    onset_ts: int           # when the condition began, not when we noticed
    raised_ts: int
    magnitude: float        # L/min for a leak, minutes of lead for others
    confidence: float
    detail: str = ""
    outcome: str = "open"   # open | confirmed | false | resolved
    id: int | None = None

    def message(self) -> str:
        """Worded as a suspicion with its evidence. Never as a fact."""
        if self.kind == LEAK:
            lost = self.magnitude * (self.raised_ts - self.onset_ts) / 60.0
            return (f"Suspected leak on {self.tank_id}: losing about "
                    f"{self.magnitude:.1f} L/min more than this tank's own "
                    f"history explains, since {_clock(self.onset_ts)} "
                    f"(~{lost:.0f} L so far). Confidence {self.confidence:.0%}.")
        if self.kind == SHORTFALL:
            return (f"{self.tank_id} is forecast to fall below its reserve in "
                    f"about {self.magnitude:.0f} minutes and the pump is not "
                    f"running. {self.detail}")
        if self.kind == OVERFLOW:
            return f"{self.tank_id} is overflowing or about to: {self.detail}"
        return f"{self.tank_id}: {self.kind}"


def _clock(ts: int) -> str:
    return time.strftime("%a %H:%M", time.gmtime(ts))


@dataclass
class AlertManager:
    """De-duplicates conditions and holds a cool-off window.

    One unresolved leak produces one alert, not one per ten minutes. A
    condition that clears and returns raises again only after the cool-off,
    so a detector flickering around its threshold cannot spam anyone.
    """
    cooloff_s: int = DEFAULT_COOLOFF_S
    open: dict[tuple[str, str], Alert] = field(default_factory=dict)
    history: list[Alert] = field(default_factory=list)
    _last_raised: dict[tuple[str, str], int] = field(default_factory=dict)

    def raise_(self, tank_id: str, kind: str, onset_ts: int, now_ts: int,
               magnitude: float, confidence: float, detail: str = "") -> Alert | None:
        """Returns the new alert, or None if it was suppressed."""
        key = (tank_id, kind)
        if key in self.open:
            return None                                  # already known
        last = self._last_raised.get(key)
        if last is not None and now_ts - last < self.cooloff_s:
            return None                                  # inside cool-off
        a = Alert(tank_id, kind, onset_ts, now_ts, magnitude, confidence, detail)
        self.open[key] = a
        self._last_raised[key] = now_ts
        self.history.append(a)
        return a

    def clear(self, tank_id: str, kind: str) -> Alert | None:
        """The condition has gone away. Does not reset the cool-off."""
        a = self.open.pop((tank_id, kind), None)
        if a is not None and a.outcome == "open":
            a.outcome = "resolved"
        return a

    def feedback(self, alert: Alert, outcome: str) -> None:
        """A user marks an alert confirmed or a false alarm. Kept for review."""
        if outcome not in ("confirmed", "false", "resolved"):
            raise ValueError(f"unknown outcome: {outcome}")
        alert.outcome = outcome
        if outcome in ("false", "resolved"):
            self.open.pop((alert.tank_id, alert.kind), None)
        if outcome == "false":
            # A false alarm should not also silence the next six hours.
            self._last_raised.pop((alert.tank_id, alert.kind), None)

    def open_alerts(self) -> list[Alert]:
        return list(self.open.values())
