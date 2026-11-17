"""Read API and dashboard host, on the standard library's HTTP server.

No framework: the dashboard needs a handful of JSON endpoints and one static
page, and a dependency-free server is one that still starts at the demo.

    python -m watertwin.api            serve on :8000, replaying a live feed
    python -m watertwin.api --port 9000 --speed 240
"""
from __future__ import annotations
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .alerts import Alert
from .interface import TankSpec
from .pipeline import Params, Pipeline
from .simulator import (ACADEMIC_PROFILE, Fault, HOSTEL_PROFILE, SimConfig,
                        SimulatedSource)
from .store import Store
from .twin import train_baseline

STATIC = Path(__file__).parent / "static"

# The operating point chosen by the threshold sweep in validate.py.
TUNED = Params(k_lpm=0.3, h_litres=60.0)


class Engine:
    """Drives the pipelines from their feeds and holds the shared state."""

    def __init__(self, tanks: list[tuple[TankSpec, Pipeline, object]],
                 store: Store):
        self.store = store
        self.lock = threading.Lock()
        self.sim_ts = 0
        self.alerts_by_id: dict[int, Alert] = {}
        self._tanks = {}
        for spec, pipe, source in tanks:
            self._tanks[spec.tank_id] = {
                "spec": spec, "pipe": pipe, "feed": iter(source.readings()),
                "done": False,
            }
            store.put_tank(spec)

    @property
    def specs(self) -> list[TankSpec]:
        return [t["spec"] for t in self._tanks.values()]

    def step(self) -> bool:
        """One reading for every tank. False when every feed is exhausted."""
        alive = False
        with self.lock:
            for t in self._tanks.values():
                if t["done"]:
                    continue
                try:
                    r = next(t["feed"])
                except StopIteration:
                    t["done"] = True
                    continue
                alive = True
                self.sim_ts = max(self.sim_ts, r.ts)
                self.store.put_readings([r])
                for a in t["pipe"].feed(r):
                    if a.id is not None:
                        self.alerts_by_id[a.id] = a
        return alive

    # --- what the dashboard reads --------------------------------------
    def state(self) -> dict:
        with self.lock:
            tanks = []
            for t in self._tanks.values():
                spec, pipe = t["spec"], t["pipe"]
                twin, fc = pipe.twin, pipe.last_forecast
                tanks.append({
                    "tank_id": spec.tank_id,
                    "label": spec.label,
                    "capacity_l": spec.capacity_l,
                    "reserve_l": spec.reserve_l,
                    "volume_l": round(twin.volume_l, 1),
                    "level_m": round(twin.level_m, 3),
                    "fill": round(twin.fill_fraction, 4),
                    "pump_on": twin.pump_on,
                    "cusum": round(pipe.leak.cusum, 1),
                    "cusum_threshold": pipe.leak.h,
                    "leak_estimate_lpm": round(pipe.leak.magnitude(), 2),
                    "dry_in_min": (round(fc.lead_minutes)
                                   if fc and fc.lead_minutes else None),
                    "expected_lpm": round(pipe.baseline.expected(twin.ts), 2),
                    "observed_lpm": (round(pipe.last_sample.drain_lpm, 2)
                                     if pipe.last_sample else None),
                })
            return {"sim_ts": self.sim_ts, "tanks": tanks,
                    "alerts": self.store.alerts()}

    def series(self, tank_id: str, limit: int = 288) -> dict:
        with self.lock:
            t = self._tanks.get(tank_id)
            if t is None:
                return {}
            spec, pipe = t["spec"], t["pipe"]
            self.store.commit()
            rates = self.store.recent_rates(tank_id, limit)
            fc = pipe.last_forecast
            return {
                "tank_id": tank_id,
                "capacity_l": spec.capacity_l,
                "reserve_l": spec.reserve_l,
                "rates": [{"ts": r["ts"], "observed": r["drain_lpm"],
                           "expected": r["expected_lpm"],
                           "residual": r["residual_lpm"],
                           "cusum": r["cusum"], "pumping": bool(r["pumping"])}
                          for r in rates],
                "readings": [{"ts": r["ts"],
                              "volume_l": round(spec.volume_l(r["level_m"]), 1)}
                             for r in self.store.recent_readings(tank_id, limit * 10)[::10]],
                "forecast": [{"ts": ts, "volume_l": round(v, 1)}
                             for ts, v in (fc.points if fc else [])],
                "threshold": pipe.leak.h,
            }

    def feedback(self, alert_id: int, outcome: str) -> bool:
        with self.lock:
            a = self.alerts_by_id.get(alert_id)
            if a is None:
                return False
            for t in self._tanks.values():
                if t["spec"].tank_id == a.tank_id:
                    t["pipe"].alerts.feedback(a, outcome)
            self.store.set_outcome(alert_id, outcome)
            return True


