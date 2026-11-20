import json
from fractions import Fraction
from pathlib import Path

import pytest

from titration import (
    CurveService,
    MethodParams,
    PotencyService,
    Store,
    ValidationError,
    compute_candidates,
    brute_force_selection,
    solve_selection,
)
from titration.solver import Candidate
from titration.exact import parse_fraction
from titration.demo import curve_from_slopes


def F(x):
    return parse_fraction(x)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.sqlite")
    yield s
    s.close()


@pytest.fixture
def curves(store):
    return CurveService(store)


@pytest.fixture
def potency(store):
    return PotencyService(store)


def make_candidates(volumes, strengths):
    return [
        Candidate(
            index=i,
            volume=F(v),
            strength=F(s),
            left_slope=F(0),
            right_slope=F(s),
        )
        for i, (v, s) in enumerate(zip(volumes, strengths))
    ]


# -- solver ---------------------------------------------------------------
def test_uneven_spacing_and_max_slope_trap():
    # The largest single strength is at v=5, but uneven spacing makes the
    # combined optimum the pair (4, 12).
    cands = make_candidates([2, 5, 12, 15], [30, 45, 40, 40])
    res = solve_selection(cands, 2, F(2))
    assert res.feasible
    # (5,12) and (5,15) both total 85; lexicographically smallest wins.
    assert res.volumes == [F(5), F(12)]
    assert res.objective == F(85)


def test_two_endpoint_competition():
    cands = make_candidates([1, 3, 10, 12], [20, 20, 19, 19])
    res = solve_selection(cands, 2, F(2))
    # Both pairs total 40; lexicographically smallest list wins.
    assert res.volumes == [F(1), F(3)]
    assert res.objective == F(40)


def test_equal_total_strength_lex_tiebreak_exhaustive():
    # Legal pairs (1,3), (1,7), (2,7), (3,7) all total 10; lex smallest is
    # [1,3].
    cands = make_candidates([1, 2, 3, 7], [5, 5, 5, 5])
    res = solve_selection(cands, 2, F(2))
    assert res.volumes == [F(1), F(3)]
    bf = brute_force_selection(cands, 2, F(2))
    assert res.volumes == bf.volumes
    assert res.objective == bf.objective


def test_no_endpoint_insufficient_combination():
    cands = make_candidates([1, Fraction(3, 2), 2], [10, 10, 10])
    res = solve_selection(cands, 2, F(2))
    assert not res.feasible
    assert res.reason
    assert res.objective is None


def test_k_less_than_candidates():
    cands = make_candidates([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6])
    res = solve_selection(cands, 3, F(1))
    assert res.feasible
    assert len(res.volumes) == 3


def test_gap_zero_allows_adjacent():
    cands = make_candidates([1, 2], [1, 2])
    res = solve_selection(cands, 2, F(0))
    assert res.volumes == [F(1), F(2)]


@pytest.mark.parametrize(
    "vols,strengths,k,d",
    [
        ([0, 1, 2, 3], [1, 5, 4, 9], 2, 1),
        ([0, 1, 2, 3, 4], [3, 1, 4, 1, 5], 2, 2),
        ([0, 1, 2, 3, 4], [3, 1, 4, 1, 5], 3, 1),
        ([0, 2, 5, 9], [1, 1, 1, 1], 2, 3),
    ],
)
def test_dp_matches_brute_force(vols, strengths, k, d):
    cands = make_candidates(vols, strengths)
    dp = solve_selection(cands, k, F(d))
    bf = brute_force_selection(cands, k, F(d))
    assert dp.feasible == bf.feasible
    assert dp.volumes == bf.volumes
    assert dp.objective == bf.objective


def test_brute_force_on_under_ten_points_exhaustive():
    import itertools

    for vols in ([0, 1, 3, 6, 10], [0, 2, 3, 7, 8, 9], [1, 2, 4, 8]):
        for strengths in itertools.product([1, 3, 5], repeat=len(vols)):
            for k in range(1, min(3, len(vols)) + 1):
                for d in (0, 1, 3):
                    cands = make_candidates(vols, strengths)
                    dp = solve_selection(cands, k, F(d))
                    bf = brute_force_selection(cands, k, F(d))
                    assert dp.feasible == bf.feasible, (vols, strengths, k, d)
                    assert dp.volumes == bf.volumes, (vols, strengths, k, d)
                    assert dp.objective == bf.objective


