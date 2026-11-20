"""Tests for out-of-control event reconstruction and re-work insurance.

Covers the pure alarm rules (single/paired, breaks, late arrivals), the
versioned event product service with offline equivalence, the insurance DP
against exhaustive enumeration, the worked example from the brief and the
end-to-end path that enters frozen potencies through PotencyService.
"""

from __future__ import annotations

import itertools
import json
from fractions import Fraction

import pytest

from titration import (
    ControlMeasurement,
    ControlService,
    Policy,
    PotencyService,
    Store,
    ValidationError,
    brute_force_optimum,
    build_accidents,
    detect_events,
    evaluate_plan,
    events_from_product,
    optimize,
    settle_payable,
    sort_events,
)
from titration.control_events import RULE_PAIRED, RULE_SINGLE
from titration.control_service import PRODUCT_MODEL
from titration.reinsurance import MAX_ACCIDENTS, MAX_RESTORATIONS


# --------------------------------------------------------------------------
# Measurement / residual helpers
# --------------------------------------------------------------------------
def meas(plan, residual, *, station=1, level=1, center=100, scale=10,
         registered_at=None, received=True):
    registered_at = registered_at or f"2026-01-{plan:02d}T08:00:00+00:00"
    if not received:
        return ControlMeasurement(
            station=station, plan_no=plan, level=level, center=None,
            scale=None, potency=None, registered_at=registered_at,
            received=False,
        )
    potency = Fraction(center) + Fraction(scale) * Fraction(residual)
    return ControlMeasurement(
        station=station, plan_no=plan, level=level,
        center=Fraction(center), scale=Fraction(scale), potency=potency,
        registered_at=registered_at,
    )


def keys(events):
    return [e.event_key for e in events]


# --------------------------------------------------------------------------
# Alarm rule semantics
# --------------------------------------------------------------------------
def test_residual_is_exact_rational():
    m = ControlMeasurement(
        station=1, plan_no=1, level=1, center=Fraction(100),
        scale=Fraction(4), potency=Fraction(109),
        registered_at="t",
    )
    # (109 - 100) / 4 = 9/4
    assert m.residual() == Fraction(9, 4)


def test_single_rule_at_threshold_and_below():
    # A zero separator prevents pairing, so 3/2 below the single threshold
    # never alarms; the two +/-2 hits stay isolated single-rule events.
    events = detect_events(
        [meas(1, 2), meas(2, 0), meas(3, Fraction(3, 2)), meas(4, -2)]
    )
    assert keys(events) == ["S1-L1-P1-1", "S1-L1-P4-4"]
    assert events[0].alarms[0].rules == (RULE_SINGLE,)


def test_paired_rule_requires_both_abs_at_least_one_and_same_sign():
    # +1 then +1: plan 2 alarms on paired only; plan 1 is not an alarm item.
    events = detect_events([meas(1, 1), meas(2, 1)])
    assert keys(events) == ["S1-L1-P2-2"]
    alarm = events[0].alarms[0]
    assert alarm.rules == (RULE_PAIRED,)
    assert alarm.predecessor_plan_no == 1
    assert alarm.predecessor_residual == 1
    # The paired predecessor source record is retained even though it is not
    # itself a loss item.
    assert alarm.predecessor_record["plan_no"] == 1


def test_paired_rule_rejects_opposite_signs():
    assert detect_events([meas(1, 1), meas(2, -1)]) == []
    assert detect_events([meas(1, -1), meas(2, -1)])  # negative side works
    events = detect_events([meas(1, -1), meas(2, -1)])
    assert keys(events) == ["S1-L1-P2-2"]
    assert events[0].alarms[0].rules == (RULE_PAIRED,)


def test_paired_threshold_boundary_exact():
    # abs = 1 qualifies; abs just below 1 does not.
    events = detect_events([meas(1, Fraction(99, 100)), meas(2, 1)])
    assert events == []
    events = detect_events([meas(1, 1), meas(2, Fraction(99, 100))])
    assert events == []


def test_missing_item_breaks_continuation_and_event():
    items = [
        meas(1, 2),
        meas(2, 0, received=False),
        meas(3, 2),
        meas(4, 2),
    ]
    events = detect_events(items)
    assert keys(events) == ["S1-L1-P1-1", "S1-L1-P3-4"]


