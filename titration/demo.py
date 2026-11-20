"""End-to-end synthetic demo.

Runs a self-contained scenario in a fresh (or given) SQLite database and
prints the published endpoints, per-sample potencies and batch report.

The synthetic curves are arbitrary rational data; they are not derived from any
real drug standard.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .curve_service import CurveService, MethodParams, compute_candidates
from .potency import PotencyService
from .solver import brute_force_selection
from .store import Store


# Volumes deliberately unevenly spaced.  Segment slopes are constructed so
# the two dominant discrete strength spikes sit near v=4 and v=12.
BASE_VOLUMES = [0, 1, 4, 5, 8, 11, 12, 16, 17]
BASE_SLOPES = [1, 1, 60, 40, 40, 40, 1, 1]


def curve_from_slopes(volumes, slopes):
    """Build potentials from segment slopes, starting at e=0."""
    potentials = [Fraction(0)]
    for dv, slope in zip(
        (b - a for a, b in zip(volumes, volumes[1:])), slopes
    ):
        potentials.append(potentials[-1] + dv * slope)
    return volumes, potentials


def shift(volumes, delta: Fraction):
    return [v + delta for v in volumes]


def build_scenario(db_path: str) -> dict:
    store = Store(db_path)
    curves = CurveService(store)
    potency = PotencyService(store)
    out: dict = {}

    base_v, base_e = curve_from_slopes(
        [Fraction(v) for v in BASE_VOLUMES], [Fraction(s) for s in BASE_SLOPES]
    )
    sample_volumes = base_v
    blank_volumes = shift(base_v, Fraction(-1, 2))
    sample_b_volumes = shift(base_v, Fraction(-1, 4))

    blank_curve = curves.create_curve("blank", blank_volumes, base_e)
    sample_a_curve = curves.create_curve("sample-A", sample_volumes, base_e)
    sample_b_curve = curves.create_curve("sample-B", sample_b_volumes, base_e)

    method = curves.create_method(
        "dual-endpoint",
        low_volume=0,
        high_volume=30,
        threshold=10,
        k=2,
        min_gap=2,
        equivalence_index=2,
    )
    out["method"] = method

    product_blank = curves.publish_endpoints(blank_curve["id"], method["id"])
    product_a = curves.publish_endpoints(sample_a_curve["id"], method["id"])
    product_b = curves.publish_endpoints(sample_b_curve["id"], method["id"])

    out["blank_endpoints"] = product_blank["selected_volumes"]
    out["sample_a_endpoints"] = product_a["selected_volumes"]
    out["sample_b_endpoints"] = product_b["selected_volumes"]
    out["sample_a_objective"] = product_a["objective"]

    # Brute-force audit of the same public candidate set (curve < 10 points).
    params = MethodParams(
        Fraction(0),
        Fraction(30),
        Fraction(10),
        2,
        Fraction(2),
        2,
    )
    cands = compute_candidates(sample_volumes, base_e, params)
    bf = brute_force_selection(cands, 2, Fraction(2))
    out["brute_force_match"] = (
        bf.volumes == [Fraction(v) for v in product_a["selected_volumes"]]
        and f"{bf.objective}" == str(Fraction(product_a["objective"]))
    )

    # Unequal net masses.  p=1/2, c=2 mg/mL, f=1.
    sample_a = potency.create_sample(
        "A",
        net_mass=100,
        sample_fraction=Fraction(1, 2),
        concentration=2,
        factor=1,
        blank_curve_id=blank_curve["id"],
        sample_curve_id=sample_a_curve["id"],
    )
    sample_b = potency.create_sample(
        "B",
        net_mass=300,
        sample_fraction=Fraction(1, 2),
        concentration=2,
        factor=1,
        blank_curve_id=blank_curve["id"],
        sample_curve_id=sample_b_curve["id"],
    )

    # Failing sample built from external public products: blank j=2 volume
    # (20) exceeds sample j=2 volume (10), so effective volume is negative.
    external_blank = potency.import_external_product(
        {
            "curve_name": "external-blank",
            "method_name": "external",
            "k": 2,
            "selected_volumes": ["5", "20"],
            "external_ref": "ext-blank-1",
        }
    )
    external_sample = potency.import_external_product(
        {
            "curve_name": "external-sample",
            "method_name": "external",
            "k": 2,
            "selected_volumes": ["3", "10"],
            "external_ref": "ext-sample-1",
        }
    )
    sample_bad = potency.create_sample(
        "bad-negative",
        net_mass=100,
        sample_fraction=1,
        concentration=2,
        factor=1,
        blank_product_id=external_blank["id"],
        sample_product_id=external_sample["id"],
    )

    batch = potency.create_batch(
        "batch-1", method["id"], [sample_a["id"], sample_b["id"]]
    )
    report_ok = potency.compute_batch(batch["id"])
    out["batch_report"] = {
        "status": report_ok["status"],
        "average": None
        if report_ok["average"] is None
        else str(report_ok["average"]),
        "per_sample": [
            {
                "sample": s["sample_name"],
                "potency": s["potency"],
                "net_mass": s["net_mass"],
            }
            for s in report_ok["details"]["samples"]
        ],
    }

    batch_incomplete = potency.create_batch(
        "batch-incomplete", method["id"], [sample_a["id"], sample_bad["id"]]
    )
    report_incomplete = potency.compute_batch(batch_incomplete["id"])
    out["incomplete_report"] = {
        "status": report_incomplete["status"],
        "average": report_incomplete["average"],
        "gaps": report_incomplete["details"]["gaps"],
    }

    # Recompute after correcting sample A's curve (+1 mL shift).  A new
    # product version and a different potency must appear; the old report
    # stays bound to its original inputs.
    old_a_potency = report_ok["details"]["samples"][0]["potency"]
    old_report_id = report_ok["id"]
    corrected_v, corrected_e = curve_from_slopes(
        shift(sample_volumes, Fraction(1)),
        [Fraction(s) for s in BASE_SLOPES],
    )
    curves.update_curve(
        sample_a_curve["id"],
        corrected_v,
        corrected_e,
        expected_version=1,
    )
    batch2 = potency.create_batch(
        "batch-1-recomputed", method["id"], [sample_a["id"], sample_b["id"]]
    )
    report_new = potency.compute_batch(batch2["id"])
    new_a_potency = report_new["details"]["samples"][0]["potency"]
    replay = potency.replay_report(old_report_id)
    out["recompute"] = {
        "old_potency": old_a_potency,
        "new_potency": new_a_potency,
        "changed": old_a_potency != new_a_potency,
        "old_report_status": store.get_report(old_report_id)["status"],
        "old_report_a_potency": store.get_report(old_report_id)["details"][
            "samples"
        ][0]["potency"],
        "old_report_id": old_report_id,
        "replay_old_report_a_potency": replay["samples"][0]["potency"],
    }

    out["explanation"] = {
        "selected": [s["volume"] for s in product_a["explanation"]["selected"]],
        "objective": product_a["explanation"]["objective"],
        "excluded": [
            {
                "volume": e["volume"],
                "strength": e["strength"],
                "reason": e["reason"],
            }
            for e in product_a["explanation"]["excluded"]
        ],
    }

    store.close()
    out["db"] = str(db_path)
    return _jsonable(out)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Fraction):
        return str(obj)
    return obj


def run_demo(db_path: str = "var/demo.sqlite") -> dict:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    result = build_scenario(str(path))
    out = Path("results/demo.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
