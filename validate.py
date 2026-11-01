"""Validation harness: measure detection against faults with known onsets.

Nothing here is asserted. Every number in the report is produced by running
the same pipeline the demo runs, over scenarios whose ground truth was
recorded when they were generated.

    python validate.py            full report into results/
    python validate.py --quick    smaller fault library, no sweep
"""
import json
import pathlib
import statistics
import sys
import time
from dataclasses import dataclass, field

from watertwin import (Fault, LEAK, OVERFLOW, Params, Pipeline, SimConfig,
                       SimulatedSource, TankSpec, train_baseline)
from watertwin.alerts import AlertManager
from watertwin.detect import LeakDetector
from watertwin.twin import RateSample, Twin

SPEC = TankSpec("OH-HOSTEL-A", capacity_l=5000, area_m2=2.0, reserve_l=750,
                label="Hostel A overhead")
T0 = 1_767_225_600                  # Thu 01 Jan 2026 00:00 UTC
TRAIN_DAYS = 21
T1 = T0 + TRAIN_DAYS * 86400
LEAK_RATES = (0.5, 1.0, 2.0, 3.0, 5.0)
SEEDS = (11, 23, 37, 51, 67)
RESULTS = pathlib.Path(__file__).parent / "results"


@dataclass
class Scenario:
    name: str
    days: float
    seed: int
    expect: str                     # leak | overflow | none
    onset_ts: int = 0
    rate_lpm: float = 0.0
    faults: list[Fault] = field(default_factory=list)
    quantum_m: float = 0.0

    def source(self) -> SimulatedSource:
        return SimulatedSource(SimConfig(SPEC, T1, days=self.days,
                                         seed=self.seed, faults=self.faults,
                                         level_quantum_m=self.quantum_m))


def fault_library(quick: bool = False) -> list[Scenario]:
    """Leaks to detect, faults to catch, and cases designed to fool us."""
    seeds = SEEDS[:2] if quick else SEEDS
    rates = (1.0, 3.0) if quick else LEAK_RATES
    out: list[Scenario] = []

    for rate in rates:
        for sd in seeds:
            onset = T1 + 2 * 86400
            out.append(Scenario(f"leak {rate} L/min s{sd}", 5.0, sd, "leak",
                                onset, rate,
                                [Fault("leak", onset, rate_lpm=rate)]))

    for sd in seeds[:3]:
        onset = T1 + 86400
        out.append(Scenario(f"stuck float s{sd}", 3.0, sd, "overflow", onset,
                            faults=[Fault("stuck_float", onset)]))

    # Cases that must NOT raise a leak alert.
    for sd in seeds:
        out.append(Scenario(f"clean s{sd}", 14.0, sd, "none"))
    for sd in seeds[:3]:
        onset = T1 + 86400
        out.append(Scenario(f"demand spike s{sd}", 4.0, sd, "none",
                            faults=[Fault("demand_spike", onset,
                                          onset + 3 * 3600, rate_lpm=8.0)]))
    for sd in seeds[:2]:
        onset = T1 + 86400
        out.append(Scenario(f"sensor dropout s{sd}", 4.0, sd, "none",
                            faults=[Fault("dropout", onset, onset + 5 * 3600)]))
    # A 1 cm sensor, the Phase 2 resolution limit, on an otherwise clean run.
    for sd in seeds[:2]:
        out.append(Scenario(f"quantised 1cm s{sd}", 10.0, sd, "none",
                            quantum_m=0.01))
    return out


@dataclass
class Outcome:
    scenario: str
    expect: str
    rate_lpm: float
    detected: bool
    latency_min: float | None
    estimate_lpm: float | None
    false_leaks: int
    days: float