def _brute_best_with_point(cands, k, d):
    from itertools import combinations

    out = {}
    n = len(cands)
    for p in range(n):
        best = None
        for combo in combinations(range(n), k):
            if p not in combo:
                continue
            if all(
                cands[b].volume - cands[a].volume >= d
                for a, b in zip(combo, combo[1:])
            ):
                total = sum((cands[i].strength for i in combo), Fraction(0))
                if best is None or total > best:
                    best = total
        out[cands[p].index] = best
    return out


def test_exclusion_analysis_matches_brute_force():
    # Regression: with d=0 the candidate must not be counted on both sides
    # of the prefix/suffix split.
    import itertools

    from titration.solver import analyze_exclusions

    for vols in ([0, 1, 2, 3], [0, 1, 3, 6, 10], [0, 2, 5, 9]):
        for strengths in itertools.product([1, 3, 5], repeat=len(vols)):
            for k in range(1, min(3, len(vols)) + 1):
                for d in (0, 1, 2):
                    cands = make_candidates(vols, strengths)
                    res = solve_selection(cands, k, F(d))
                    if not res.feasible:
                        continue
                    analysis = analyze_exclusions(cands, k, F(d), res.objective)
                    expected = _brute_best_with_point(cands, k, F(d))
                    for idx, exp in expected.items():
                        got = analysis[idx]["max_total_with_point"]
                        assert got == exp, (vols, strengths, k, d, idx)
                        if exp is not None:
                            assert analysis[idx]["deficit"] == res.objective - exp


# -- candidates -----------------------------------------------------------
def test_candidate_strength_uses_uneven_slopes():
    volumes = [F(0), F(1), F(5), F(6)]
    potentials = [F(0), F(1), F(41), F(42)]
    params = MethodParams(F(0), F(10), F(0), 1, F(0))
    cands = compute_candidates(volumes, potentials, params)
    # internal volumes 1 and 5.
    by_v = {c.volume: c for c in cands}
    # at v=1: L=1, R=40/4=10 -> s=9
    assert by_v[F(1)].strength == F(9)
    # at v=5: L=40/4=10, R=1 -> s=9
    assert by_v[F(5)].strength == F(9)


def test_candidate_range_and_threshold_filters():
    volumes = [F(i) for i in range(6)]
    potentials = [F(0), F(0), F(10), F(10), F(20), F(20)]
    params = MethodParams(F(1), F(3), F(5), 1, F(0))
    cands = compute_candidates(volumes, potentials, params)
    assert all(params.low_volume <= c.volume <= params.high_volume for c in cands)
    assert all(c.strength >= params.threshold for c in cands)
    assert [c.volume for c in cands] == [F(1), F(2), F(3)]


def test_endpoints_are_never_candidates():
    volumes = [F(0), F(1), F(2), F(3)]
    potentials = [F(0), F(5), F(10), F(50)]
    params = MethodParams(F(0), F(3), F(0), 1, F(0))
    cands = compute_candidates(volumes, potentials, params)
    assert {c.volume for c in cands} == {F(1), F(2)}


# -- curve publication ----------------------------------------------------
def two_spike_curve(curves, name, shift=0):
    volumes = [F(v) + F(shift) for v in (0, 1, 4, 5, 8, 11, 12, 16, 17)]
    es = [F(0)]
    slopes = [1, 1, 60, 40, 40, 40, 1, 1]
    for dv, s in zip(
        (b - a for a, b in zip(volumes, volumes[1:])), slopes
    ):
        es.append(es[-1] + dv * s)
    return curves.create_curve(name, volumes, es)


def test_publish_and_explain(curves):
    c = two_spike_curve(curves, "c")
    m = curves.create_method("m", 0, 30, 10, 2, 2, 2)
    p = curves.publish_endpoints(c["id"], m["id"])
    assert p["selected_volumes"] == ["4", "12"]
    expl = p["explanation"]
    assert [s["volume"] for s in expl["selected"]] == ["4", "12"]
    # Every excluded competitor must have an explanation.
    for e in expl["excluded"]:
        assert e["reason"]
    assert expl["objective"] == "98"
    assert len(expl["candidates"]) >= len(expl["selected"])


def test_publish_no_endpoint_raises(curves):
    c = two_spike_curve(curves, "narrow")
    m = curves.create_method("m", 0, 30, 9999, 2, 2, 1)
    with pytest.raises(ValidationError, match="NO_ENDPOINT"):
        curves.publish_endpoints(c["id"], m["id"])
    assert curves.list_endpoint_products(curve_id=c["id"]) == []


