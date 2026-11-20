"""Exact-rational titration equivalence and potency service.

The package implements a deliberately finite, discrete signal model:

    L_i = (e_i - e_{i-1}) / (v_i - v_{i-1})
    R_i = (e_{i+1} - e_i) / (v_{i+1} - v_i)
    s_i = abs(R_i - L_i)

Only internal, measured points are candidates.  No continuous-curve
inflection is inferred.
"""

from .control_events import (
    AlarmSource,
    ControlEvent,
    ControlMeasurement,
    detect_events,
    events_from_product,
    sort_events,
)
from .control_service import ControlService
from .curve_service import CurveService, MethodParams, compute_candidates
from .exact import (
    ValidationError,
    NotFoundError,
    ConflictError,
    parse_fraction,
    format_fraction,
)
from .potency import PotencyService
from .reinsurance import (
    Accident,
    OptimalPlan,
    Policy,
    brute_force_optimum,
    build_accidents,
    evaluate_plan,
    optimize,
    settle_payable,
)
from .solver import solve_selection, brute_force_selection
from .store import Store

__all__ = [
    "CurveService",
    "MethodParams",
    "compute_candidates",
    "PotencyService",
    "Store",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "parse_fraction",
    "format_fraction",
    "solve_selection",
    "brute_force_selection",
    "ControlService",
    "ControlMeasurement",
    "ControlEvent",
    "AlarmSource",
    "detect_events",
    "events_from_product",
    "sort_events",
    "Policy",
    "Accident",
    "OptimalPlan",
    "optimize",
    "evaluate_plan",
    "build_accidents",
    "settle_payable",
    "brute_force_optimum",
]