def test_level_change_breaks_continuation():
    items = [
        meas(1, 2, level=1),
        meas(2, 2, level=2),
        meas(3, 2, level=2),
    ]
    events = detect_events(items)
    assert keys(events) == ["S1-L1-P1-1", "S1-L2-P2-3"]
    # First level-2 item cannot pair across the level boundary.
    assert events[1].alarms[0].rules == (RULE_SINGLE,)


def test_plan_gap_breaks_continuation():
    events = detect_events([meas(1, 1), meas(3, 1)])
    # No adjacent predecessor -> neither paired rule fires; both below 2.
    assert events == []
    events = detect_events([meas(1, 2), meas(3, 2)])
    assert keys(events) == ["S1-L1-P1-1", "S1-L1-P3-3"]


def test_non_alarming_item_separates_runs_but_keeps_predecessor_role():
    # +1, 0, +1: the zero item is not alarming and cannot bridge a pair, so
    # nothing alarms at all.
    assert detect_events([meas(1, 1), meas(2, 0), meas(3, 1)]) == []
    # +1, +1, 0, +2: paired event at 2, zero closes it, single event at 4.
    events = detect_events([meas(1, 1), meas(2, 1), meas(3, 0), meas(4, 2)])
    assert keys(events) == ["S1-L1-P2-2", "S1-L1-P4-4"]


def test_late_arrival_fills_gap_and_merges_events():
    with_gap = [
        meas(1, 2),
        meas(2, 0, received=False),
        meas(3, 2),
        meas(4, 2),
    ]
    assert keys(detect_events(with_gap)) == [
        "S1-L1-P1-1", "S1-L1-P3-4"
    ]
    filled = [with_gap[0], meas(2, 2), *with_gap[2:]]
    events = detect_events(filled)
    # The late item alarms and bridges the two old events into one.
    assert keys(events) == ["S1-L1-P1-4"]
    assert events[0].plan_numbers == (1, 2, 3, 4)


def test_brief_worked_example_three_events():
    residuals = [2, 0, 2, 0, 2]
    events = detect_events([meas(i + 1, r) for i, r in enumerate(residuals)])
    assert keys(events) == [
        "S1-L1-P1-1", "S1-L1-P3-3", "S1-L1-P5-5"
    ]
    events2 = detect_events(
        [meas(1, 2), meas(2, 0), meas(3, 2), meas(4, 0), meas(5, 0)]
    )
    assert keys(events2) == ["S1-L1-P1-1", "S1-L1-P3-3"]


def test_duplicate_planned_item_rejected():
    with pytest.raises(ValidationError):
        detect_events([meas(1, 2), meas(1, 2)])


def test_non_positive_scale_rejected():
    with pytest.raises(ValidationError):
        ControlMeasurement.from_dict(
            {
                "station": 1, "plan_no": 1, "level": 1,
                "center": "0", "scale": "0", "potency": "1",
                "registered_at": "t",
            }
        )


def test_multi_station_ordering_by_registration_then_station():
    a = meas(1, 2, station=2, registered_at="2026-02-01T00:00:00+00:00")
    b = meas(1, 2, station=1, registered_at="2026-01-01T00:00:00+00:00")
    # A non-alarming plan 2 separates this from station 1's first event.
    c0 = meas(2, 0, station=1, registered_at="2026-01-15T00:00:00+00:00")
    c = meas(3, 2, station=1, registered_at="2026-02-01T00:00:00+00:00")
    ordered = sort_events(detect_events([a, b, c0, c]))
    # registration first; same timestamp -> station id (1 before 2).
    assert [(e.station, e.first_plan_no) for e in ordered] == [
        (1, 1), (1, 3), (2, 1)
    ]


# --------------------------------------------------------------------------
# Insurance optimizer
# --------------------------------------------------------------------------
def _accidents(losses):
    from titration.reinsurance import Accident

    return [
        Accident(
            event_key=f"E{i}", station=1, level=1, first_plan_no=i,
            last_plan_no=i, last_registered_at=f"t{i}", loss=loss,
        )
        for i, loss in enumerate(losses)
    ]


def test_settle_payable_floor_and_limit():
    assert settle_payable(5, 3, 10) == 2
    assert settle_payable(2, 3, 10) == 0
    assert settle_payable(100, 0, 6) == 6
    assert settle_payable(100, 0, 0) == 0


