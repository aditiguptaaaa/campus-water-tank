# Campus Water Tank Digital Twin

A live model of a campus overhead water tank that mirrors its level,
forecasts shortfalls, and detects leaks from deviations in the expected
drain rate — so facilities staff are told before supply fails, not after
the water bill arrives.

Capstone project, CBCA301, Bennett University. Phase 1.

- **SDG 6** Clean Water and Sanitation (primary), **SDG 11** Sustainable
  Cities and Communities (secondary)
- **T9** Digital Twins 2.0 (primary), **T7** AI + Edge Computing (secondary)

## The problem

Overhead tanks are managed by eye. A caretaker looks at a float indicator,
decides the tank looks low, and starts the pump. There is no record of what
the building actually consumes, no expectation of what the level should be
at a given hour, and therefore no way to tell a normal drawdown from a leak.
A leak of 1 L/min — far below anything anyone would notice — wastes about
43,000 litres a month.

The fix for a leaking joint is cheap and routine. What is missing is the
signal that says where and when to look. That makes this a detection
problem, not a plumbing one.

## How it works

A **digital twin** is distinguished from a dashboard by carrying a model:
a dashboard shows what the sensor reported, a twin also computes what the
sensor *should* have reported, and can therefore notice that the two
disagree.

1. **Mass balance.** Over an interval, the change in stored volume is
   inflow minus consumption minus loss. Loss is the term we cannot observe,
   and it is what the detector recovers.
2. **Baseline.** Expected consumption per hour-of-week, as a median over
   history. A hostel block drawing hard at 07:00 and nothing at 03:00 is
   normal, and the baseline encodes that — so detection never has to assume
   an idle window, which is where the usual "overnight drop" heuristic fails.
3. **Residual + CUSUM.** `S = max(0, S + (observed − expected − k) · minutes)`,
   alerting when `S` passes `h`. A single high reading means nothing; demand
   is noisy. A small *persistent* excess accumulates until it is unambiguous.
   `S` is in litres of unexplained loss, and expected detection time for a
   leak of `q` L/min is roughly `h / (q − k)` minutes.
4. **Forecast.** The same baseline projected forward answers "when does this
   tank run dry if nobody acts", which is the alert worth having.
5. **Overflow.** Needs no learned model of demand: still filling well past
   the level where this pump's float normally cuts out. That is the stuck
   float that defeats the mechanical control this system backs up.

Pump windows and data gaps are held, not counted — inflow dominates and a
gap is unassessable, so neither is evidence either way.

## Results

Measured on simulated data with injected faults of known onset, at the
operating point chosen by the threshold sweep (`k` = 0.3 L/min, `h` = 60 L,
10-minute windows). **These are not measurements from a deployed system.**

| ID | Criterion | Target | Measured | |
| --- | --- | --- | --- | --- |
| SC-1 | Leak detection at 1 L/min and above | ≥ 90% | 100% | PASS |
| SC-2 | Detection latency at 1 L/min (median) | ≤ 120 min | 120 min | PASS |
| SC-3 | False alarms on fault-free data | ≤ 1 / tank-week | 0.25 | PASS |
| SC-4 | Shortfall warning lead time (median) | ≥ 180 min | 233 min | PASS |
| SC-5 | Forecast error at 6 h (mean absolute) | ≤ 10% of capacity | 0.7% | PASS |
| SC-6 | Overflow detection | 100% | 100% | PASS |

| Leak (L/min) | Detected | Median latency | Median estimate |
| --- | --- | --- | --- |
| 0.5 | 5/5 | 250 min | 0.62 L/min |
| 1.0 | 5/5 | 120 min | 1.21 L/min |
| 2.0 | 5/5 | 90 min | 1.97 L/min |
| 3.0 | 5/5 | 80 min | 2.77 L/min |
| 5.0 | 5/5 | 80 min | 3.88 L/min |

Fault-free scenarios include legitimate demand spikes, sensor dropouts and a
1 cm quantised sensor, none of which should raise a leak alert. Over 15.7
fault-free tank-weeks they raised 4: one in each of the three demand-spike
runs and one on the quantised sensor — inside the SC-3 ceiling, but not
zero, and the demand spike is the case that most often fools it. The full
operating curve is in [`results/REPORT.md`](results/REPORT.md); the chosen
point maximises detection *subject to* the false-alarm ceiling, rather than
reporting the best detection rate at an alert rate nobody would tolerate.

