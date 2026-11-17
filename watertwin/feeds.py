"""The historical feed: readings replayed from a CSV file.

Real campus history arrives as a spreadsheet export, not a live stream, so
this is what a baseline would actually be trained from before any sensor is
fitted. It is also the second implementation of `DataSource`, which is what
makes the interface's claim testable rather than asserted: the twin cannot
tell this apart from the simulator, and `test_feeds_are_interchangeable`
proves it by feeding both into identical twins and comparing.

Tank geometry is configuration, not data, so the spec is supplied by the
caller rather than carried in the file.
"""
from __future__ import annotations
import csv
from pathlib import Path
from typing import Iterable, Iterator

from .interface import DataSource, Reading, TankSpec

FIELDS = ("tank_id", "ts", "level_m", "pump_on")


def write_csv(path: str | Path, readings: Iterable[Reading]) -> int:
    """Capture a reading stream to disk. Returns the number of rows written."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for r in readings:
            w.writerow((r.tank_id, r.ts, repr(r.level_m), int(r.pump_on)))
            n += 1
    return n


class CsvSource(DataSource):
    """Replays readings from a CSV written by `write_csv` or exported by hand.

    Rows are yielded in file order after a sort by timestamp, and rows for
    other tanks are skipped, so one export covering a whole campus can feed
    one tank's twin.
    """

    def __init__(self, spec: TankSpec, path: str | Path):
        self.spec = spec
        self.path = Path(path)

    def tanks(self) -> list[TankSpec]:
        return [self.spec]

    def readings(self) -> Iterator[Reading]:
        with open(self.path, newline="", encoding="utf-8") as fh:
            rows = []
            for row in csv.DictReader(fh):
                if row["tank_id"] != self.spec.tank_id:
                    continue
                rows.append(Reading(row["tank_id"], int(row["ts"]),
                                    float(row["level_m"]),
                                    row["pump_on"] in ("1", "True", "true")))
        yield from sorted(rows, key=lambda r: r.ts)


class StoredSource(DataSource):
    """Replays what the store already holds. The same contract, from SQLite."""

    def __init__(self, spec: TankSpec, store, limit: int = 1_000_000):
        self.spec = spec
        self.store = store
        self.limit = limit

    def tanks(self) -> list[TankSpec]:
        return [self.spec]

    def readings(self) -> Iterator[Reading]:
        for row in self.store.recent_readings(self.spec.tank_id, self.limit):
            yield Reading(row["tank_id"], row["ts"], row["level_m"],
                          bool(row["pump_on"]))