def test_update_curve_creates_new_product_not_reusing_old(curves):
    c = two_spike_curve(curves, "c")
    m = curves.create_method("m", 0, 30, 10, 2, 2, 2)
    p1 = curves.publish_endpoints(c["id"], m["id"])
    shifted_v, shifted_e = curve_from_slopes(
        [F(v) + 1 for v in (0, 1, 4, 5, 8, 11, 12, 16, 17)],
        [F(s) for s in (1, 1, 60, 40, 40, 40, 1, 1)],
    )
    curves.update_curve(c["id"], shifted_v, shifted_e, expected_version=1)
    p2 = curves.publish_endpoints(c["id"], m["id"])
    assert p1["id"] != p2["id"]
    assert p1["selected_volumes"] != p2["selected_volumes"]
    assert p2["selected_volumes"] == ["5", "13"]
    assert p2["curve_version"] == 2


def test_method_threshold_change_new_product(curves):
    c = two_spike_curve(curves, "c")
    m = curves.create_method("m", 0, 30, 10, 2, 2, 2)
    p1 = curves.publish_endpoints(c["id"], m["id"])
    m2 = curves.update_method(m["id"], threshold=25)
    assert m2["version"] == 2
    p2 = curves.publish_endpoints(c["id"], m["id"])
    assert p2["id"] != p1["id"]
    assert p2["method_version"] == 2


def test_threshold_change_can_change_selection(curves):
    # Two equal-total selections where one competitor is removed by a raised
    # threshold; the tie-break then selects a genuinely different pair.
    volumes = [F(v) for v in (0, 3, 5, 11, 13, 16, 18, 19)]
    slopes = [F(s) for s in (0, 5, 11, 4, 13, 5, 10)]
    v, e = curve_from_slopes(volumes, slopes)
    c = curves.create_curve("tie", v, e)
    m = curves.create_method("tie", 0, 30, 1, 2, 4, 1)
    p1 = curves.publish_endpoints(c["id"], m["id"])
    assert p1["selected_volumes"] == ["5", "13"]
    curves.update_method(m["id"], threshold=7)
    p2 = curves.publish_endpoints(c["id"], m["id"])
    assert p2["selected_volumes"] == ["11", "16"]
    assert p1["objective"] == p2["objective"] == "15"


def test_expected_version_conflict(curves):
    c = two_spike_curve(curves, "c")
    m = curves.create_method("m", 0, 30, 10, 2, 2, 2)
    with pytest.raises(Exception):
        curves.publish_endpoints(
            c["id"], m["id"], expected_curve_version=99
        )


# -- potency --------------------------------------------------------------
def _curves_and_method(curves, shift=0):
    c = two_spike_curve(curves, "sample", shift)
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    return c, m


def test_potency_exact_formula(store, curves, potency):
    blank = two_spike_curve(curves, "blank", -1)
    sample = two_spike_curve(curves, "sample", 1)
    # blank endpoints: 3, 11 ; sample: 5, 13 -> effective j=2: 2
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    s = potency.create_sample(
        "s",
        net_mass=100,
        sample_fraction=F(1) / 2,
        concentration=3,
        factor=F(2),
        blank_curve_id=blank["id"],
        sample_curve_id=sample["id"],
    )
    r = potency.compute_sample(s["id"], m["id"])
    assert r["status"] == "OK"
    assert r["effective_volume"] == "2"
    # 2 * 3 * 2 / (100 * 1/2) = 12/50 = 6/25
    assert r["potency"] == "6/25"


def test_negative_effective_volume_rejected(store, potency):
    blank_p = potency.import_external_product(
        {
            "curve_name": "b",
            "method_name": "m",
            "k": 2,
            "selected_volumes": ["5", "20"],
        }
    )
    sample_p = potency.import_external_product(
        {
            "curve_name": "s",
            "method_name": "m",
            "k": 2,
            "selected_volumes": ["3", "10"],
        }
    )
    m = store.create_method(
        "m", MethodParams(F(0), F(30), F(0), 2, F(0), 2)
    )
    s = potency.create_sample(
        "neg",
        net_mass=10,
        sample_fraction=1,
        concentration=1,
        factor=1,
        blank_product_id=blank_p["id"],
        sample_product_id=sample_p["id"],
    )
    r = potency.compute_sample(s["id"], m["id"], refresh=False)
    assert r["status"] == "FAILED"
    assert any("negative" in f for f in r["failures"])
    assert r["potency"] is None


def test_zero_effective_volume_legal(store, potency):
    blank_p = potency.import_external_product(
        {"curve_name": "b", "method_name": "m", "k": 2,
         "selected_volumes": ["5", "20"]}
    )
    sample_p = potency.import_external_product(
        {"curve_name": "s", "method_name": "m", "k": 2,
         "selected_volumes": ["3", "20"]}
    )
    m = store.create_method("m", MethodParams(F(0), F(30), F(0), 2, F(0), 2))
    s = potency.create_sample(
        "zero", 10, 1, 1, 1,
        blank_product_id=blank_p["id"], sample_product_id=sample_p["id"],
    )
    r = potency.compute_sample(s["id"], m["id"], refresh=False)
    assert r["status"] == "OK"
    assert r["effective_volume"] == "0"
    assert r["potency"] == "0"


