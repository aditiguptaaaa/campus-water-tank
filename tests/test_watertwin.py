"""Tests. Plain asserts, no framework -- nothing here needs one.

    python tests/test_watertwin.py

Covers the parts that would fail silently if they broke: the mass balance,
the baseline, the detector's behaviour on leaks and on things that only
look like leaks, and the alert manager's de-duplication.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watertwin import (Fault, LEAK, OVERFLOW, Params, Pipeline, Reading,
                       SimConfig, SimulatedSource, Store, TankSpec, Twin,
                       train_baseline)
from watertwin.alerts import AlertManager
from watertwin.detect import LeakDetector, forecast_shortfall
from watertwin.simulator import hour_of_week
from watertwin.twin import Baseline, RateSample

SPEC = TankSpec("T1", capacity_l=5000, area_m2=2.0, reserve_l=750, label="test")
T0 = 1_767_225_600          # Thu 01 Jan 2026 00:00 UTC


def flat(rate: float) -> list[float]:
    return [rate] * 24


def quiet_cfg(**kw) -> SimConfig:
    """A deterministic run: no noise, no bursts, pump disabled."""
    base = dict(spec=SPEC, start_ts=T0, days=1.0, profile=flat(2.0),
                noise_sigma=0.0, burst_chance=0.0, weekend_factor=1.0,
                pump_on_frac=-1.0, seed=1)
    base.update(kw)
    return SimConfig(**base)


# --- geometry -----------------------------------------------------------
def test_geometry_round_trip():
    for v in (0.0, 1234.5, 5000.0):
        assert abs(SPEC.volume_l(SPEC.level_m(v)) - v) < 1e-6
    assert SPEC.volume_l(99.0) == SPEC.capacity_l, "clamps at capacity"
    assert SPEC.volume_l(-1.0) == 0.0, "clamps at empty"


# --- the mass balance ---------------------------------------------------
def test_simulator_drains_at_the_demanded_rate():
    src = SimulatedSource(quiet_cfg(days=2 / 24))
    rs = list(src.readings())
    v0, v1 = SPEC.volume_l(rs[0].level_m), SPEC.volume_l(rs[60].level_m)
    assert abs((v0 - v1) - 120.0) < 1e-6, f"2 L/min for 60 min, got {v0 - v1}"


def test_leak_adds_to_the_drain():
    plain = SimulatedSource(quiet_cfg(days=2 / 24))
    leaky = SimulatedSource(quiet_cfg(days=2 / 24,
                                      faults=[Fault("leak", T0, rate_lpm=1.5)]))
    pr, lr = list(plain.readings()), list(leaky.readings())
    drop = lambda rs: SPEC.volume_l(rs[0].level_m) - SPEC.volume_l(rs[60].level_m)
    extra = drop(lr) - drop(pr)
    assert abs(extra - 90.0) < 1e-6, f"1.5 L/min for 60 min, got {extra}"


def test_twin_measures_the_drain_rate():
    twin = Twin(SPEC, window_min=10)
    samples = [s for r in SimulatedSource(quiet_cfg(days=2 / 24)).readings()
               if (s := twin.update(r)) is not None]
    assert samples, "a 2 hour run must close some windows"
    for s in samples:
        assert abs(s.drain_lpm - 2.0) < 1e-6
        assert not s.gap and not s.pumping


def test_gap_is_flagged_not_guessed():
    twin = Twin(SPEC, window_min=10)
    twin.update(Reading("T1", T0, 1.0, False))
    s = twin.update(Reading("T1", T0 + 3600, 0.9, False))
    assert s is not None and s.gap, "an hour-long window must be marked a gap"


# --- the baseline -------------------------------------------------------
def test_baseline_learns_the_daily_shape():
    profile = [1.0] * 24
    profile[7] = 9.0
    src = SimulatedSource(quiet_cfg(days=14.0, profile=profile,
                                    pump_on_frac=0.35))
    base = train_baseline(SPEC, src.readings())
    assert base.trained and base.coverage() > 0.9
    peak = base.slot_median[hour_of_week(T0 + 7 * 3600)]
    night = base.slot_median[hour_of_week(T0 + 3 * 3600)]
    assert peak > 5.0, f"07h slot should be the peak, got {peak}"
    assert night < 2.0, f"03h slot should be quiet, got {night}"


def test_baseline_learns_the_pump_set_points():
    src = SimulatedSource(quiet_cfg(days=7.0, pump_on_frac=0.35,
                                    pump_off_frac=0.95))
    base = train_baseline(SPEC, src.readings())
    assert 1500 < base.pump_start_l < 2100, base.pump_start_l
    assert 4500 < base.pump_stop_l < 4900, base.pump_stop_l
    assert base.pump_stop_l > base.pump_start_l


def test_baseline_ignores_pumping_windows():
    b = Baseline()
    for i in range(10):
        ts = T0 + i * 600
        b.observe(RateSample(ts, 3.0, False, 10.0, False))
        b.observe(RateSample(ts, -55.0, True, 10.0, False))   # filling
        b.observe(RateSample(ts, 900.0, False, 10.0, True))   # a gap
    b.fit()
    assert abs(b.global_median - 3.0) < 1e-9, b.global_median


# --- the detector -------------------------------------------------------
def _baseline_for(rate: float, spread: float = 0.0) -> Baseline:
    """A fitted baseline at `rate`, optionally with realistic variability."""
    b = Baseline()
    wobble = (0.0, spread, -spread, spread / 2, -spread / 2)
    for i in range(400):
        b.observe(RateSample(T0 + i * 600, rate + wobble[i % 5], False,
                             10.0, False))
    return b.fit()


def test_cusum_fires_on_a_sustained_small_excess():
    det = LeakDetector(_baseline_for(2.0), k_lpm=0.5, h_litres=45.0)
    fired_at = None
    for i in range(60):
        d = det.update(RateSample(T0 + i * 600, 3.0, False, 10.0, False))
        if d.firing and fired_at is None:
            fired_at = i * 10
    assert fired_at is not None, "a 1 L/min excess must eventually fire"
    # h / (excess - k) = 45 / 0.5 = 90 minutes, plus one window of rounding.
    assert 80 <= fired_at <= 110, f"fired at {fired_at} min"


def test_cusum_stays_quiet_through_ordinary_variability():
    det = LeakDetector(_baseline_for(2.0, spread=0.8), k_lpm=0.5, h_litres=45.0)
    wobble = (0.0, 0.8, -0.8, 0.4, -0.4)
    for i in range(200):
        d = det.update(RateSample(T0 + i * 600, 2.0 + wobble[i % 5],
                                  False, 10.0, False))
        assert not d.firing, f"noise alone fired at window {i}"


def test_cusum_shrugs_off_one_short_burst():
    det = LeakDetector(_baseline_for(2.0, spread=0.8), k_lpm=0.5, h_litres=45.0)
    for i in range(40):
        rate = 5.0 if i == 20 else 2.0     # one 30 L burst, under the 45 L bar
        assert not det.update(
            RateSample(T0 + i * 600, rate, False, 10.0, False)).firing


def test_cusum_does_fire_on_one_very_large_excess():
    """Intended: h is litres of unexplained loss, not a count of windows.

    100 L gone in ten minutes is a burst pipe, and waiting for it to be
    'sustained' would be the wrong call."""
    det = LeakDetector(_baseline_for(2.0, spread=0.8), k_lpm=0.5, h_litres=45.0)
    for i in range(5):
        det.update(RateSample(T0 + i * 600, 2.0, False, 10.0, False))
    d = det.update(RateSample(T0 + 3000, 12.0, False, 10.0, False))
    assert d.firing and d.cusum > 45.0


def test_cusum_holds_through_pumping_and_gaps():
    det = LeakDetector(_baseline_for(2.0), k_lpm=0.5, h_litres=45.0)
    for i in range(6):
        det.update(RateSample(T0 + i * 600, 3.0, False, 10.0, False))
    held = det.cusum
    det.update(RateSample(T0 + 7000, -55.0, True, 10.0, False))
    det.update(RateSample(T0 + 9000, 500.0, False, 40.0, True))
    assert det.cusum == held, "pumping and gaps are unassessable, not evidence"


def test_magnitude_estimates_the_leak_not_the_flow():
    det = LeakDetector(_baseline_for(2.0), k_lpm=0.5, h_litres=45.0)
    for i in range(60):
        det.update(RateSample(T0 + i * 600, 4.5, False, 10.0, False))
    assert abs(det.magnitude() - 2.5) < 0.2, det.magnitude()


def test_detector_resets_when_the_excess_goes_away():
    det = LeakDetector(_baseline_for(2.0), k_lpm=0.5, h_litres=45.0)
    for i in range(20):
        det.update(RateSample(T0 + i * 600, 3.0, False, 10.0, False))
    for i in range(20, 80):
        det.update(RateSample(T0 + i * 600, 0.5, False, 10.0, False))
    assert det.cusum == 0.0 and det.onset_ts == 0


# --- the forecast -------------------------------------------------------
def test_forecast_finds_the_reserve_crossing():
    twin = Twin(SPEC)
    twin.update(Reading("T1", T0, SPEC.level_m(1750.0), False))
    f = forecast_shortfall(twin, _baseline_for(2.0), horizon_min=720)
    assert f.dry_ts is not None
    # (1750 - 750) / 2 L/min = 500 minutes.
    assert abs(f.lead_minutes - 500) <= 10, f.lead_minutes
    vols = [v for _, v in f.points]
    assert all(b <= a for a, b in zip(vols, vols[1:])), "must fall, not rise"


def test_forecast_accounts_for_an_active_leak():
    twin = Twin(SPEC)
    twin.update(Reading("T1", T0, SPEC.level_m(1750.0), False))
    base = _baseline_for(2.0)
    clean = forecast_shortfall(twin, base, 0.0, horizon_min=720)
    leaky = forecast_shortfall(twin, base, 2.0, horizon_min=720)
    assert leaky.lead_minutes < clean.lead_minutes / 1.5


# --- alert management ---------------------------------------------------
def test_alert_manager_dedupes_and_cools_off():
    m = AlertManager(cooloff_s=3600)
    assert m.raise_("T1", LEAK, T0, T0, 1.0, 0.8) is not None
    assert m.raise_("T1", LEAK, T0, T0 + 600, 1.0, 0.8) is None, "already open"
    m.clear("T1", LEAK)
    assert m.raise_("T1", LEAK, T0, T0 + 1200, 1.0, 0.8) is None, "in cool-off"
    assert m.raise_("T1", LEAK, T0, T0 + 7200, 1.0, 0.8) is not None
    assert m.raise_("T1", OVERFLOW, T0, T0 + 600, 0.0, 0.9) is not None, \
        "a different condition is not a duplicate"


def test_false_alarm_feedback_reopens_the_channel():
    m = AlertManager(cooloff_s=3600)
    a = m.raise_("T1", LEAK, T0, T0, 1.0, 0.8)
    m.feedback(a, "false")
    assert a.outcome == "false" and not m.open_alerts()
    assert m.raise_("T1", LEAK, T0, T0 + 60, 1.0, 0.8) is not None, \
        "a false alarm must not also silence the next six hours"


def test_alert_message_names_the_tank_and_hedges():
    m = AlertManager()
    a = m.raise_("T1", LEAK, T0, T0 + 3600, 1.5, 0.75)
    msg = a.message()
    assert "T1" in msg and "Suspected" in msg
    assert "1.5 L/min" in msg and "75%" in msg


# --- end to end ---------------------------------------------------------
def test_pipeline_detects_an_injected_leak():
    base = train_baseline(SPEC, SimulatedSource(
        SimConfig(SPEC, T0, days=14.0, seed=3)).readings())
    t1 = T0 + 14 * 86400
    onset = t1 + 86400
    pipe = Pipeline(SPEC, base, Params(k_lpm=0.3, h_litres=60.0))
    src = SimulatedSource(SimConfig(SPEC, t1, days=3.0, seed=9,
                                    faults=[Fault("leak", onset, rate_lpm=2.0)]))
    hits = [a for r in src.readings() for a in pipe.feed(r)
            if a.kind == LEAK and a.raised_ts >= onset]
    assert hits, "a 2 L/min leak must be caught"
    assert (hits[0].raised_ts - onset) / 60 <= 180, "within three hours"


def test_pipeline_is_quiet_on_a_clean_week():
    base = train_baseline(SPEC, SimulatedSource(
        SimConfig(SPEC, T0, days=14.0, seed=3)).readings())
    t1 = T0 + 14 * 86400
    pipe = Pipeline(SPEC, base, Params(k_lpm=0.3, h_litres=60.0))
    src = SimulatedSource(SimConfig(SPEC, t1, days=7.0, seed=4))
    raised = [a for r in src.readings() for a in pipe.feed(r)]
    assert len(raised) <= 1, [a.kind for a in raised]


def test_pipeline_catches_a_stuck_float_before_the_tank_spills():
    base = train_baseline(SPEC, SimulatedSource(
        SimConfig(SPEC, T0, days=14.0, seed=3)).readings())
    t1 = T0 + 14 * 86400
    pipe = Pipeline(SPEC, base, Params(k_lpm=0.3, h_litres=60.0))
    src = SimulatedSource(SimConfig(SPEC, t1, days=2.0, seed=6,
                                    faults=[Fault("stuck_float", t1 + 3600)]))
    spill_at_alert = None
    for r in src.readings():
        for a in pipe.feed(r):
            if a.kind == OVERFLOW and spill_at_alert is None:
                spill_at_alert = src.overflow_l
    assert spill_at_alert is not None, "a stuck float must be caught"
    assert spill_at_alert < 50, f"{spill_at_alert:.0f} L lost before alerting"


# --- persistence --------------------------------------------------------
def test_store_round_trip():
    s = Store(":memory:")
    s.put_tank(SPEC)
    assert [t.tank_id for t in s.tanks()] == ["T1"]
    s.put_readings([Reading("T1", T0 + i, 1.0 + i / 100, False)
                    for i in range(5)])
    s.commit()
    assert len(s.recent_readings("T1")) == 5
    m = AlertManager()
    a = m.raise_("T1", LEAK, T0, T0, 1.0, 0.8)
    aid = s.put_alert(a)
    s.set_outcome(aid, "confirmed")
    assert s.alerts("T1")[0]["outcome"] == "confirmed"
    s.close()


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as e:
            failed.append((name, e))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed.append((name, e))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