class Handler(BaseHTTPRequestHandler):
    engine: Engine = None       # set by serve()

    def log_message(self, *a):  # keep the console readable
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            page = STATIC / "index.html"
            return self._send(page.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._json(self.engine.state())
        if u.path == "/api/series":
            q = parse_qs(u.query)
            tank = (q.get("tank") or [""])[0]
            data = self.engine.series(tank)
            return self._json(data) if data else self._json({"error": "no such tank"}, 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path != "/api/feedback":
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            ok = self.engine.feedback(int(body["id"]), str(body["outcome"]))
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({"ok": ok}, 200 if ok else 404)


def build_demo(days: float = 6.0, with_leak: bool = True) -> Engine:
    """Two tanks, baselines trained on clean history, then a live feed.

    The second tank develops a leak partway through, so the dashboard has
    something to find while anyone is watching it.
    """
    t0 = 1_767_225_600
    train_days = 21
    t1 = t0 + train_days * 86400

    hostel = TankSpec("OH-HOSTEL-A", 5000, 2.0, 750, "Hostel A overhead")
    academic = TankSpec("OH-ACAD-B", 3000, 1.5, 450, "Academic block B overhead")

    store = Store(":memory:")
    tanks = []
    for spec, profile, seed, faults in (
        (hostel, HOSTEL_PROFILE, 7, []),
        (academic, ACADEMIC_PROFILE, 19,
         [Fault("leak", t1 + 9 * 3600, rate_lpm=2.5)] if with_leak else []),
    ):
        hist = SimulatedSource(SimConfig(spec, t0, days=train_days,
                                         profile=list(profile), seed=seed))
        base = train_baseline(spec, hist.readings())
        live = SimulatedSource(SimConfig(spec, t1, days=days,
                                         profile=list(profile), seed=seed + 100,
                                         faults=faults))
        tanks.append((spec, Pipeline(spec, base, TUNED, store), live))
    return Engine(tanks, store)


def serve(engine: Engine, port: int = 8000, speed: float = 120.0) -> None:
    """speed = simulated minutes per real second."""
    def pump():
        per_step = 1.0 / speed if speed > 0 else 0.0
        n = 0
        while engine.step():
            n += 1
            if n % 60 == 0:
                engine.store.commit()
            if per_step:
                time.sleep(per_step)
        engine.store.commit()
        print("feed exhausted; the dashboard still serves the final state")

    threading.Thread(target=pump, daemon=True).start()
    Handler.engine = engine
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard on http://127.0.0.1:{port}  "
          f"({speed:g} simulated minutes per second)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main() -> int:
    ap = argparse.ArgumentParser(description="Water tank digital twin dashboard")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--speed", type=float, default=120.0,
                    help="simulated minutes per real second")
    ap.add_argument("--days", type=float, default=6.0)
    ap.add_argument("--no-leak", action="store_true")
    a = ap.parse_args()
    serve(build_demo(a.days, not a.no_leak), a.port, a.speed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
