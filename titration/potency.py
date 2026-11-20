"""Sample/batch potency warehouse.

Consumes published endpoint products only.  It never reads the private
candidate table: the public product's ``selected_volumes`` list is the sole
source of the j-th equivalence volume.

Potency of one sample::

    effective_volume = sample_product.selected_volumes[j-1]
                     - blank_product.selected_volumes[j-1]
    potency          = effective_volume * c * f / (m * p)

No extra unit conversion.  Negative effective volume is rejected; zero is
legal.  A batch average is weighted by net mass m, never by averaging
volumes first.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Sequence

from .exact import (
    NotFoundError,
    ValidationError,
    format_fraction,
    fraction_to_json,
    parse_fraction,
)
from .store import Store


def _validate_sample_inputs(
    *,
    net_mass: object,
    sample_fraction: object,
    concentration: object,
    factor: object,
) -> tuple[Fraction, Fraction, Fraction, Fraction]:
    m = parse_fraction(net_mass, field="net_mass")
    p = parse_fraction(sample_fraction, field="sample_fraction")
    c = parse_fraction(concentration, field="concentration")
    f = parse_fraction(factor, field="factor")
    if m <= 0:
        raise ValidationError("net_mass must be > 0")
    if not (0 < p <= 1):
        raise ValidationError("sample_fraction must satisfy 0 < p <= 1")
    if c <= 0:
        raise ValidationError("concentration must be > 0")
    if f <= 0:
        raise ValidationError("reaction factor must be > 0")
    return m, p, c, f


def _evaluate(
    sample_id: int,
    sample: dict,
    method_id: int,
    method_version: int,
    j: int,
    blank_product: dict | None,
    sample_product: dict | None,
    *,
    extra_failures: Sequence[str] = (),
) -> dict:
    """Pure per-sample evaluation from two public products.

    Only ``selected_volumes[j-1]`` is read; the private candidate table is not
    accessible here.
    """
    failures = list(extra_failures)

    def check_product(label: str, product: dict | None) -> Fraction | None:
        if product is None:
            return None
        if product["k"] < j:
            failures.append(f"{label}: product has K={product['k']} < j={j}")
            return None
        vols = product["selected_volumes"]
        if len(vols) != product["k"]:
            failures.append(
                f"{label}: product declares K={product['k']} but exposes "
                f"{len(vols)} endpoint volumes"
            )
            return None
        return parse_fraction(vols[j - 1], field=f"{label} j-th volume")

    blank_v = check_product("blank", blank_product)
    sample_v = check_product("sample", sample_product)

    effective: Fraction | None = None
    potency: Fraction | None = None
    if not failures and blank_v is not None and sample_v is not None:
        effective = sample_v - blank_v
        if effective < 0:
            failures.append(
                "negative effective volume: sample j-th volume "
                f"{format_fraction(sample_v)} < blank j-th volume "
                f"{format_fraction(blank_v)}"
            )
            effective = None
        else:
            potency = (
                effective
                * sample["concentration"]
                * sample["factor"]
                / (sample["net_mass"] * sample["sample_fraction"])
            )

    ok = not failures and potency is not None
    return {
        "sample_id": sample_id,
        "sample_name": sample["name"],
        "method_id": method_id,
        "method_version": method_version,
        "equivalence_index": j,
        "status": "OK" if ok else "FAILED",
        "failures": failures,
        "net_mass": fraction_to_json(sample["net_mass"]),
        "sample_fraction": fraction_to_json(sample["sample_fraction"]),
        "concentration": fraction_to_json(sample["concentration"]),
        "factor": fraction_to_json(sample["factor"]),
        "blank_product_id": None if blank_product is None else blank_product["id"],
        "blank_product_version": None
        if blank_product is None
        else {
            "curve_version": blank_product["curve_version"],
            "method_version": blank_product["method_version"],
        },
        "sample_product_id": None
        if sample_product is None
        else sample_product["id"],
        "sample_product_version": None
        if sample_product is None
        else {
            "curve_version": sample_product["curve_version"],
            "method_version": sample_product["method_version"],
        },
        "blank_j_volume": None if blank_v is None else fraction_to_json(blank_v),
        "sample_j_volume": None if sample_v is None else fraction_to_json(sample_v),
        "effective_volume": None
        if effective is None
        else fraction_to_json(effective),
        "potency": None if potency is None else fraction_to_json(potency),
    }


class PotencyService:
    def __init__(self, store: Store):
        self.store = store
        self._curve_service = None

    # Lazy import to avoid a circular dependency at module import time.
    def _curves(self):
        if self._curve_service is None:
            from .curve_service import CurveService

            self._curve_service = CurveService(self.store)
        return self._curve_service

    # -- sample management ------------------------------------------------
    def create_sample(
        self,
        name: str,
        net_mass: object,
        sample_fraction: object,
        concentration: object,
        factor: object,
        *,
        blank_curve_id: int | None = None,
        sample_curve_id: int | None = None,
        blank_product_id: int | None = None,
        sample_product_id: int | None = None,
    ) -> dict:
        if not name or not str(name).strip():
            raise ValidationError("sample name must be non-empty")
        m, p, c, f = _validate_sample_inputs(
            net_mass=net_mass,
            sample_fraction=sample_fraction,
            concentration=concentration,
            factor=factor,
        )
        uses_curves = blank_curve_id is not None or sample_curve_id is not None
        uses_products = blank_product_id is not None or sample_product_id is not None
        if uses_curves and uses_products:
            raise ValidationError(
                "provide either blank/sample curves or blank/sample products,"
                " not both"
            )
        if uses_curves:
            if blank_curve_id is None or sample_curve_id is None:
                raise ValidationError(
                    "both blank_curve_id and sample_curve_id are required"
                )
            self.store.get_curve(blank_curve_id)
            self.store.get_curve(sample_curve_id)
        elif uses_products:
            if blank_product_id is None or sample_product_id is None:
                raise ValidationError(
                    "both blank_product_id and sample_product_id are required"
                )
            self.store.get_endpoint_product(blank_product_id)
            self.store.get_endpoint_product(sample_product_id)
        else:
            raise ValidationError(
                "sample needs either curves or public endpoint products"
            )
        return self.store.create_sample(
            {
                "name": name,
                "net_mass": m,
                "sample_fraction": p,
                "concentration": c,
                "factor": f,
                "blank_curve_id": blank_curve_id,
                "sample_curve_id": sample_curve_id,
                "blank_product_id": blank_product_id,
                "sample_product_id": sample_product_id,
            }
        )

    def get_sample(self, sample_id: int) -> dict:
        return self.store.get_sample(sample_id)

    # -- product resolution ----------------------------------------------
    def _resolve_products(
        self,
        sample: dict,
        method_id: int,
        *,
        refresh: bool,
    ) -> dict:
        """Resolve public products for a sample.

        Curve-backed samples recompute the current public product when
        ``refresh`` is true, so a corrected curve or method threshold yields a
        new product and a genuinely different potency.  With ``refresh=False``
        (and for externally imported products) the already-bound product IDs
        are reused, which is how an old audit stays bound to its old inputs.
        Only public endpoint products are consulted here.
        """
        blank_id = sample["blank_product_id"]
        sample_prod_id = sample["sample_product_id"]
        failures: list[str] = []

        def resolve_one(label: str, curve_id, bound_id):
            if curve_id is not None and refresh:
                try:
                    product, _ = self._curves().publish_for_current(
                        curve_id, method_id
                    )
                    return product
                except ValidationError as exc:
                    failures.append(f"{label}: {exc}")
                    return None
            if bound_id is not None:
                try:
                    return self.store.get_endpoint_product(bound_id)
                except NotFoundError as exc:
                    failures.append(f"{label}: {exc}")
                    return None
            if curve_id is not None:
                try:
                    product, _ = self._curves().publish_for_current(
                        curve_id, method_id
                    )
                    return product
                except ValidationError as exc:
                    failures.append(f"{label}: {exc}")
                    return None
            failures.append(f"{label}: no curve or product identity")
            return None

        blank_product = resolve_one(
            "blank", sample["blank_curve_id"], blank_id
        )
        sample_product = resolve_one(
            "sample", sample["sample_curve_id"], sample_prod_id
        )
        return {
            "blank_product": blank_product,
            "sample_product": sample_product,
            "failures": failures,
        }

    # -- single sample ----------------------------------------------------
    def compute_sample(
        self,
        sample_id: int,
        method_id: int,
        *,
        refresh: bool = True,
    ) -> dict:
        method = self.store.get_method(method_id)
        j = method["equivalence_index"]
        if j is None:
            raise ValidationError(
                f"method {method_id} does not declare an equivalence index j"
            )
        sample = self.store.get_sample(sample_id)
        resolved = self._resolve_products(sample, method_id, refresh=refresh)
        if refresh and resolved["blank_product"] is not None and resolved[
            "sample_product"
        ] is not None:
            self.store.bind_sample_products(
                sample_id,
                resolved["blank_product"]["id"],
                resolved["sample_product"]["id"],
            )
        return _evaluate(
            sample_id,
            sample,
            method_id,
            method["version"],
            j,
            resolved["blank_product"],
            resolved["sample_product"],
            extra_failures=resolved["failures"],
        )

    # -- batch ------------------------------------------------------------
    def create_batch(self, name: str, method_id: int, sample_ids: Sequence[int]) -> dict:
        return self.store.create_batch(name, method_id, sample_ids)

    def compute_batch(
        self,
        batch_id: int,
        *,
        refresh: bool = True,
    ) -> dict:
        """Compute all parallel samples and the net-mass-weighted average.

        If any required sample fails, the report is INCOMPLETE, lists every
        failed sample and its reason, and reports no average.  Failed samples
        are never silently dropped.
        """
        batch = self.store.get_batch(batch_id)
        method = self.store.get_method(batch["method_id"])
        if method["equivalence_index"] is None:
            raise ValidationError(
                f"method {batch['method_id']} does not declare an"
                " equivalence index j"
            )
        results = [
            self.compute_sample(sid, batch["method_id"], refresh=refresh)
            for sid in batch["sample_ids"]
        ]
        failed = [r for r in results if r["status"] != "OK"]
        gaps = [
            {
                "sample_id": r["sample_id"],
                "sample_name": r["sample_name"],
                "failures": r["failures"],
            }
            for r in failed
        ]
        if failed:
            status = "INCOMPLETE"
            average = None
        else:
            status = "COMPLETE"
            total_mass = sum(
                (parse_fraction(r["net_mass"]) for r in results), Fraction(0)
            )
            weighted = sum(
                (
                    parse_fraction(r["net_mass"]) * parse_fraction(r["potency"])
                    for r in results
                ),
                Fraction(0),
            )
            average = weighted / total_mass
        details = {
            "batch_id": batch_id,
            "batch_name": batch["name"],
            "method_id": batch["method_id"],
            "method_version": method["version"],
            "equivalence_index": method["equivalence_index"],
            "samples": results,
            "gaps": gaps,
            "weighting": "net_mass",
        }
        return self.store.create_report(
            batch_id=batch_id,
            method_id=batch["method_id"],
            method_version=method["version"],
            status=status,
            average=average,
            details=details,
        )

    # -- external products -----------------------------------------------
    def import_external_product(self, payload: dict) -> dict:
        return self.store.import_endpoint_product(payload)

    def replay_report(self, report_id: int) -> dict:
        """Recompute a stored report from the inputs it recorded.

        This proves old accounting stays bound to old inputs even after curves
        or methods are corrected: the replay uses the exact public product IDs
        and the method version/``j`` captured in the report, never the current
        curve or current method definition.
        """
        report = self.store.get_report(report_id)
        method_version = report["details"].get("method_version")
        j = report["details"].get("equivalence_index")
        if method_version is None or j is None:
            raise ValidationError(
                f"report {report_id} lacks captured method version or j"
            )
        samples = []
        for entry in report["details"]["samples"]:
            blank_id = entry.get("blank_product_id")
            sample_id = entry.get("sample_product_id")
            if blank_id is None or sample_id is None:
                samples.append(
                    {
                        "sample_id": entry["sample_id"],
                        "status": "FAILED",
                        "failures": ["no public product IDs captured in report"],
                    }
                )
                continue
            result = self._compute_from_products(
                entry["sample_id"],
                report["method_id"],
                method_version,
                j,
                blank_id,
                sample_id,
            )
            samples.append(result)
        failed = [s for s in samples if s["status"] != "OK"]
        if failed:
            average = None
            status = "INCOMPLETE"
        else:
            total_mass = sum(
                (parse_fraction(s["net_mass"]) for s in samples), Fraction(0)
            )
            weighted = sum(
                (
                    parse_fraction(s["net_mass"]) * parse_fraction(s["potency"])
                    for s in samples
                ),
                Fraction(0),
            )
            average = weighted / total_mass
            status = "COMPLETE"
        return {
            "source_report_id": report_id,
            "status": status,
            "average": None if average is None else format_fraction(average),
            "samples": samples,
        }

    def _compute_from_products(
        self,
        sample_id: int,
        method_id: int,
        method_version: int,
        j: int,
        blank_product_id: int,
        sample_product_id: int,
    ) -> dict:
        sample = self.store.get_sample(sample_id)
        blank_product = self.store.get_endpoint_product(blank_product_id)
        sample_product = self.store.get_endpoint_product(sample_product_id)
        return _evaluate(
            sample_id,
            sample,
            method_id,
            method_version,
            j,
            blank_product,
            sample_product,
        )

    def get_report(self, report_id: int) -> dict:
        return self.store.get_report(report_id)

    def list_reports(self, batch_id: int | None = None) -> list[dict]:
        return self.store.list_reports(batch_id)