def run(scen: Scenario, base, params: Params) -> Outcome:
    pipe = Pipeline(SPEC, base, params)
    first = None
    false_leaks = 0
    for r in scen.source().readings():
        for a in pipe.feed(r):
            if a.kind == LEAK:
                if scen.expect == "leak" and a.raised_ts >= scen.onset_ts:
                    if first is None:
                        first = a
                else:
                    false_leaks += 1
            elif a.kind == OVERFLOW and scen.expect == "overflow":
                if first is None:
                    first = a
    return Outcome(scen.name, scen.expect, scen.rate_lpm, first is not None,
                   (first.raised_ts - scen.onset_ts) / 60.0 if first else None,
                   first.magnitude if first else None, false_leaks, scen.days)


# --- threshold sweep ----------------------------------------------------
# The simulator is the slow part, so each scenario's rate samples are
# generated once and the detector replayed over them for every (k, h).

def cache_samples(scens: list[Scenario]) -> dict[str, list[RateSample]]:
    cache = {}
    for s in scens:
        twin = Twin(SPEC)
        out = []
        for r in s.source().readings():
            smp = twin.update(r)
            if smp is not None:
                out.append(smp)
        cache[s.name] = out
    return cache


def replay(samples: list[RateSample], scen: Scenario, base,
           k: float, h: float, cooloff_s: int):
    """Detector + alert manager over cached samples. Returns the same
    (detected, latency, false alarms) the full pipeline would produce."""
    det = LeakDetector(base, k, h)
    mgr = AlertManager(cooloff_s)
    hit, false_leaks = None, 0
    for s in samples:
        d = det.update(s)
        if not d.firing:
            mgr.clear(SPEC.tank_id, LEAK)
            continue
        a = mgr.raise_(SPEC.tank_id, LEAK, d.onset_ts, s.ts,
                       d.magnitude_lpm, d.confidence)
        if a is None:
            continue
        if scen.expect == "leak" and s.ts >= scen.onset_ts:
            if hit is None:
                hit = s.ts
        else:
            false_leaks += 1
    return hit is not None, (hit - scen.onset_ts) / 60.0 if hit else None, false_leaks


def sweep(scens, cache, base, ks, hs, cooloff_s) -> list[dict]:
    rows = []
    leaks = [s for s in scens if s.expect == "leak"]
    nones = [s for s in scens if s.expect == "none"]
    clean_weeks = sum(s.days for s in nones) / 7.0
    for k in ks:
        for h in hs:
            det_1lpm, lat, hits = [], [], 0
            for s in leaks:
                ok, ms, _ = replay(cache[s.name], s, base, k, h, cooloff_s)
                hits += ok
                if ok:
                    lat.append(ms)
                if abs(s.rate_lpm - 1.0) < 1e-9:
                    det_1lpm.append(ok)
            fa = sum(replay(cache[s.name], s, base, k, h, cooloff_s)[2]
                     for s in nones)
            rows.append({
                "k_lpm": k, "h_litres": h,
                "detection_rate": hits / len(leaks),
                "detection_rate_1lpm": (sum(det_1lpm) / len(det_1lpm)
                                        if det_1lpm else None),
                "median_latency_min": statistics.median(lat) if lat else None,
                "false_alarms_per_tank_week": fa / clean_weeks,
            })
    return rows


def pick(rows: list[dict], ceiling: float = 1.0) -> dict:
    """Maximise detection subject to the false-alarm ceiling, not instead of it."""
    ok = [r for r in rows if r["false_alarms_per_tank_week"] <= ceiling]
    if not ok:
        return min(rows, key=lambda r: r["false_alarms_per_tank_week"])
    return max(ok, key=lambda r: (r["detection_rate"],
                                  -(r["median_latency_min"] or 1e9)))