## Running it

Python 3.11+. **No dependencies** — standard library only, so it starts at
the demo without a `pip install`.

```bash
python tests/test_watertwin.py     # 28 tests
python validate.py                 # full fault library + threshold sweep (~4-8 s)
python validate.py --quick         # smaller library, no sweep, untuned defaults
python -m watertwin.api            # dashboard on http://127.0.0.1:8000
```

The dashboard replays a simulated feed at 120 simulated minutes per second
over two tanks; the second develops a 2.5 L/min leak nine hours in, so there
is something to find while you watch. `--speed`, `--port`, `--days` and
`--no-leak` adjust it.

## Layout

| File | What it is |
| --- | --- |
| `watertwin/interface.py` | The data-source contract every feed satisfies |
| `watertwin/simulator.py` | Tank physics forward in time, with injectable faults |
| `watertwin/feeds.py` | Historical feeds: CSV replay and store replay |
| `watertwin/twin.py` | Mass balance and the learned baseline |
| `watertwin/detect.py` | CUSUM leak detector, forecaster, overflow check |
| `watertwin/alerts.py` | De-duplication, cool-off, user feedback |
| `watertwin/pipeline.py` | The running system for one tank |
| `watertwin/store.py` | SQLite persistence |
| `watertwin/api.py` | HTTP API and dashboard host |
| `validate.py` | Fault library, metrics, threshold sweep |

Everything above `interface.py` is unaware of which feed is attached, which
is what makes Phase 2 a driver implementation rather than a rebuild. That
claim is tested rather than asserted: `test_feeds_are_interchangeable` runs
identical twins over a live simulated feed and the same run replayed from a
CSV, and requires the resulting state to match exactly.

```python
from watertwin import CsvSource, TankSpec, train_baseline
spec = TankSpec("OH-HOSTEL-A", 5000, 2.0, 750)
base = train_baseline(spec, CsvSource(spec, "history.csv").readings())
```

## Phase 2, not in this repo

Replace the simulated feed with a JSN-SR04T ultrasonic sensor on an ESP32
(about ₹1,500–2,000 per tank). Because both satisfy the same interface, this
is a new `DataSource` and a configuration change.

## Known limitations

These are properties of the approach, stated so the results are read
correctly.

- **Validated on simulated data.** However carefully the simulator is built
  from physics, it cannot contain every irregularity of real consumption.
- **Detection, not diagnosis.** The system reports that a tank is losing
  water faster than its history explains. It cannot say whether the cause is
  a cracked joint, a leaking valve, or a draw the baseline has not seen.
- **The baseline inherits whatever history contains.** A leak already
  running during training is learned as normal and will not be flagged. The
  system detects *changes* in behaviour, not absolute inefficiency.
- **There is a floor on detectable leak size.** At 0.5 L/min the median
  latency is over four hours; below that the residual is lost in demand
  noise at any usable false-alarm rate.
- **Latency includes waiting for the pump.** `h / (q − k)` is a best case.
  The detector holds while the pump runs, and every leak scenario starts at
  midnight, when most runs are mid-refill — so a 5 L/min leak, due in about
  13 minutes, is typically flagged at 80, some ten minutes after the refill
  ends. Because all onsets share one time of day, the latencies (SC-2
  included, which passes at exactly its 120-minute limit) reflect that
  phase of the pump cycle; onsets at other hours were not tested. The slack
  is also the larger of `k` and the hour's own spread, so it can exceed `k`.
- **The magnitude estimate is biased** — slightly high at low leak rates,
  low at high ones, since it averages over the whole episode including the
  onset ramp. It is an estimate for triage, not a meter reading.
- **Phase 1 saves no water.** The deliverable is a validated method and the
  modelled potential of a deployment, not a realised saving.

## Deviations from the Milestone-1 proposal

Two, both recorded here rather than quietly applied:

- **No FastAPI, React or pandas.** The proposal named them; the dashboard
  needs a handful of JSON endpoints and three charts, and a dependency-free
  build is one that still runs at the demo. `http.server`, `sqlite3` and
  `statistics` cover it. Chart.js is loaded from a CDN for the charts only.
- **Seasonal median baseline only.** `statsmodels` was listed as a fallback
  if the median underfits. It did not, so it was not added.
