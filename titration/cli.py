"""Command-line entry points.

Subcommands
-----------
``curve-create`` / ``curve-update`` / ``curve-show`` / ``curve-list``
``method-create`` / ``method-update`` / ``method-show``
``publish``          publish an immutable endpoint product
``explain``          show candidates, selection and excluded competitors
``sample-create`` / ``sample-show`` / ``sample-eval``
``batch-create`` / ``batch-eval`` / ``report-show``
``import-product``   consume an external same-format product
``demo``             build the synthetic validation scenario end-to-end
``validate``         run the self-checks and write a JSON result
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .curve_service import CurveService
from .exact import (
    ConflictError,
    NotFoundError,
    ValidationError,
    fraction_to_json,
)
from .potency import PotencyService
from .store import Store


def _store(args: argparse.Namespace) -> Store:
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    return Store(path)


def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _json_rational(obj: Any) -> Any:
    """Recursively convert Fractions to canonical JSON strings.

    Checked by exact type so builtin ``int`` ids/versions stay numbers.
    """
    if isinstance(obj, dict):
        return {k: _json_rational(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_rational(v) for v in obj]
    if isinstance(obj, Fraction):
        return fraction_to_json(obj)
    return obj


# -- curve / method commands ---------------------------------------------
def _cmd_curve_create(args: argparse.Namespace, svc: CurveService) -> None:
    curve = svc.create_curve(args.name, args.volumes, args.potentials)
    curve.pop("_volumes", None)
    curve.pop("_potentials", None)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(curve, indent=2))
    _dump(curve)


def _cmd_curve_update(args: argparse.Namespace, svc: CurveService) -> None:
    curve = svc.update_curve(
        args.curve_id,
        args.volumes,
        args.potentials,
        expected_version=args.expected_version,
    )
    _dump({k: v for k, v in curve.items() if not k.startswith("_")})


def _cmd_curve_show(args: argparse.Namespace, svc: CurveService) -> None:
    curve = svc.get_curve(args.curve_id)
    _dump({k: v for k, v in curve.items() if not k.startswith("_")})


def _cmd_curve_list(args: argparse.Namespace, svc: CurveService) -> None:
    _dump(
        [
            {k: v for k, v in c.items() if not k.startswith("_")}
            for c in svc.list_curves()
        ]
    )


def _cmd_method_create(args: argparse.Namespace, svc: CurveService) -> None:
    method = svc.create_method(
        args.name,
        args.low_volume,
        args.high_volume,
        args.threshold,
        args.k,
        args.min_gap,
        args.equivalence_index,
    )
    _dump(_json_rational(method))


def _cmd_method_update(args: argparse.Namespace, svc: CurveService) -> None:
    method = svc.update_method(
        args.method_id,
        low_volume=args.low_volume,
        high_volume=args.high_volume,
        threshold=args.threshold,
        k=args.k,
        min_gap=args.min_gap,
        equivalence_index=args.equivalence_index,
        expected_version=args.expected_version,
    )
    _dump(_json_rational(method))


def _cmd_method_show(args: argparse.Namespace, svc: CurveService) -> None:
    _dump(_json_rational(svc.get_method(args.method_id)))


def _cmd_publish(args: argparse.Namespace, svc: CurveService) -> None:
    product = svc.publish_endpoints(
        args.curve_id,
        args.method_id,
        expected_curve_version=args.expected_curve_version,
        expected_method_version=args.expected_method_version,
    )
    product.pop("explanation", None)
    _dump(_json_rational(product))


def _cmd_explain(args: argparse.Namespace, svc: CurveService) -> None:
    product = svc.publish_endpoints(args.curve_id, args.method_id)
    _dump(_json_rational(product["explanation"]))


# -- sample / batch commands ---------------------------------------------
def _cmd_sample_create(args: argparse.Namespace, svc: PotencyService) -> None:
    sample = svc.create_sample(
        args.name,
        args.net_mass,
        args.sample_fraction,
        args.concentration,
        args.factor,
        blank_curve_id=args.blank_curve,
        sample_curve_id=args.sample_curve,
        blank_product_id=args.blank_product,
        sample_product_id=args.sample_product,
    )
    _dump(_json_rational(sample))


def _cmd_sample_eval(args: argparse.Namespace, svc: PotencyService) -> None:
    _dump(
        _json_rational(
            svc.compute_sample(
                args.sample_id, args.method_id, refresh=not args.no_refresh
            )
        )
    )


def _cmd_batch_create(args: argparse.Namespace, svc: PotencyService) -> None:
    batch = svc.create_batch(args.name, args.method_id, args.samples)
    _dump(_json_rational(batch))


def _cmd_batch_eval(args: argparse.Namespace, svc: PotencyService) -> None:
    _dump(
        _json_rational(svc.compute_batch(args.batch_id, refresh=not args.no_refresh))
    )


def _cmd_report_show(args: argparse.Namespace, svc: PotencyService) -> None:
    _dump(_json_rational(svc.get_report(args.report_id)))


def _cmd_import_product(args: argparse.Namespace, svc: PotencyService) -> None:
    payload = json.loads(Path(args.file).read_text())
    _dump(_json_rational(svc.import_external_product(payload)))


# -- demo / validation ----------------------------------------------------
def _cmd_demo(args: argparse.Namespace, svc: Any) -> None:
    from .demo import run_demo

    _dump(run_demo(args.db))


def _cmd_validate(args: argparse.Namespace, svc: Any) -> None:
    from .validation import run_validation

    result = run_validation(args.db, args.out)
    _dump(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="titration")
    parser.add_argument("--db", default="var/titration.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("curve-create")
    p.add_argument("--name", required=True)
    p.add_argument("--volumes", nargs="+", required=True)
    p.add_argument("--potentials", nargs="+", required=True)
    p.add_argument("--json-out")
    p.set_defaults(func=_cmd_curve_create)

    p = sub.add_parser("curve-update")
    p.add_argument("--curve-id", type=int, required=True)
    p.add_argument("--expected-version", type=int, required=True)
    p.add_argument("--volumes", nargs="+", required=True)
    p.add_argument("--potentials", nargs="+", required=True)
    p.set_defaults(func=_cmd_curve_update)

    p = sub.add_parser("curve-show")
    p.add_argument("--curve-id", type=int, required=True)
    p.set_defaults(func=_cmd_curve_show)

    p = sub.add_parser("curve-list")
    p.set_defaults(func=_cmd_curve_list)

    p = sub.add_parser("method-create")
    p.add_argument("--name", required=True)
    p.add_argument("--low-volume", required=True)
    p.add_argument("--high-volume", required=True)
    p.add_argument("--threshold", required=True)
    p.add_argument("--k", type=int, required=True)
    p.add_argument("--min-gap", required=True)
    p.add_argument("--equivalence-index", type=int)
    p.set_defaults(func=_cmd_method_create)

    p = sub.add_parser("method-update")
    p.add_argument("--method-id", type=int, required=True)
    p.add_argument("--low-volume")
    p.add_argument("--high-volume")
    p.add_argument("--threshold")
    p.add_argument("--k", type=int)
    p.add_argument("--min-gap")
    p.add_argument("--equivalence-index", type=int)
    p.add_argument("--expected-version", type=int)
    p.set_defaults(func=_cmd_method_update)

    p = sub.add_parser("method-show")
    p.add_argument("--method-id", type=int, required=True)
    p.set_defaults(func=_cmd_method_show)

    p = sub.add_parser("publish")
    p.add_argument("--curve-id", type=int, required=True)
    p.add_argument("--method-id", type=int, required=True)
    p.add_argument("--expected-curve-version", type=int)
    p.add_argument("--expected-method-version", type=int)
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("explain")
    p.add_argument("--curve-id", type=int, required=True)
    p.add_argument("--method-id", type=int, required=True)
    p.set_defaults(func=_cmd_explain)

    p = sub.add_parser("sample-create")
    p.add_argument("--name", required=True)
    p.add_argument("--net-mass", required=True)
    p.add_argument("--sample-fraction", required=True)
    p.add_argument("--concentration", required=True)
    p.add_argument("--factor", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--blank-curve", type=int)
    group.add_argument("--blank-product", type=int)
    p.add_argument("--sample-curve", type=int)
    p.add_argument("--sample-product", type=int)
    p.set_defaults(func=_cmd_sample_create)

    p = sub.add_parser("sample-show")
    p.add_argument("--sample-id", type=int, required=True)
    p.set_defaults(func=lambda a, s: _dump(_json_rational(s.get_sample(a.sample_id))))

    p = sub.add_parser("sample-eval")
    p.add_argument("--sample-id", type=int, required=True)
    p.add_argument("--method-id", type=int, required=True)
    p.add_argument("--no-refresh", action="store_true")
    p.set_defaults(func=_cmd_sample_eval)

    p = sub.add_parser("batch-create")
    p.add_argument("--name", required=True)
    p.add_argument("--method-id", type=int, required=True)
    p.add_argument("--samples", nargs="+", type=int, required=True)
    p.set_defaults(func=_cmd_batch_create)

    p = sub.add_parser("batch-eval")
    p.add_argument("--batch-id", type=int, required=True)
    p.add_argument("--no-refresh", action="store_true")
    p.set_defaults(func=_cmd_batch_eval)

    p = sub.add_parser("report-show")
    p.add_argument("--report-id", type=int, required=True)
    p.set_defaults(func=_cmd_report_show)

    p = sub.add_parser("import-product")
    p.add_argument("--file", required=True)
    p.set_defaults(func=_cmd_import_product)

    p = sub.add_parser("demo")
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("validate")
    p.add_argument("--out", default="results/validation.json")
    p.set_defaults(func=_cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = _store(args)
    try:
        if args.command in {
            "curve-create",
            "curve-update",
            "curve-show",
            "curve-list",
            "method-create",
            "method-update",
            "method-show",
            "publish",
            "explain",
        }:
            svc = CurveService(store)
        elif args.command in {"demo", "validate"}:
            svc = None
        else:
            svc = PotencyService(store)
        args.func(args, svc)
    except (ValidationError, NotFoundError, ConflictError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
