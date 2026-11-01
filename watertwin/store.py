"""Persistence. SQLite because one tank-year is ~525k rows, well inside it.

The schema is plain SQL with no SQLite-only types, so the Phase 2 move to
PostgreSQL is a connection change rather than a rewrite.
"""
import sqlite3
from typing import Iterable

from .interface import Reading, TankSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS tanks (
    tank_id    TEXT PRIMARY KEY,
    capacity_l REAL NOT NULL,
    area_m2    REAL NOT NULL,
    reserve_l  REAL NOT NULL,
    label      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS readings (
    tank_id  TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    level_m  REAL NOT NULL,
    pump_on  INTEGER NOT NULL,
    PRIMARY KEY (tank_id, ts)
);
CREATE TABLE IF NOT EXISTS rates (
    tank_id   TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    drain_lpm REAL NOT NULL,
    expected_lpm REAL,
    residual_lpm REAL,
    cusum     REAL,
    pumping   INTEGER NOT NULL,
    PRIMARY KEY (tank_id, ts)
);
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tank_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    onset_ts  INTEGER NOT NULL,
    raised_ts INTEGER NOT NULL,
    magnitude REAL NOT NULL,
    confidence REAL NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    outcome   TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS ix_readings_ts ON readings (tank_id, ts);
CREATE INDEX IF NOT EXISTS ix_rates_ts ON rates (tank_id, ts);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- writes ---------------------------------------------------------
    def put_tank(self, s: TankSpec) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tanks VALUES (?,?,?,?,?)",
            (s.tank_id, s.capacity_l, s.area_m2, s.reserve_l, s.label),
        )
        self.db.commit()

    def put_readings(self, rs: Iterable[Reading]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO readings VALUES (?,?,?,?)",
            [(r.tank_id, r.ts, r.level_m, int(r.pump_on)) for r in rs],
        )
        self.db.commit()

    def put_rate(self, tank_id, ts, drain, expected, residual, cusum, pumping):
        self.db.execute(
            "INSERT OR REPLACE INTO rates VALUES (?,?,?,?,?,?,?)",
            (tank_id, ts, drain, expected, residual, cusum, int(pumping)),
        )

    def put_alert(self, a) -> int:
        cur = self.db.execute(
            "INSERT INTO alerts (tank_id,kind,onset_ts,raised_ts,magnitude,"
            "confidence,detail,outcome) VALUES (?,?,?,?,?,?,?,?)",
            (a.tank_id, a.kind, a.onset_ts, a.raised_ts, a.magnitude,
             a.confidence, a.detail, a.outcome),
        )
        self.db.commit()
        return cur.lastrowid

    def set_outcome(self, alert_id: int, outcome: str) -> None:
        self.db.execute("UPDATE alerts SET outcome=? WHERE id=?",
                        (outcome, alert_id))
        self.db.commit()

    def commit(self) -> None:
        self.db.commit()

    # --- reads ----------------------------------------------------------
    def tanks(self) -> list[TankSpec]:
        return [TankSpec(r["tank_id"], r["capacity_l"], r["area_m2"],
                         r["reserve_l"], r["label"])
                for r in self.db.execute("SELECT * FROM tanks")]

    def recent_rates(self, tank_id: str, limit: int = 300) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM rates WHERE tank_id=? ORDER BY ts DESC LIMIT ?",
            (tank_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_readings(self, tank_id: str, limit: int = 1440) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM readings WHERE tank_id=? ORDER BY ts DESC LIMIT ?",
            (tank_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def alerts(self, tank_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM alerts"
        args: tuple = ()
        if tank_id:
            sql += " WHERE tank_id=?"
            args = (tank_id,)
        sql += " ORDER BY raised_ts DESC"
        return [dict(r) for r in self.db.execute(sql, args)]