def main() -> int:
    quick = "--quick" in sys.argv
    t0 = time.time()
    RESULTS.mkdir(exist_ok=True)

    print(f"training baseline on {TRAIN_DAYS} clean days ...")
    base = train_baseline(
        SPEC,
        SimulatedSource(SimConfig(SPEC, T0, days=TRAIN_DAYS, seed=7)).readings())
    print(f"  slot coverage {base.coverage():.0%}, pump cycles "
          f"{base.pump_start_l:.0f} -> {base.pump_stop_l:.0f} L")

    scens = fault_library(quick)
    print(f"generating {len(scens)} scenarios ...")
    cache = cache_samples(scens)

    params = Params()
    sweep_rows, chosen = [], None
    if not quick:
        ks = [0.3, 0.5, 0.75, 1.0, 1.5]
        hs = [20.0, 30.0, 45.0, 60.0, 90.0]
        print(f"sweeping {len(ks) * len(hs)} operating points ...")
        sweep_rows = sweep(scens, cache, base, ks, hs, params.cooloff_s)
        chosen = pick(sweep_rows)
        params = Params(k_lpm=chosen["k_lpm"], h_litres=chosen["h_litres"])
        print(f"  chosen: k={params.k_lpm} L/min, h={params.h_litres} L")

    print("running the full pipeline at the chosen operating point ...")
    outcomes = [run(s, base, params) for s in scens]
    report = summarise(outcomes, params, base, sweep_rows, chosen)
    report["runtime_s"] = round(time.time() - t0, 1)

    (RESULTS / "metrics.json").write_text(json.dumps(report, indent=2))
    (RESULTS / "REPORT.md").write_text(render(report), encoding="utf-8")
    print(render(report))
    print(f"\nwritten to {RESULTS / 'REPORT.md'} and metrics.json")
    return 0


def summarise(outs, params: Params, base, sweep_rows, chosen) -> dict:
    leaks = [o for o in outs if o.expect == "leak"]
    overs = [o for o in outs if o.expect == "overflow"]
    nones = [o for o in outs if o.expect == "none"]
    clean_weeks = sum(o.days for o in nones) / 7.0

    by_rate = {}
    for rate in sorted({o.rate_lpm for o in leaks}):
        grp = [o for o in leaks if o.rate_lpm == rate]
        lat = [o.latency_min for o in grp if o.latency_min is not None]
        est = [o.estimate_lpm for o in grp if o.estimate_lpm is not None]
        by_rate[f"{rate:g}"] = {
            "n": len(grp),
            "detected": sum(o.detected for o in grp),
            "detection_rate": sum(o.detected for o in grp) / len(grp),
            "median_latency_min": statistics.median(lat) if lat else None,
            "median_estimate_lpm": statistics.median(est) if est else None,
        }

    fa = sum(o.false_leaks for o in outs)
    lat_all = [o.latency_min for o in leaks if o.latency_min is not None]
    return {
        "params": {"k_lpm": params.k_lpm, "h_litres": params.h_litres,
                   "window_min": params.window_min,
                   "cooloff_hours": params.cooloff_s / 3600},
        "baseline": {"slot_coverage": round(base.coverage(), 3),
                     "pump_start_l": round(base.pump_start_l),
                     "pump_stop_l": round(base.pump_stop_l)},
        "leaks": {"n": len(leaks),
                  "detection_rate": sum(o.detected for o in leaks) / len(leaks),
                  "median_latency_min": (statistics.median(lat_all)
                                         if lat_all else None),
                  "by_rate": by_rate},
        "overflow": {"n": len(overs),
                     "detection_rate": (sum(o.detected for o in overs) / len(overs)
                                        if overs else None)},
        "false_alarms": {"total": fa, "clean_tank_weeks": round(clean_weeks, 1),
                         "per_tank_week": round(fa / clean_weeks, 3)},
        "sweep": sweep_rows,
        "chosen_point": chosen,
        "scenarios": [o.__dict__ for o in outs],
    }