def test_brief_policy_scenarios():
    accidents = _accidents([2, 6, 6])
    # D=0, L=6, F=1, one restoration -> restore after accident #2 -> net 11.
    plan = optimize(accidents, Policy.from_values(0, 6, 1, 1))
    assert plan.net_indemnity == 11
    assert [d.restoration_used for d in plan.decisions] == [False, True, False]
    assert plan.rights_remaining == 0
    assert [d.payable for d in plan.decisions] == [2, 4, 6]
    # Restoring after the first accident only nets 7 (suboptimal).
    early = evaluate_plan(accidents, Policy.from_values(0, 6, 1, 1), (0,))
    assert early.net_indemnity == 7
    # Fee 7: keep the right, net 6.
    plan_fee7 = optimize(accidents, Policy.from_values(0, 6, 1, 7))
    assert plan_fee7.net_indemnity == 6
    assert plan_fee7.rights_used == 0


def test_optimizer_matches_brute_force_small_cases():
    rng_losses = [0, 1, 3, 6, 10]
    rng_policies = list(
        itertools.product([0, 2, 4], [4, 8], [0, 1, 2], [1, 5])
    )
    for losses in itertools.product(rng_losses, repeat=4):
        accidents = _accidents(losses)
        for d, limit, rights, fee in rng_policies:
            policy = Policy.from_values(d, limit, rights, fee)
            dp = optimize(accidents, policy).net_indemnity
            bf = brute_force_optimum(accidents, policy)
            assert dp == bf, (losses, d, limit, rights, fee, dp, bf)


def test_optimizer_matches_brute_force_randomized():
    import random

    rng = random.Random(20261120)
    for _ in range(300):
        n = rng.randint(1, 10)
        losses = [rng.randint(0, 50) for _ in range(n)]
        policy = Policy.from_values(
            deductible=rng.randint(0, 10),
            limit=rng.randint(1, 30),
            restorations=rng.randint(0, MAX_RESTORATIONS),
            restoration_fee=rng.randint(0, 20),
        )
        accidents = _accidents(losses)
        assert optimize(accidents, policy).net_indemnity == (
            brute_force_optimum(accidents, policy)
        )


def test_large_integers_are_exact():
    big = 10**30
    accidents = _accidents([big, big, big])
    policy = Policy.from_values(0, big, 2, 1)
    plan = optimize(accidents, policy)
    # Restore twice, pay every accident in full, minus two unit fees.
    assert plan.net_indemnity == 3 * big - 2
    assert plan.total_payable == 3 * big


def test_policy_validation_limits():
    with pytest.raises(ValidationError):
        Policy.from_values(0, 0, 0, 0)
    with pytest.raises(ValidationError):
        Policy.from_values(-1, 5, 0, 0)
    with pytest.raises(ValidationError):
        Policy.from_values(0, 5, MAX_RESTORATIONS + 1, 0)
    with pytest.raises(ValidationError):
        Policy.from_values(0, 5, 1, 1.5)  # floats rejected


def test_too_many_accidents_rejected():
    accidents = _accidents([1] * (MAX_ACCIDENTS + 1))
    with pytest.raises(ValidationError):
        optimize(accidents, Policy.from_values(0, 5, 0, 0))


def test_losses_attach_to_alarm_items_only_and_clip_negative():
    events = detect_events([meas(1, 2), meas(2, 0), meas(3, 2)])
    # Negative explicit loss is invalid; missing entries contribute zero.
    with pytest.raises(ValidationError):
        build_accidents(events, {(1, 1): -1})
    accidents = build_accidents(events, {(1, 1): 5, (1, 2): 99, (1, 3): 7})
    # Plan 2 is not an alarm item: its loss is ignored entirely.
    assert [a.loss for a in accidents] == [5, 7]


def test_evaluate_plan_rejects_illegal_restoration():
    accidents = _accidents([1, 1])
    policy = Policy.from_values(0, 5, 0, 1)
    with pytest.raises(ValidationError):
        evaluate_plan(accidents, policy, (0,))


# --------------------------------------------------------------------------
# Service: archive, versioned products, offline equivalence
# --------------------------------------------------------------------------
@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "ctrl.sqlite")
    yield ControlService(store)
    store.close()


