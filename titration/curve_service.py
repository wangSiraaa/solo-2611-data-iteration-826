"""Curve service: candidates, selection, publication, explanations.

This service knows nothing about samples, weighing or dilution.  It owns
curves, methods and immutable published endpoint products.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Sequence

from .exact import (
    ConflictError,
    ValidationError,
    format_fraction,
    fraction_to_json,
    parse_fraction,
)
from .solver import (
    Candidate,
    SelectionResult,
    analyze_exclusions,
    solve_selection,
)
from .store import Store


@dataclass(frozen=True)
class MethodParams:
    low_volume: Fraction
    high_volume: Fraction
    threshold: Fraction
    k: int
    min_gap: Fraction
    equivalence_index: int | None = None

    @classmethod
    def from_values(
        cls,
        low_volume: object,
        high_volume: object,
        threshold: object,
        k: object,
        min_gap: object,
        equivalence_index: object = None,
    ) -> "MethodParams":
        lo = parse_fraction(low_volume, field="low_volume")
        hi = parse_fraction(high_volume, field="high_volume")
        t = parse_fraction(threshold, field="threshold")
        d = parse_fraction(min_gap, field="min_gap")
        if lo > hi:
            raise ValidationError("low_volume must be <= high_volume")
        if t < 0:
            raise ValidationError("threshold must be non-negative")
        if d < 0:
            raise ValidationError("min_gap must be non-negative")
        if isinstance(k, bool) or not isinstance(k, int):
            raise ValidationError("k must be a positive integer")
        if k <= 0:
            raise ValidationError("k must be a positive integer")
        j = equivalence_index
        if j is not None:
            if isinstance(j, bool) or not isinstance(j, int):
                raise ValidationError("equivalence_index must be an integer")
            if j < 1 or j > k:
                raise ValidationError("equivalence_index must satisfy 1 <= j <= K")
        return cls(lo, hi, t, k, d, j)

    def to_json(self) -> dict:
        return {
            "low_volume": fraction_to_json(self.low_volume),
            "high_volume": fraction_to_json(self.high_volume),
            "threshold": fraction_to_json(self.threshold),
            "k": self.k,
            "min_gap": fraction_to_json(self.min_gap),
            "equivalence_index": self.equivalence_index,
        }


def _validate_curve_points(
    volumes: Sequence[object], potentials: Sequence[object]
) -> tuple[list[Fraction], list[Fraction]]:
    if len(volumes) != len(potentials):
        raise ValidationError("volume and potential lists must have equal length")
    if len(volumes) > 300:
        raise ValidationError("at most 300 points per curve")
    if len(volumes) < 3:
        raise ValidationError("a curve needs at least 3 points for any candidate")
    vs = [parse_fraction(v, field=f"volume[{i}]") for i, v in enumerate(volumes)]
    es = [parse_fraction(e, field=f"potential[{i}]") for i, e in enumerate(potentials)]
    for i in range(1, len(vs)):
        if not vs[i] > vs[i - 1]:
            raise ValidationError(
                f"volumes must be strictly increasing (index {i})"
            )
    return vs, es


def compute_candidates(
    volumes: Sequence[Fraction],
    potentials: Sequence[Fraction],
    params: MethodParams,
) -> list[Candidate]:
    """All internal points with in-range volume and strength >= threshold.

    The strength uses only the stated discrete left/right slopes.  It is NOT a
    continuous-curve derivative or claimed inflection.
    """
    out: list[Candidate] = []
    for i in range(1, len(volumes) - 1):
        dv_left = volumes[i] - volumes[i - 1]
        dv_right = volumes[i + 1] - volumes[i]
        left = (potentials[i] - potentials[i - 1]) / dv_left
        right = (potentials[i + 1] - potentials[i]) / dv_right
        strength = abs(right - left)
        in_range = params.low_volume <= volumes[i] <= params.high_volume
        if in_range and strength >= params.threshold:
            out.append(
                Candidate(
                    index=i,
                    volume=volumes[i],
                    strength=strength,
                    left_slope=left,
                    right_slope=right,
                )
            )
    return out


def _candidate_json(c: Candidate) -> dict:
    return {
        "index": c.index,
        "volume": fraction_to_json(c.volume),
        "strength": fraction_to_json(c.strength),
        "left_slope": fraction_to_json(c.left_slope),
        "right_slope": fraction_to_json(c.right_slope),
    }


class CurveService:
    def __init__(self, store: Store):
        self.store = store

    # -- curve management -------------------------------------------------
    def create_curve(
        self,
        name: str,
        volumes: Iterable[object],
        potentials: Iterable[object],
    ) -> dict:
        vs, es = _validate_curve_points(list(volumes), list(potentials))
        return self.store.create_curve(name, vs, es)

    def update_curve(
        self,
        curve_id: int,
        volumes: Iterable[object],
        potentials: Iterable[object],
        *,
        expected_version: int,
    ) -> dict:
        vs, es = _validate_curve_points(list(volumes), list(potentials))
        return self.store.update_curve(
            curve_id,
            vs,
            es,
            expected_version=expected_version,
        )

    def get_curve(self, curve_id: int, *, version: int | None = None) -> dict:
        return self.store.get_curve(curve_id, version)

    def list_curves(self) -> list[dict]:
        return self.store.list_curves()

    # -- methods ----------------------------------------------------------
    def create_method(
        self,
        name: str,
        low_volume: object,
        high_volume: object,
        threshold: object,
        k: int,
        min_gap: object,
        equivalence_index: int | None = None,
    ) -> dict:
        p = MethodParams.from_values(
            low_volume, high_volume, threshold, k, min_gap, equivalence_index
        )
        return self.store.create_method(name, p, p.equivalence_index)

    def update_method(
        self,
        method_id: int,
        *,
        low_volume: object | None = None,
        high_volume: object | None = None,
        threshold: object | None = None,
        k: int | None = None,
        min_gap: object | None = None,
        equivalence_index: object | None = None,
        expected_version: int | None = None,
    ) -> dict:
        existing = self.store.get_method(method_id)
        p = MethodParams.from_values(
            existing["low_volume"] if low_volume is None else low_volume,
            existing["high_volume"] if high_volume is None else high_volume,
            existing["threshold"] if threshold is None else threshold,
            existing["k"] if k is None else k,
            existing["min_gap"] if min_gap is None else min_gap,
            existing["equivalence_index"]
            if equivalence_index is None
            else equivalence_index,
        )
        return self.store.update_method(
            method_id, p, p.equivalence_index, expected_version=expected_version
        )

    def get_method(self, method_id: int) -> dict:
        return self.store.get_method(method_id)

    # -- publication ------------------------------------------------------
    def publish_endpoints(
        self,
        curve_id: int,
        method_id: int,
        *,
        expected_curve_version: int | None = None,
        expected_method_version: int | None = None,
    ) -> dict:
        """Recompute candidates and publish one immutable endpoint product.

        Candidates and selected K points are always recomputed from the
        *current* curve and method rows (optionally pinned by expected
        versions).  A new publication never reuses an old endpoint.  If the
        contract cannot be met, this raises and publishes nothing.
        """
        curve = self.store.get_curve(curve_id)
        method = self.store.get_method(method_id)
        if (
            expected_curve_version is not None
            and curve["version"] != expected_curve_version
        ):
            raise ConflictError(
                f"curve {curve_id} version {curve['version']} != expected "
                f"{expected_curve_version}"
            )
        if (
            expected_method_version is not None
            and method["version"] != expected_method_version
        ):
            raise ConflictError(
                f"method {method_id} version {method['version']} != expected "
                f"{expected_method_version}"
            )
        params = MethodParams(
            method["low_volume"],
            method["high_volume"],
            method["threshold"],
            method["k"],
            method["min_gap"],
            method["equivalence_index"],
        )
        candidates = compute_candidates(
            curve["_volumes"], curve["_potentials"], params
        )
        selection = solve_selection(candidates, params.k, params.min_gap)
        if not selection.feasible:
            raise ValidationError(
                f"NO_ENDPOINT for curve {curve_id} / method {method_id}: "
                f"{selection.reason}"
            )
        explanation = self.explain(candidates, selection, params)
        input_snapshot = {
            "curve_id": curve_id,
            "curve_name": curve["name"],
            "curve_version": curve["version"],
            "volumes": curve["volumes"],
            "potentials": curve["potentials"],
            "method_id": method_id,
            "method_name": method["name"],
            "method_version": method["version"],
            "params": params.to_json(),
            "model": "discrete_left_right_slope",
        }
        return self.store.create_endpoint_product(
            curve_id=curve_id,
            curve_name=curve["name"],
            curve_version=curve["version"],
            method_id=method_id,
            method_name=method["name"],
            method_version=method["version"],
            k=params.k,
            selection=selection,
            explanation=explanation,
            candidates=candidates,
            input_snapshot=input_snapshot,
        )

    def publish_for_current(
        self, curve_id: int, method_id: int
    ) -> tuple[dict, bool]:
        """Publish (or return existing) product for current versions.

        Returns ``(product, created)``.
        """
        curve = self.store.get_curve(curve_id)
        method = self.store.get_method(method_id)
        existing = self.store.find_endpoint_product(
            curve_id, curve["version"], method_id, method["version"]
        )
        if existing is not None:
            return existing, False
        return self.publish_endpoints(curve_id, method_id), True

    # -- explanation ------------------------------------------------------
    def explain(
        self,
        candidates: Sequence[Candidate],
        selection: SelectionResult,
        params: MethodParams,
    ) -> dict:
        chosen = set(selection.volumes)
        exclusions = analyze_exclusions(
            candidates, params.k, params.min_gap, selection.objective
        )
        ordered = sorted(candidates, key=lambda c: c.volume)
        excluded = []
        selected = []
        for c in ordered:
            payload = _candidate_json(c)
            info = exclusions.get(c.index, {})
            if c.volume in chosen:
                payload["role"] = "selected"
                payload["best_total_with_point"] = fraction_to_json(
                    info.get("max_total_with_point")
                ) if info.get("max_total_with_point") is not None else None
                selected.append(payload)
                continue
            best = info.get("max_total_with_point")
            deficit = info.get("deficit")
            payload["role"] = "excluded"
            payload["best_total_with_point"] = (
                None if best is None else fraction_to_json(best)
            )
            payload["deficit_vs_optimum"] = (
                None if deficit is None else fraction_to_json(deficit)
            )
            if not info.get("feasible_with_point", False):
                payload["reason"] = (
                    "no legal K-selection can include this point under the "
                    "gap constraint"
                )
            elif info.get("tied_with_optimum"):
                payload["reason"] = (
                    "competes at equal best total strength; loses only on the "
                    "lexicographically-smallest ascending volume tie-break"
                )
            else:
                nearest = min(selection.volumes, key=lambda v: abs(v - c.volume))
                if abs(c.volume - nearest) < params.min_gap:
                    payload["reason"] = (
                        "spacing conflict with selected volume "
                        f"{format_fraction(nearest)} (gap < "
                        f"{format_fraction(params.min_gap)} mL)"
                    )
                else:
                    payload["reason"] = (
                        "adding it forces a total strength at least "
                        f"{format_fraction(deficit)} below the optimum"
                    )
            excluded.append(payload)
        return {
            "params": params.to_json(),
            "candidates": [_candidate_json(c) for c in ordered],
            "selected": selected,
            "objective": fraction_to_json(selection.objective),
            "excluded": excluded,
        }

    def get_endpoint_product(self, product_id: int) -> dict:
        return self.store.get_endpoint_product(product_id)

    def list_endpoint_products(self, **filters: Any) -> list[dict]:
        return self.store.list_endpoint_products(**filters)
