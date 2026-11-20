"""Reproducible validation entry point.

Builds the required synthetic scenarios, audits the DP solver against
exhaustive enumeration on every curve with fewer than ten points, and writes a
machine-readable result to ``results/validation.json``.

Each check is a named assertion so failures are attributable, not silent.
"""

from __future__ import annotations

import itertools
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from .curve_service import CurveService, MethodParams, compute_candidates
from .demo import curve_from_slopes, shift, BASE_SLOPES, BASE_VOLUMES
from .exact import format_fraction, fraction_to_json
from .potency import PotencyService
from .solver import (
    Candidate,
    brute_force_selection,
    solve_selection,
)
from .store import Store


def _F(x) -> Fraction:
    if isinstance(x, Fraction):
        return x
    if isinstance(x, int):
        return Fraction(x)
    return Fraction(x)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Fraction):
        return fraction_to_json(obj)
    return obj


def _cands(volumes, strengths) -> list[Candidate]:
    return [
        Candidate(
            index=i,
            volume=_F(v),
            strength=_F(s),
            left_slope=Fraction(0),
            right_slope=_F(s),
        )
        for i, (v, s) in enumerate(zip(volumes, strengths))
    ]


def run_validation(db_path: str, out_path: str) -> dict:
    checks: list[dict] = []

    def check(name: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            checks.append({"name": name, "passed": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - report, never swallow silently
            checks.append(
                {
                    "name": name,
                    "passed": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

    # 1. Uneven spacing: raw max slope is not the pair optimum.
    def uneven() -> str:
        cands = _cands([0, 2, 9, 10], [10, 60, 55, 1])
        res = solve_selection(cands, 2, Fraction(1))
        # Single max is v=2 (60); pair (2,9)=115, (2,10)=61.
        assert res.volumes == [Fraction(2), Fraction(9)]
        assert res.objective == Fraction(115)
        return "selected 2,9 (total 115) over raw-max 2 alone"

    check("uneven_spacing_pair_beats_raw_max_slope", uneven)

    # 2. Two-endpoint competition with equal totals -> lexicographic tie-break
    def tie() -> str:
        cands = _cands([1, 2, 7, 8], [5, 5, 5, 5])
        res = solve_selection(cands, 2, Fraction(1))
        # legal pairs: (1,2),(1,7),(1,8),(2,7),(2,8),(7,8); all total 10.
        assert res.volumes == [Fraction(1), Fraction(2)]
        bf = brute_force_selection(cands, 2, Fraction(1))
        assert res.volumes == bf.volumes
        return "equal totals resolved to lexicographically smallest list"

    check("equal_total_strength_lex_tiebreak", tie)

    # 3. NO_ENDPOINT is explicit, never a short selection.
    def no_ep() -> str:
        cands = _cands([0, 1, 2], [5, 5, 5])
        res = solve_selection(cands, 2, Fraction(3))
        assert res.feasible is False
        assert res.objective is None
        assert res.volumes == []
        return f"reason: {res.reason}"

    check("no_endpoint_explicit", no_ep)

    # 4. Exhaustive DP-vs-brute-force on every curve with < 10 points.
    def exhaustive() -> str:
        volume_sets = [
            [0, 1, 2, 5, 9],
            [0, 1, 4, 5, 8, 11, 12, 16, 17],
            [1, 2, 4, 8],
            [0, 3, 4, 9, 10, 11, 20],
        ]
        combos = 0
        for vols in volume_sets:
            strengths_space = itertools.product([1, 4, 9], repeat=len(vols))
            for strengths in strengths_space:
                for k in range(1, min(4, len(vols)) + 1):
                    for d in (0, 1, 2, 5):
                        cands = _cands(vols, strengths)
                        dp = solve_selection(cands, k, Fraction(d))
                        bf = brute_force_selection(cands, k, Fraction(d))
                        combos += 1
                        assert dp.feasible == bf.feasible, (vols, strengths, k, d)
                        assert dp.volumes == bf.volumes, (vols, strengths, k, d)
                        assert dp.objective == bf.objective, (
                            vols,
                            strengths,
                            k,
                            d,
                        )
        return f"{combos} (curve, strength, K, d) cases match exhaustive enumeration"

    check("dp_matches_exhaustive_under_ten_points", exhaustive)

    # 4b. Exclusion ("why not this competitor") analysis matches brute force.
    def exclusions_exact() -> str:
        def best_with_point(cands, k, d):
            n = len(cands)
            out = {}
            for p in range(n):
                best = None
                for combo in itertools.combinations(range(n), k):
                    if p not in combo:
                        continue
                    if all(
                        cands[b].volume - cands[a].volume >= d
                        for a, b in zip(combo, combo[1:])
                    ):
                        total = sum(
                            (cands[i].strength for i in combo), Fraction(0)
                        )
                        if best is None or total > best:
                            best = total
                out[cands[p].index] = best
            return out

        from .solver import analyze_exclusions

        cases = 0
        for vols in ([0, 1, 2, 3], [0, 1, 3, 6, 10], [0, 2, 5, 9]):
            for strengths in itertools.product([1, 3, 5], repeat=len(vols)):
                for k in range(1, min(3, len(vols)) + 1):
                    for d in (0, 1, 2):
                        cands = _cands(vols, strengths)
                        res = solve_selection(cands, k, Fraction(d))
                        if not res.feasible:
                            continue
                        analysis = analyze_exclusions(
                            cands, k, Fraction(d), res.objective
                        )
                        expected = best_with_point(cands, k, Fraction(d))
                        for idx, exp in expected.items():
                            got = analysis[idx]["max_total_with_point"]
                            assert got == exp, (vols, strengths, k, d, idx)
                            cases += 1
        return f"{cases} per-candidate optimum checks match brute force"

    check("exclusion_reasons_match_brute_force", exclusions_exact)

    # 5. 300-point / K=6 scale and gap invariant.
    def scale() -> str:
        volumes = [Fraction(i) for i in range(300)]
        potentials = [Fraction(0)]
        for i in range(299):
            potentials.append(
                potentials[-1] + (Fraction(100) if i % 7 else Fraction(1))
            )
        params = MethodParams(
            Fraction(0), Fraction(300), Fraction(0), 6, Fraction(3), 1
        )
        cands = compute_candidates(volumes, potentials, params)
        res = solve_selection(cands, 6, Fraction(3))
        assert res.feasible
        assert len(res.volumes) == 6
        assert all(
            b - a >= 3 for a, b in zip(res.volumes, res.volumes[1:])
        )
        return f"{len(cands)} candidates, selected {[format_fraction(v) for v in res.volumes]}"

    check("scale_300_points_k6", scale)

    # The rest exercise the full store, curve service and potency warehouse.
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    store = Store(path)
    curves = CurveService(store)
    potency = PotencyService(store)
    state: dict[str, Any] = {}

    def setup() -> str:
        base_v, base_e = curve_from_slopes(
            [Fraction(v) for v in BASE_VOLUMES],
            [Fraction(s) for s in BASE_SLOPES],
        )
        state["base"] = (base_v, base_e)
        state["blank_curve"] = curves.create_curve("blank", shift(base_v, Fraction(-1, 2)), base_e)
        state["sample_curve"] = curves.create_curve("sample", base_v, base_e)
        state["method"] = curves.create_method(
            "dual", 0, 30, 10, 2, 2, 2
        )
        return "curves + method created"

    check("setup_store", setup)

    def endpoints_public() -> str:
        product = curves.publish_endpoints(
            state["sample_curve"]["id"], state["method"]["id"]
        )
        state["sample_product"] = product
        assert product["selected_volumes"] == ["4", "12"]
        assert product["objective"] == "98"
        expl = product["explanation"]
        assert len(expl["selected"]) == 2
        for entry in expl["excluded"]:
            assert entry["reason"], entry
        state["explanation"] = expl
        return "published 4,12 with explained exclusions"

    check("endpoint_publication_and_explanation", endpoints_public)

    def two_endpoint_competition_store() -> str:
        # A curve with two strong regions and a decoy just inside the gap.
        vols = [Fraction(v) for v in (0, 1, 3, 4, 9, 10, 11, 18, 19)]
        slopes = [1, 1, 50, 49, 1, 48, 47, 1, 1]
        v, e = curve_from_slopes(vols, [Fraction(s) for s in slopes])
        c = curves.create_curve("competition", v, e)
        m = curves.create_method("competition", 0, 30, 5, 2, 2, 1)
        p = curves.publish_endpoints(c["id"], m["id"])
        vols_sel = [Fraction(x) for x in p["selected_volumes"]]
        assert len(vols_sel) == 2
        assert vols_sel[1] - vols_sel[0] >= 2
        return f"selected {p['selected_volumes']}"

    check("two_endpoint_competition_published", two_endpoint_competition_store)

    def negatives_and_zero() -> str:
        blank_p = potency.import_external_product(
            {
                "curve_name": "ext-blank",
                "method_name": "ext",
                "k": 2,
                "selected_volumes": ["5", "20"],
                "external_ref": "val-blank",
            }
        )
        neg_p = potency.import_external_product(
            {
                "curve_name": "ext-neg",
                "method_name": "ext",
                "k": 2,
                "selected_volumes": ["3", "10"],
                "external_ref": "val-neg",
            }
        )
        zero_p = potency.import_external_product(
            {
                "curve_name": "ext-zero",
                "method_name": "ext",
                "k": 2,
                "selected_volumes": ["3", "20"],
                "external_ref": "val-zero",
            }
        )
        m = curves.create_method("ext", 0, 30, 0, 2, 0, 2)
        neg = potency.create_sample(
            "neg", 10, 1, 1, 1,
            blank_product_id=blank_p["id"], sample_product_id=neg_p["id"],
        )
        r_neg = potency.compute_sample(neg["id"], m["id"], refresh=False)
        assert r_neg["status"] == "FAILED"
        assert r_neg["potency"] is None
        zero = potency.create_sample(
            "zero", 10, 1, 1, 1,
            blank_product_id=blank_p["id"], sample_product_id=zero_p["id"],
        )
        r_zero = potency.compute_sample(zero["id"], m["id"], refresh=False)
        assert r_zero["status"] == "OK"
        assert r_zero["effective_volume"] == "0"
        assert r_zero["potency"] == "0"
        state["ext_method"] = m
        state["zero_sample"] = zero
        return "negative rejected, zero accepted with zero potency"

    check("negative_and_zero_effective_volume", negatives_and_zero)

    def sample_weights() -> str:
        base_v, base_e = state["base"]
        method = state["method"]
        a = curves.create_curve("w-A", shift(base_v, Fraction(0)), base_e)
        b = curves.create_curve("w-B", shift(base_v, Fraction(-1, 4)), base_e)
        sa = potency.create_sample(
            "A", 100, Fraction(1, 2), 2, 1,
            blank_curve_id=state["blank_curve"]["id"], sample_curve_id=a["id"],
        )
        sb = potency.create_sample(
            "B", 300, Fraction(1, 2), 2, 1,
            blank_curve_id=state["blank_curve"]["id"], sample_curve_id=b["id"],
        )
        batch = potency.create_batch("weights", method["id"], [sa["id"], sb["id"]])
        report = potency.compute_batch(batch["id"])
        assert report["status"] == "COMPLETE"
        per = {s["sample_name"]: Fraction(s["potency"]) for s in report["details"]["samples"]}
        weighted = (100 * per["A"] + 300 * per["B"]) / 400
        simple = (per["A"] + per["B"]) / 2
        assert report["average"] == weighted
        assert report["average"] != simple
        state["weight_batch"] = batch
        state["weight_report"] = report
        return f"weighted average {fraction_to_json(weighted)} != simple mean {fraction_to_json(simple)}"

    check("unequal_net_mass_weighting", sample_weights)

    def incomplete_batch() -> str:
        method = state["ext_method"]
        good = state["zero_sample"]
        neg_p = potency.import_external_product(
            {
                "curve_name": "ext-neg2",
                "method_name": "ext",
                "k": 2,
                "selected_volumes": ["3", "10"],
                "external_ref": "val-neg2",
            }
        )
        blank_p = store.get_endpoint_product(
            potency.get_sample(good["id"])["blank_product_id"]
        )
        bad = potency.create_sample(
            "bad", 10, 1, 1, 1,
            blank_product_id=blank_p["id"], sample_product_id=neg_p["id"],
        )
        batch = potency.create_batch("incomplete", method["id"], [good["id"], bad["id"]])
        report = potency.compute_batch(batch["id"], refresh=False)
        assert report["status"] == "INCOMPLETE"
        assert report["average"] is None
        assert len(report["details"]["gaps"]) == 1
        assert len(report["details"]["samples"]) == 2
        return "status INCOMPLETE, one gap listed, no average"

    check("incomplete_batch_lists_gaps", incomplete_batch)

    def recompute() -> str:
        method = state["method"]
        base_v, base_e = state["base"]
        sample = potency.create_sample(
            "recalc", 100, 1, 2, 1,
            blank_curve_id=state["blank_curve"]["id"],
            sample_curve_id=state["sample_curve"]["id"],
        )
        batch = potency.create_batch("recalc", method["id"], [sample["id"]])
        r1 = potency.compute_batch(batch["id"])
        old_id = r1["id"]
        old_pot = r1["details"]["samples"][0]["potency"]
        shifted_v, shifted_e = curve_from_slopes(
            shift(base_v, Fraction(2)),
            [Fraction(s) for s in BASE_SLOPES],
        )
        curves.update_curve(
            state["sample_curve"]["id"], shifted_v, shifted_e, expected_version=1
        )
        r2 = potency.compute_batch(batch["id"])
        new_pot = r2["details"]["samples"][0]["potency"]
        assert new_pot != old_pot
        assert store.get_report(old_id)["details"]["samples"][0]["potency"] == old_pot
        replay = potency.replay_report(old_id)
        assert replay["samples"][0]["potency"] == old_pot
        return f"old {old_pot} -> new {new_pot}; replay of old report stays {old_pot}"

    check("correction_changes_potency_and_old_report_immutable", recompute)

    def threshold_recompute() -> str:
        # A real curve where raising the threshold removes a tie-breaking
        # competitor, so the selected pair genuinely changes (same optimum).
        vols = [Fraction(v) for v in (0, 3, 5, 11, 13, 16, 18, 19)]
        slopes = [Fraction(s) for s in (0, 5, 11, 4, 13, 5, 10)]
        v, e = curve_from_slopes(vols, slopes)
        c = curves.create_curve("thr", v, e)
        m = curves.create_method("thr", 0, 30, 1, 2, 4, 1)
        p1 = curves.publish_endpoints(c["id"], m["id"])
        assert p1["selected_volumes"] == ["5", "13"]
        curves.update_method(m["id"], threshold=7)
        p2 = curves.publish_endpoints(c["id"], m["id"])
        assert p2["id"] != p1["id"]
        assert p2["selected_volumes"] == ["11", "16"]
        assert p1["objective"] == p2["objective"] == "15"
        # A too-high threshold must yield NO_ENDPOINT, never a stale pair.
        curves.update_method(m["id"], threshold=60)
        try:
            curves.publish_endpoints(c["id"], m["id"])
            raised = False
        except Exception:
            raised = True
        assert raised
        return (
            f"threshold 1 -> {p1['selected_volumes']}, threshold 7 -> "
            f"{p2['selected_volumes']} (same total 15), threshold 60 -> "
            "NO_ENDPOINT"
        )

    check("threshold_correction_new_product", threshold_recompute)

    def no_candidate_table() -> str:
        product = state["sample_product"]
        store.conn.execute(
            "UPDATE endpoint_products SET candidates_json='[]' WHERE id = ?",
            (product["id"],),
        )
        store.conn.commit()
        blank_product = curves.publish_endpoints(
            state["blank_curve"]["id"], state["method"]["id"]
        )
        s = potency.create_sample(
            "no-private",
            100,
            1,
            2,
            1,
            blank_product_id=blank_product["id"],
            sample_product_id=product["id"],
        )
        r = potency.compute_sample(s["id"], state["method"]["id"], refresh=False)
        assert r["status"] == "OK"
        return "potency computed with private candidates deleted"

    check("potency_uses_only_public_products", no_candidate_table)

    store.close()

    passed = sum(1 for c in checks if c["passed"])
    result = {
        "passed": passed,
        "failed": len(checks) - passed,
        "checks": checks,
        "db": str(db_path),
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    return _jsonable(result)
