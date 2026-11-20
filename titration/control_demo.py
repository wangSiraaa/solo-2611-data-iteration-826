"""End-to-end synthetic demo for out-of-control events and re-work cover.

Everything is synthetic and runs offline.  Frozen potencies enter through the
existing public potency entry point (``PotencyService.compute_sample`` over
imported public endpoint products); the values are then archived as control
measurements with the level/center/scale in force at each planned number.

The scenario reproduces the worked example in the brief:

* station 1, five planned items, residuals ``+2, 0, +2, 0, +2`` -> three
  accidents with losses ``2, 6, 6``; policies ``(D=0, L=6, F=1, R=1)`` and
  ``(D=0, L=6, F=7, R=1)`` give optimal nets ``11`` and ``6``;
* correcting the last item to residual ``0`` leaves two accidents and an
  optimal net of ``7``, while the original version-1 product stays queryable;
* station 2 starts with a missing plan-2 item (two accidents), then a late
  measurement fills the gap and merges them into one, showing how the
  deductible changes when two self-retentions collapse into one;
* the exported event product is re-imported and recomputed from a plain JSON
  snapshot with no database access.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .control_events import events_from_product
from .control_service import ControlService
from .curve_service import MethodParams
from .potency import PotencyService
from .reinsurance import Policy, build_accidents, optimize
from .store import Store

# Method used for every control-sample potency (equivalence index j=2).
_METHOD_ID = 1
_METHOD_PARAMS = MethodParams(Fraction(0), Fraction(1000), Fraction(0), 2, Fraction(0), 2)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return _jsonable(obj.to_dict())
    return obj


def _import_product(potency: PotencyService, ref: str, second_volume: str) -> dict:
    """Import a public endpoint product whose j=2 volume drives the potency."""
    return potency.import_external_product(
        {
            "curve_name": f"ctrl-curve-{ref}",
            "method_name": "ctrl-dual",
            "k": 2,
            "selected_volumes": ["1", second_volume],
            "external_ref": ref,
        }
    )


def _freeze_potency(
    potency: PotencyService,
    sample_product: dict,
    *,
    name: str,
    net_mass: int = 10,
    concentration: int = 2,
    factor: int = 2,
) -> str:
    """Compute one frozen potency through the existing potency entry point.

    Every blank exposes j=2 volume 50; with c=2, f=2, m=10, p=1 the potency is
    ``(sample_j2 - 50) * 4 / 10``.
    """
    blank = _import_product(potency, f"blank-{name}", "50")
    sample = potency.create_sample(
        f"ctrl-{name}",
        net_mass=net_mass,
        sample_fraction=1,
        concentration=concentration,
        factor=factor,
        blank_product_id=blank["id"],
        sample_product_id=sample_product["id"],
    )
    result = potency.compute_sample(sample["id"], _METHOD_ID, refresh=False)
    assert result["status"] == "OK", result["failures"]
    return result["potency"]


def build_control_scenario(db_path: str) -> dict:
    store = Store(db_path)
    potency = PotencyService(store)
    control = ControlService(store)

    # A dual-endpoint method declaring j=2, matching the imported K=2 products.
    store.create_method(
        "ctrl-dual",
        _METHOD_PARAMS,
        equivalence_index=2,
    )

    out: dict[str, Any] = {"potency_entry": [], "stations": {}}

    # ------------------------------------------------------------------
    # Station 1: residuals +2, 0, +2, 0, +2 ; center 100, scale 10.
    # potency = 100 + 10*residual -> 120, 100, 120, 100, 120.
    # ------------------------------------------------------------------
    target_potencies = [120, 100, 120, 100, 120]
    # Blank j2=50; potency = (sample_j2 - 50) * 4 / 10 -> j2 = 50 + potency*2.5
    volumes = ["350", "300", "350", "300", "350"]
    frozen: list[str] = []
    for plan, (volume, expected) in enumerate(zip(volumes, target_potencies), start=1):
        product = _import_product(potency, f"s1-p{plan}", volume)
        value = _freeze_potency(potency, product, name=f"s1-p{plan}")
        assert int(value) == expected, (value, expected)
        frozen.append(value)
        out["potency_entry"].append(
            {"station": 1, "plan_no": plan, "potency": value}
        )
        control.record_measurement(
            station=1,
            plan_no=plan,
            level=1,
            center=100,
            scale=10,
            potency=value,
            registered_at=f"2026-01-0{plan}T08:00:00+00:00",
            source_ref=f"sample:ctrl-s1-p{plan}",
        )
    for plan, loss in zip((1, 3, 5), (2, 6, 6)):
        control.set_rework_loss(1, plan, loss)

    product_v1 = control.publish_event_product(
        "station-1-history", station=1, note="original five-item archive"
    )
    v1_ref = product_v1["product_ref"]
    out["stations"][1] = {"v1": _product_summary(product_v1)}

    # Policy A: D=0, L=6, F=1, one restoration -> net 11 (restore after #2).
    policy_a = Policy.from_values(0, 6, 1, 1)
    plan_a = control.recompute_policy(v1_ref, policy_a)
    # Policy B: same events, fee raised to 7 -> keep the right, net 6.
    policy_b = Policy.from_values(0, 6, 1, 7)
    plan_b = control.recompute_policy(v1_ref, policy_b)
    out["stations"][1]["policy_A_fee1"] = plan_a.to_dict()
    out["stations"][1]["policy_B_fee7"] = plan_b.to_dict()

    # A deliberately suboptimal plan (restore after the first accident): net 7.
    from .reinsurance import evaluate_plan

    accidents_v1 = control.accidents_from_product(v1_ref)
    early_restore = evaluate_plan(accidents_v1, policy_a, restore_after=(0,))
    out["stations"][1]["early_restore_after_first"] = early_restore.to_dict()

    # Correct plan 5: potency 120 -> 100 (residual +2 -> 0).  New product
    # version; version 1 remains stored for the historical recheck.
    corrected_product = _import_product(potency, "s1-p5-corrected", "300")
    corrected_value = _freeze_potency(
        potency, corrected_product, name="s1-p5-corrected"
    )
    control.record_measurement(
        station=1,
        plan_no=5,
        level=1,
        center=100,
        scale=10,
        potency=corrected_value,
        registered_at="2026-01-05T08:00:00+00:00",
        arrived_at="2026-02-01T09:30:00+00:00",
        source_ref="sample:ctrl-s1-p5-corrected",
    )
    product_v2 = control.publish_event_product(
        "station-1-history",
        station=1,
        note="plan 5 corrected to residual 0",
    )
    out["stations"][1]["v2"] = _product_summary(product_v2)
    plan_c = control.recompute_policy(product_v2["product_ref"], policy_a)
    out["stations"][1]["policy_A_after_correction"] = plan_c.to_dict()

    # Historical recheck: the retained v1 product still yields net 11.
    recheck = control.recompute_policy(v1_ref, policy_a)
    out["stations"][1]["historical_v1_recheck"] = {
        "product_ref": v1_ref,
        "net_indemnity": recheck.net_indemnity,
        "matches_original": recheck.net_indemnity == plan_a.net_indemnity,
    }

    # Offline equivalence: export v2, drop the DB-facing object, rebuild from
    # the JSON snapshot alone, then recompute policy A with no Store access.
    export_path = Path("results/event_product_station1_v2.json")
    control.export_event_product(product_v2["product_ref"], export_path)
    snapshot = json.loads(export_path.read_text())
    offline_events = events_from_product(snapshot)
    offline_accidents = build_accidents(offline_events, store.get_rework_losses())
    offline_plan = optimize(offline_accidents, policy_a)
    imported = control.import_event_product(snapshot)
    out["stations"][1]["offline_equivalent_product"] = {
        "exported_to": str(export_path),
        "imported_ref": imported["product_ref"],
        "imported_source": imported["source"],
        "offline_net_indemnity": offline_plan.net_indemnity,
        "matches_stored": offline_plan.net_indemnity == plan_c.net_indemnity,
    }

    # ------------------------------------------------------------------
    # Station 2: plan 2 initially missing -> two accidents; a late result
    # fills the gap and merges them.  D=3 makes the deductible effect visible.
    # Residuals +2, (late)+2, +2, +2, +2 ; every alarm is a single-rule hit.
    # ------------------------------------------------------------------
    # plan 1 residual +2 (potency 120, sample j2 volume 350); plan 2 missing.
    p21 = _import_product(potency, "s2-p1", "350")
    v21 = _freeze_potency(potency, p21, name="s2-p1")
    control.record_measurement(
        station=2, plan_no=1, level=1, center=100, scale=10, potency=v21,
        registered_at="2026-03-01T08:00:00+00:00",
        source_ref="sample:ctrl-s2-p1",
    )
    control.record_measurement(
        station=2, plan_no=2, level=1, center=100, scale=10,
        potency=None,
        registered_at="2026-03-02T08:00:00+00:00",
        received=False,
    )
    # plans 3-5 all residual +2 (potency 120).
    for plan in (3, 4, 5):
        prod = _import_product(potency, f"s2-p{plan}", "350")
        val = _freeze_potency(potency, prod, name=f"s2-p{plan}")
        control.record_measurement(
            station=2, plan_no=plan, level=1, center=100, scale=10,
            potency=val,
            registered_at=f"2026-03-0{plan}T08:00:00+00:00",
            source_ref=f"sample:ctrl-s2-p{plan}",
        )
    for plan, loss in ((1, 4), (2, 3), (3, 3), (4, 3), (5, 3)):
        control.set_rework_loss(2, plan, loss)

    gap_product = control.publish_event_product(
        "station-2-late",
        station=2,
        note="plan 2 outstanding: gap splits the events",
    )
    policy_d = Policy.from_values(3, 100, 0, 0)
    gap_plan = control.recompute_policy(gap_product["product_ref"], policy_d)
    out["stations"][2] = {
        "with_gap": {
            "product_ref": gap_product["product_ref"],
            "events": _product_summary(gap_product)["event_keys"],
            "plan": gap_plan.to_dict(),
        }
    }

    # Late arrival for plan 2: residual +2 (potency 120).  It is itself a
    # single-rule alarm and bridges the two old events into one.
    late_product = _import_product(potency, "s2-p2-late", "350")
    late_value = _freeze_potency(potency, late_product, name="s2-p2-late")
    control.record_measurement(
        station=2, plan_no=2, level=1, center=100, scale=10,
        potency=late_value,
        registered_at="2026-03-02T08:00:00+00:00",
        arrived_at="2026-03-06T10:00:00+00:00",
        source_ref="sample:ctrl-s2-p2-late",
    )
    merged_product = control.publish_event_product(
        "station-2-late",
        station=2,
        note="late plan 2 fills the gap and merges events",
    )
    merged_plan = control.recompute_policy(
        merged_product["product_ref"], policy_d
    )
    out["stations"][2]["after_late_arrival"] = {
        "product_ref": merged_product["product_ref"],
        "events": _product_summary(merged_product)["event_keys"],
        "plan": merged_plan.to_dict(),
        "deductible_paid_before": sum(
            d.loss - d.payable for d in gap_plan.decisions
        ),
        "deductible_paid_after": sum(
            d.loss - d.payable for d in merged_plan.decisions
        ),
        "old_gap_product_retained": gap_product["product_ref"],
    }

    store.close()
    out["db"] = str(db_path)
    return _jsonable(out)


def _product_summary(product: dict) -> dict:
    snapshot = product["snapshot"]
    return {
        "product_ref": product["product_ref"],
        "version": product["version"],
        "scope": product["scope"],
        "source": product["source"],
        "event_count": len(snapshot["events"]),
        "event_keys": [e["event_key"] for e in snapshot["events"]],
        "events": snapshot["events"],
    }


def run_control_demo(db_path: str = "var/control_demo.sqlite") -> dict:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    result = build_control_scenario(str(path))
    out = Path("results/control_demo.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
