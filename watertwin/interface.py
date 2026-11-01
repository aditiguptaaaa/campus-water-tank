"""The data-source interface: one contract every feed satisfies.

Phase 1 feeds (simulated, historical) and Phase 2 feeds (ultrasonic sensors)
all yield the same Reading stream, so nothing above this module knows which
is attached. Swapping in real hardware is a new DataSource, not a redesign.
"""
from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True)
class TankSpec:
    """Physical description of one overhead tank."""
    tank_id: str
    capacity_l: float
    area_m2: float          # horizontal cross-section: level <-> volume
    reserve_l: float        # below this, supply to the building is at risk
    label: str = ""

    @property
    def full_level_m(self) -> float:
        return self.capacity_l / (self.area_m2 * 1000.0)

    def volume_l(self, level_m: float) -> float:
        return max(0.0, min(level_m * self.area_m2 * 1000.0, self.capacity_l))

    def level_m(self, volume_l: float) -> float:
        return volume_l / (self.area_m2 * 1000.0)


@dataclass(frozen=True)
class Reading:
    """One observation of a tank. The only thing that crosses the interface."""
    tank_id: str
    ts: int                 # unix seconds
    level_m: float
    pump_on: bool


class DataSource(Protocol):
    """Every feed implements exactly this."""

    def tanks(self) -> list[TankSpec]:
        ...

    def readings(self) -> Iterator[Reading]:
        ...
