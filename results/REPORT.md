# Validation report -- Campus Water Tank Digital Twin (Phase 1)

Measured on simulated data with injected faults of known onset.
These are not measurements from a deployed system.

Operating point: k = 0.3 L/min, h = 60.0 L, 10-minute windows, 6 h cool-off.

## Success criteria

| ID | Criterion | Target | Measured | Verdict |
| --- | --- | --- | --- | --- |
| SC-1 | Leak detection at 1 L/min and above | >= 90% | 100% | PASS |
| SC-2 | Detection latency at 1 L/min (median) | <= 120 min | 120 | PASS |
| SC-3 | False alarms on fault-free data | <= 1 / tank-week | 0.26 | PASS |
| SC-4 | Shortfall warning lead time (median) | >= 180 min | 233 | PASS |
| SC-5 | Forecast error at 6 h (MAE) | <= 10% of capacity | 0.7% | PASS |
| SC-6 | Overflow detection | 100% | 100% | PASS |

## Detection by leak rate

| Leak (L/min) | n | Detected | Rate | Median latency (min) | Median estimate (L/min) |
| --- | --- | --- | --- | --- | --- |
| 0.5 | 5 | 5 | 100% | 250 | 0.62 |
| 1 | 5 | 5 | 100% | 120 | 1.21 |
| 2 | 5 | 5 | 100% | 90 | 1.97 |
| 3 | 5 | 5 | 100% | 80 | 2.77 |
| 5 | 5 | 5 | 100% | 80 | 3.88 |

## Shortfall warning

3 pump-failure scenarios, 100% warned. Median lead before the reserve was actually crossed: 233 min (worst 233 min).

## Forecast accuracy

65 back-tests at a 6 hour horizon against runs with the pump off: mean absolute error 33 L (0.7% of capacity), 90th percentile 70 L.

## False alarms

4 false leak alerts over 15.7 fault-free tank-weeks = **0.26 per tank-week**.

Fault-free scenarios include legitimate demand spikes, sensor
dropouts and a 1 cm quantised sensor. All must stay quiet.

## Threshold sweep

Detection against false alarms across the operating curve.
The chosen point maximises detection subject to SC-3, rather
than reporting the best detection rate at an unusable rate.

| k (L/min) | h (L) | Detection | 1 L/min | Median latency | False alarms / tank-week |
| --- | --- | --- | --- | --- | --- |
| 0.3 | 20 | 100% | 100% | -- | 10.82 |
| 0.3 | 30 | 100% | 100% | 60 | 5.85 |
| 0.3 | 45 | 100% | 100% | 80 | 1.97 |
| 0.3 | 60 | 100% | 100% | 90 | 0.25 **<--** |
| 0.3 | 90 | 100% | 100% | 110 | 0.19 |
| 0.5 | 20 | 100% | 100% | 10 | 9.23 |
| 0.5 | 30 | 100% | 100% | 70 | 4.65 |
| 0.5 | 45 | 100% | 100% | 80 | 1.02 |
| 0.5 | 60 | 100% | 100% | 90 | 0.19 |
| 0.5 | 90 | 100% | 100% | 110 | 0.19 |
| 0.75 | 20 | 100% | 100% | 60 | 6.87 |
| 0.75 | 30 | 100% | 100% | 80 | 2.35 |
| 0.75 | 45 | 100% | 100% | 90 | 0.89 |
| 0.75 | 60 | 96% | 100% | 100 | 0.19 |
| 0.75 | 90 | 80% | 100% | 110 | 0.19 |
| 1.0 | 20 | 100% | 100% | 70 | 5.03 |
| 1.0 | 30 | 96% | 100% | 80 | 1.65 |
| 1.0 | 45 | 92% | 100% | 100 | 0.25 |
| 1.0 | 60 | 80% | 100% | 100 | 0.19 |
| 1.0 | 90 | 80% | 100% | 110 | 0.19 |
| 1.5 | 20 | 100% | 100% | 90 | 2.23 |
| 1.5 | 30 | 88% | 100% | 100 | 1.34 |
| 1.5 | 45 | 76% | 80% | 100 | 0.19 |
| 1.5 | 60 | 60% | 0% | 100 | 0.19 |
| 1.5 | 90 | 60% | 0% | 120 | 0.19 |

Runtime 8.0 s.