def test_product_with_fewer_endpoints_than_j_fails(store, potency):
    blank_p = potency.import_external_product(
        {"curve_name": "b", "method_name": "m", "k": 1,
         "selected_volumes": ["5"]}
    )
    sample_p = potency.import_external_product(
        {"curve_name": "s", "method_name": "m", "k": 1,
         "selected_volumes": ["8"]}
    )
    m = store.create_method("m", MethodParams(F(0), F(30), F(0), 2, F(0), 2))
    s = potency.create_sample(
        "short", 10, 1, 1, 1,
        blank_product_id=blank_p["id"], sample_product_id=sample_p["id"],
    )
    r = potency.compute_sample(s["id"], m["id"], refresh=False)
    assert r["status"] == "FAILED"
    assert any("K=1 < j=2" in f for f in r["failures"])


def test_batch_weighted_by_net_mass(store, curves, potency):
    blank = two_spike_curve(curves, "blank", -1)
    sample_a = two_spike_curve(curves, "a", 1)
    sample_b = two_spike_curve(curves, "b", 2)
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    sa = potency.create_sample(
        "a", 100, 1, 2, 2,
        blank_curve_id=blank["id"], sample_curve_id=sample_a["id"],
    )
    sb = potency.create_sample(
        "b", 300, 1, 2, 2,
        blank_curve_id=blank["id"], sample_curve_id=sample_b["id"],
    )
    batch = potency.create_batch("batch", m["id"], [sa["id"], sb["id"]])
    report = potency.compute_batch(batch["id"])
    assert report["status"] == "COMPLETE"
    per = {s["sample_name"]: F(s["potency"]) for s in report["details"]["samples"]}
    weighted = (100 * per["a"] + 300 * per["b"]) / 400
    assert report["average"] == weighted
    # Not the simple mean.
    assert report["average"] != (per["a"] + per["b"]) / 2


def test_incomplete_batch_lists_gaps_no_average(store, curves, potency):
    blank = two_spike_curve(curves, "blank", -1)
    sample_a = two_spike_curve(curves, "a", 1)
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    good = potency.create_sample(
        "good", 100, 1, 2, 2,
        blank_curve_id=blank["id"], sample_curve_id=sample_a["id"],
    )
    bp = potency.import_external_product(
        {"curve_name": "b", "method_name": "m", "k": 2,
         "selected_volumes": ["5", "20"]}
    )
    sp = potency.import_external_product(
        {"curve_name": "s", "method_name": "m", "k": 2,
         "selected_volumes": ["3", "10"]}
    )
    bad = potency.create_sample(
        "bad", 100, 1, 1, 1,
        blank_product_id=bp["id"], sample_product_id=sp["id"],
    )
    batch = potency.create_batch("batch", m["id"], [good["id"], bad["id"]])
    report = potency.compute_batch(batch["id"])
    assert report["status"] == "INCOMPLETE"
    assert report["average"] is None
    assert len(report["details"]["gaps"]) == 1
    assert report["details"]["gaps"][0]["sample_name"] == "bad"
    # Both samples remain present in details (not silently dropped).
    assert len(report["details"]["samples"]) == 2


def test_recompute_after_curve_correction_changes_result(store, curves, potency):
    blank = two_spike_curve(curves, "blank", -1)
    sample = two_spike_curve(curves, "sample", 1)
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    s = potency.create_sample(
        "s", 100, 1, 2, 2,
        blank_curve_id=blank["id"], sample_curve_id=sample["id"],
    )
    batch = potency.create_batch("batch", m["id"], [s["id"]])
    r1 = potency.compute_batch(batch["id"])
    old_id, old_pot = r1["id"], r1["details"]["samples"][0]["potency"]
    # Correct sample curve by +2 mL so the selected endpoints really move.
    shifted_v, shifted_e = curve_from_slopes(
        [F(v) + 2 for v in (0, 1, 4, 5, 8, 11, 12, 16, 17)],
        [F(s) for s in (1, 1, 60, 40, 40, 40, 1, 1)],
    )
    curves.update_curve(sample["id"], shifted_v, shifted_e, expected_version=1)
    r2 = potency.compute_batch(batch["id"])
    new_pot = r2["details"]["samples"][0]["potency"]
    assert new_pot != old_pot
    # Old report is unchanged and bound to old inputs.
    stored = store.get_report(old_id)
    assert stored["details"]["samples"][0]["potency"] == old_pot
    # A refresh=False run reuses the currently bound products (the new ones).
    r3 = potency.compute_batch(batch["id"], refresh=False)
    assert r3["details"]["samples"][0]["potency"] == new_pot
    # Replaying the *old* report stays bound to its old product IDs.
    replay = potency.replay_report(old_id)
    assert replay["samples"][0]["potency"] == old_pot