def _ingest_brief_archive(service, *, corrected_fifth=False):
    residuals = [2, 0, 2, 0, 0 if corrected_fifth else 2]
    for plan, residual in enumerate(residuals, start=1):
        service.record_measurement(
            station=1, plan_no=plan, level=1, center=100, scale=10,
            potency=100 + 10 * residual,
            registered_at=f"2026-01-0{plan}T08:00:00+00:00",
        )
    for plan, loss in zip((1, 3, 5), (2, 6, 6)):
        service.set_rework_loss(1, plan, loss)


def test_versioned_products_retain_history(service):
    _ingest_brief_archive(service)
    v1 = service.publish_event_product("hist", station=1)
    assert v1["version"] == 1
    assert len(v1["snapshot"]["events"]) == 3
    policy = Policy.from_values(0, 6, 1, 1)
    assert service.recompute_policy(v1["product_ref"], policy).net_indemnity == 11

    # Correct plan 5 to residual 0 -> new version, two events, net 7.
    service.record_measurement(
        station=1, plan_no=5, level=1, center=100, scale=10, potency=100,
        registered_at="2026-01-05T08:00:00+00:00",
        arrived_at="2026-02-01T00:00:00+00:00",
    )
    v2 = service.publish_event_product("hist", station=1)
    assert v2["version"] == 2
    assert len(v2["snapshot"]["events"]) == 2
    assert service.recompute_policy(v2["product_ref"], policy).net_indemnity == 7

    # The retained version-1 product still reconstructs the original answer.
    recheck = service.recompute_policy("hist:v1", policy)
    assert recheck.net_indemnity == 11
    stored_v1 = service.get_event_product("hist:v1")
    assert stored_v1["snapshot"]["events"] == v1["snapshot"]["events"]


def test_same_event_product_recomputes_different_policies(service):
    _ingest_brief_archive(service)
    ref = service.publish_event_product("hist", station=1)["product_ref"]
    p1 = service.recompute_policy(ref, Policy.from_values(0, 6, 1, 1))
    p2 = service.recompute_policy(ref, Policy.from_values(0, 6, 1, 7))
    p3 = service.recompute_policy(ref, Policy.from_values(2, 6, 1, 1))
    assert p1.net_indemnity == 11
    assert p2.net_indemnity == 6
    # D=2: eligible losses are 0, 4, 4.  Keeping the right caps the last
    # claim at the remaining balance (0 + 4 + 2 = 6); restoring after #2
    # costs 1 but restores the full limit, giving 0 + 4 + 4 - 1 = 7.
    assert p3.net_indemnity == 7
    no_restore = evaluate_plan(
        service.accidents_from_product(ref),
        Policy.from_values(2, 6, 1, 1),
    )
    assert no_restore.net_indemnity == 6
    assert no_restore.rights_used == 0
    assert p3.rights_used == 1
    assert p3.decisions[1].restoration_used is True


def test_offline_snapshot_roundtrip(service, tmp_path):
    _ingest_brief_archive(service)
    product = service.publish_event_product("hist", station=1)
    path = service.export_event_product(product["product_ref"], tmp_path / "p.json")
    snapshot = json.loads(path.read_text())

    # Pure offline rebuild: no Store involved.
    events = events_from_product(snapshot)
    accidents = build_accidents(events, {(1, 1): 2, (1, 3): 6, (1, 5): 6})
    offline = optimize(accidents, Policy.from_values(0, 6, 1, 1))
    assert offline.net_indemnity == 11

    # Re-import the equivalent external product into a fresh database.
    fresh_store = Store(tmp_path / "fresh.sqlite")
    fresh = ControlService(fresh_store)
    imported = fresh.import_event_product(snapshot)
    assert imported["source"] == "external"
    assert imported["product_ref"] == product["product_ref"]
    # Idempotent re-import.
    again = fresh.import_event_product(snapshot)
    assert again["id"] == imported["id"]
    fresh_store.close()


def test_import_rejects_unknown_model(service):
    bad = {"model": "other", "product_ref": "x:v1", "version": 1,
           "scope": "x", "events": []}
    with pytest.raises(ValidationError):
        service.import_event_product(bad)


def test_conflicting_snapshot_under_same_ref_conflicts(service):
    _ingest_brief_archive(service)
    service.publish_event_product("hist", station=1)
    payload = {
        "model": PRODUCT_MODEL,
        "product_ref": "hist:v1",
        "scope": "hist",
        "version": 1,
        "events": [
            {
                "event_key": "S9-L9-P9-9", "station": 9, "level": 9,
                "first_plan_no": 9, "last_plan_no": 9,
                "last_registered_at": "t", "plan_numbers": [9],
                "alarms": [
                    {
                        "rules": ["single"], "residual": "2",
                        "predecessor_plan_no": None,
                        "predecessor_residual": None,
                        "record": {}, "predecessor_record": None,
                    }
                ],
            }
        ],
    }
    with pytest.raises(Exception):
        service.import_event_product(payload)