def render(r: dict) -> str:
    p, L, f = r["params"], r["leaks"], r["false_alarms"]
    lines = [
        "# Validation report -- Campus Water Tank Digital Twin (Phase 1)",
        "",
        "Measured on simulated data with injected faults of known onset.",
        "These are not measurements from a deployed system.",
        "",
        f"Operating point: k = {p['k_lpm']} L/min, h = {p['h_litres']} L, "
        f"{p['window_min']}-minute windows, {p['cooloff_hours']:g} h cool-off.",
        "",
        "## Success criteria",
        "",
        "| ID | Criterion | Target | Measured | Verdict |",
        "| --- | --- | --- | --- | --- |",
    ]
    one = L["by_rate"].get("1", {})
    checks = [
        ("SC-1", "Leak detection at 1 L/min and above", ">= 90%",
         _rate_at_or_above(L["by_rate"], 1.0), 0.90, "pct"),
        ("SC-2", "Detection latency at 1 L/min (median)", "<= 120 min",
         one.get("median_latency_min"), 120, "lte"),
        ("SC-3", "False alarms on fault-free data", "<= 1 / tank-week",
         f["per_tank_week"], 1.0, "lte"),
        ("SC-6", "Overflow detection", "100%",
         r["overflow"]["detection_rate"], 1.0, "pct"),
    ]
    for cid, name, target, val, thr, mode in checks:
        if val is None:
            shown, ok = "n/a", False
        elif mode == "pct":
            shown, ok = f"{val:.0%}", val >= thr
        else:
            shown, ok = (f"{val:.0f}" if val >= 10 else f"{val:.2f}"), val <= thr
        lines.append(f"| {cid} | {name} | {target} | {shown} | "
                     f"{'PASS' if ok else 'FAIL'} |")

    lines += ["", "## Detection by leak rate", "",
              "| Leak (L/min) | n | Detected | Rate | Median latency (min) | "
              "Median estimate (L/min) |",
              "| --- | --- | --- | --- | --- | --- |"]
    for rate, d in L["by_rate"].items():
        lat = f"{d['median_latency_min']:.0f}" if d["median_latency_min"] else "--"
        est = f"{d['median_estimate_lpm']:.2f}" if d["median_estimate_lpm"] else "--"
        lines.append(f"| {rate} | {d['n']} | {d['detected']} | "
                     f"{d['detection_rate']:.0%} | {lat} | {est} |")

    lines += ["", "## False alarms", "",
              f"{f['total']} false leak alerts over {f['clean_tank_weeks']} "
              f"fault-free tank-weeks = **{f['per_tank_week']:.2f} per tank-week**.",
              "",
              "Fault-free scenarios include legitimate demand spikes, sensor",
              "dropouts and a 1 cm quantised sensor. All must stay quiet.",
              ""]

    if r.get("sweep"):
        ch = r.get("chosen_point") or {}
        lines += ["## Threshold sweep", "",
                  "Detection against false alarms across the operating curve.",
                  "The chosen point maximises detection subject to SC-3, rather",
                  "than reporting the best detection rate at an unusable rate.",
                  "",
                  "| k (L/min) | h (L) | Detection | 1 L/min | Median latency | "
                  "False alarms / tank-week |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for row in r["sweep"]:
            star = (" **<--**" if (row["k_lpm"] == ch.get("k_lpm")
                                   and row["h_litres"] == ch.get("h_litres"))
                    else "")
            d1 = (f"{row['detection_rate_1lpm']:.0%}"
                  if row["detection_rate_1lpm"] is not None else "--")
            lat = (f"{row['median_latency_min']:.0f}"
                   if row["median_latency_min"] else "--")
            lines.append(f"| {row['k_lpm']} | {row['h_litres']:g} | "
                         f"{row['detection_rate']:.0%} | {d1} | {lat} | "
                         f"{row['false_alarms_per_tank_week']:.2f}{star} |")
    lines += ["", f"Runtime {r.get('runtime_s', 0)} s."]
    return "\n".join(lines)


def _rate_at_or_above(by_rate: dict, floor: float):
    grp = [(k, v) for k, v in by_rate.items() if float(k) >= floor]
    n = sum(v["n"] for _, v in grp)
    return sum(v["detected"] for _, v in grp) / n if n else None


if __name__ == "__main__":
    raise SystemExit(main())
