"""Campus Water Tank Digital Twin -- Phase 1.

A live model of an overhead tank that mirrors its level, forecasts
shortfalls and detects leaks from deviations in expected drain rate.

Standard library only: no pip install, so it runs at the demo.
"""
from __future__ import annotations
from .alerts import LEAK, OVERFLOW, SHORTFALL, Alert, AlertManager
from .detect import LeakDetector, check_overflow, forecast_shortfall
from .feeds import CsvSource, StoredSource, write_csv
from .interface import DataSource, Reading, TankSpec
from .pipeline import Params, Pipeline
from .simulator import (ACADEMIC_PROFILE, HOSTEL_PROFILE, Fault, SimConfig,
                        SimulatedSource)
from .store import Store
from .twin import Baseline, Twin, train_baseline

__all__ = [
    "ACADEMIC_PROFILE", "HOSTEL_PROFILE", "LEAK", "OVERFLOW", "SHORTFALL",
    "Alert", "AlertManager", "Baseline", "CsvSource", "DataSource", "Fault",
    "LeakDetector", "Params", "Pipeline", "Reading", "SimConfig",
    "SimulatedSource", "Store", "StoredSource", "TankSpec", "Twin",
    "check_overflow", "forecast_shortfall", "train_baseline", "write_csv",
]