def test_late_arrival_merges_events_and_changes_deductible(service):
    for plan, residual in ((1, 2), (3, 2), (4, 2), (5, 2)):
        service.record_measurement(
            station=2, plan_no=plan, level=1, center=100, scale=10,
            potency=100 + 10 * residual,
            registered_at=f"2026-03-0{plan}T08:00:00+00:00",
        )
    service.record_measurement(
        station=2, plan_no=2, level=1, center=100, scale=10, potency=None,
        registered_at="2026-03-02T08:00:00+00:00", received=False,
    )
    for plan, loss in ((1, 4), (2, 3), (3, 3), (4, 3), (5, 3)):
        service.set_rework_loss(2, plan, loss)

    gap = service.publish_event_product("late", station=2)
    policy = Policy.from_values(3, 100, 0, 0)
    gap_plan = service.recompute_policy(gap["product_ref"], policy)
    assert [d.loss for d in gap_plan.decisions] == [4, 9]
    assert gap_plan.net_indemnity == 7  # (4-3) + (9-3)
    deductible_before = sum(d.loss - d.payable for d in gap_plan.decisions)
    assert deductible_before == 6

    # Late measurement fills plan 2 and merges the two events.
    service.record_measurement(
        station=2, plan_no=2, level=1, center=100, scale=10, potency=120,
        registered_at="2026-03-02T08:00:00+00:00",
        arrived_at="2026-03-06T10:00:00+00:00",
    )
    merged = service.publish_event_product("late", station=2)
    merged_plan = service.recompute_policy(merged["product_ref"], policy)
    assert len(merged_plan.decisions) == 1
    assert merged_plan.decisions[0].loss == 16
    assert merged_plan.net_indemnity == 13  # one D=3 retention only
    deductible_after = merged_plan.decisions[0].loss - merged_plan.decisions[0].payable
    assert deductible_after == 3
    # The old split product is still available.
    assert service.get_event_product(gap["product_ref"]) is not None


def test_trigger_source_records_are_embedded(service):
    _ingest_brief_archive(service)
    product = service.publish_event_product("hist", station=1)
    snapshot = product["snapshot"]
    first = snapshot["events"][0]["alarms"][0]
    assert first["rules"] == ["single"]
    assert first["record"]["potency"] == "120"
    assert first["record"]["source_ref"] is None  # no sample ref in this test


# --------------------------------------------------------------------------
# End-to-end: frozen potency enters through the existing potency entry point
# --------------------------------------------------------------------------
def test_potency_entry_feeds_control_archive(tmp_path):
    store = Store(tmp_path / "e2e.sqlite")
    potency = PotencyService(store)
    control = ControlService(store)
    from titration.curve_service import MethodParams

    store.create_method(
        "m", MethodParams(Fraction(0), Fraction(1000), Fraction(0), 2,
                          Fraction(0), 2), equivalence_index=2,
    )
    blank = potency.import_external_product(
        {"curve_name": "b", "method_name": "m", "k": 2,
         "selected_volumes": ["1", "50"], "external_ref": "b"}
    )
    sample = potency.import_external_product(
        {"curve_name": "s", "method_name": "m", "k": 2,
         "selected_volumes": ["1", "350"], "external_ref": "s"}
    )
    specimen = potency.create_sample(
        "ctrl", 10, 1, 2, 2,
        blank_product_id=blank["id"], sample_product_id=sample["id"],
    )
    value = potency.compute_sample(specimen["id"], 1, refresh=False)
    assert value["potency"] == "120"  # (350-50)*4/10

    control.record_measurement(
        station=1, plan_no=1, level=1, center=100, scale=10,
        potency=value["potency"], registered_at="t",
        source_ref=f"sample:{specimen['id']}",
    )
    product = control.publish_event_product("e2e", station=1)
    alarm = product["snapshot"]["events"][0]["alarms"][0]
    assert alarm["residual"] == "2"
    assert alarm["record"]["source_ref"] == f"sample:{specimen['id']}"
    store.close()