def test_method_threshold_correction_changes_potency(store, curves, potency):
    blank = two_spike_curve(curves, "blank", -1)
    sample = two_spike_curve(curves, "sample", 1)
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    s = potency.create_sample(
        "s", 100, 1, 2, 2,
        blank_curve_id=blank["id"], sample_curve_id=sample["id"],
    )
    batch = potency.create_batch("batch", m["id"], [s["id"]])
    r1 = potency.compute_batch(batch["id"])
    curves.update_method(m["id"], threshold=60)
    report = potency.compute_batch(batch["id"])
    # At threshold 60 only the v=4 spike survives as a candidate, so K=2 is
    # infeasible.  The sample must be reported failed, not silently reused.
    assert report["status"] == "INCOMPLETE"
    assert any(
        "NO_ENDPOINT" in f
        for f in report["details"]["gaps"][0]["failures"]
    )


def test_external_product_import(store, potency):
    payload = {
        "curve_name": "ext-curve",
        "method_name": "ext-method",
        "k": 2,
        "selected_volumes": ["2/3", "5"],
        "objective": "7/2",
        "external_ref": "abc",
    }
    p = potency.import_external_product(payload)
    assert p["selected_volumes"] == ["2/3", "5"]
    assert p["source"] == "external"
    # Idempotent by external_ref.
    p2 = potency.import_external_product(payload)
    assert p2["id"] == p["id"]


def test_external_product_validation(potency):
    with pytest.raises(ValidationError):
        potency.import_external_product({"curve_name": "x"})
    with pytest.raises(ValidationError):
        potency.import_external_product(
            {"curve_name": "x", "method_name": "y", "k": 2,
             "selected_volumes": ["1"]}
        )


def test_potency_service_does_not_read_candidates(store, curves, potency):
    """Delete private candidate JSON from the product row; potency still works
    because it only uses the public selected volumes."""
    blank = two_spike_curve(curves, "blank", -1)
    sample = two_spike_curve(curves, "sample", 1)
    m = curves.create_method("m", 0, 40, 10, 2, 2, 2)
    bp = curves.publish_endpoints(blank["id"], m["id"])
    sp = curves.publish_endpoints(sample["id"], m["id"])
    store.conn.execute(
        "UPDATE endpoint_products SET candidates_json='[]' WHERE id IN (?, ?)",
        (bp["id"], sp["id"]),
    )
    store.conn.commit()
    s = potency.create_sample(
        "s", 100, 1, 2, 2,
        blank_product_id=bp["id"], sample_product_id=sp["id"],
    )
    r = potency.compute_sample(s["id"], m["id"], refresh=False)
    assert r["status"] == "OK"
    assert r["potency"] is not None


def test_scale_300_points_k6(store, curves):
    volumes = [F(i) for i in range(300)]
    potentials = []
    acc = F(0)
    for i in range(299):
        slope = F(1) if i % 7 else F(100)
        acc += slope
        potentials.append(acc)
    potentials.insert(0, F(0))
    c = curves.create_curve("big", volumes, potentials)
    m = curves.create_method("m", 0, 300, 0, 6, 3, 1)
    p = curves.publish_endpoints(c["id"], m["id"])
    assert len(p["selected_volumes"]) == 6
    vols = [F(v) for v in p["selected_volumes"]]
    assert all(b - a >= 3 for a, b in zip(vols, vols[1:]))


def test_rational_sentinel_and_inexact_float_rejected(curves):
    with pytest.raises(ValidationError):
        curves.create_curve("x", [0.1, 0.2, 0.3], [1, 2, 3])
    c = curves.create_curve("ok", ["0", "1/3", "2/3"], [0, 1, 2])
    assert c["volumes"] == ["0", "1/3", "2/3"]


def test_duplicate_or_decreasing_volume_rejected(curves):
    with pytest.raises(ValidationError):
        curves.create_curve("x", [0, 1, 1], [0, 1, 2])
    with pytest.raises(ValidationError):
        curves.create_curve("x", [0, 2, 1], [0, 1, 2])
