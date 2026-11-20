"""Control-sample archive, versioned event products and insurance recompute.

The service is the persistence/versioning shell around the pure rules in
:mod:`titration.control_events` and the optimizer in :mod:`titration.reinsurance`.

Event products are immutable, append-only snapshots like the endpoint
products: rebuilding after a late arrival or a potency correction publishes a
**new version** under the same scope while every prior version stays
queryable.  Each snapshot is fully self-contained JSON (frozen inputs,
residuals, trigger reasons, source records), so an equivalent product can be
exported, read offline and re-imported without the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .control_events import (
    ControlEvent,
    ControlMeasurement,
    detect_events,
    events_from_product,
    sort_events,
)
from .exact import ValidationError, parse_fraction
from .reinsurance import (
    Accident,
    OptimalPlan,
    Policy,
    build_accidents,
    optimize,
)
from .store import Store

PRODUCT_MODEL = "control_ooc_events_v1"


class ControlService:
    def __init__(self, store: Store):
        self.store = store

    # -- archive ----------------------------------------------------------
    def record_measurement(
        self,
        *,
        station: int,
        plan_no: int,
        level: int,
        center: object = None,
        scale: object = None,
        potency: object = None,
        registered_at: str,
        arrived_at: str | None = None,
        received: bool = True,
        source_ref: str | None = None,
    ) -> dict:
        """Archive one planned control item or fill a gap with a late result.

        Received items must carry center, a positive scale and the frozen
        potency.  Outstanding items are stored with ``received=False`` and no
        numeric values; they break continuation until a later call fills them.
        """
        station, plan_no, level = self._validate_ids(station, plan_no, level)
        if not registered_at or not str(registered_at).strip():
            raise ValidationError("registered_at is required")
        arrived = arrived_at if arrived_at is not None else registered_at
        if not received:
            center_f = scale_f = potency_f = None
        else:
            center_f = parse_fraction(center, field="center")
            scale_f = parse_fraction(scale, field="scale")
            potency_f = parse_fraction(potency, field="potency")
            if scale_f <= 0:
                raise ValidationError("scale must be positive")
        return self.store.upsert_control_measurement(
            station=station,
            plan_no=plan_no,
            level=level,
            center=center_f,
            scale=scale_f,
            potency=potency_f,
            registered_at=str(registered_at),
            arrived_at=str(arrived),
            received=received,
            source_ref=source_ref,
        )

    @staticmethod
    def _validate_ids(station: int, plan_no: int, level: int) -> tuple[int, int, int]:
        ids = (("station", station), ("plan_no", plan_no), ("level", level))
        out = []
        for name, value in ids:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{name} must be an integer")
            out.append(value)
        if out[0] < 0 or out[1] <= 0 or out[2] < 0:
            raise ValidationError(
                "station/level must be >= 0 and plan_no must be positive"
            )
        return out[0], out[1], out[2]

    def set_rework_loss(self, station: int, plan_no: int, loss: int) -> None:
        self._validate_ids(station, plan_no, 0)
        self.store.set_rework_loss(station, plan_no, loss)

    def _load_measurements(self, station: int | None) -> list[ControlMeasurement]:
        rows = self.store.list_control_measurements(station=station)
        return [
            ControlMeasurement(
                station=r["station"],
                plan_no=r["plan_no"],
                level=r["level"],
                center=r["center"],
                scale=r["scale"],
                potency=r["potency"],
                registered_at=r["registered_at"],
                received=r["received"],
                source_ref=r["source_ref"],
                arrived_at=r["arrived_at"],
            )
            for r in rows
        ]

    # -- versioned event products ----------------------------------------
    def publish_event_product(
        self,
        scope: str,
        *,
        station: int | None = None,
        note: str | None = None,
    ) -> dict:
        """Reconstruct events from the current archive and publish a version.

        Versions are per scope and increment on every publication; older
        versions are retained.  The snapshot embeds every input measurement,
        so the product is reproducible offline.
        """
        if not scope or not str(scope).strip():
            raise ValidationError("product scope must be non-empty")
        measurements = self._load_measurements(station)
        events = sort_events(detect_events(measurements))
        version = self._next_version(scope)
        snapshot = self._build_snapshot(
            scope=scope,
            version=version,
            measurements=measurements,
            events=events,
            note=note,
        )
        product_ref = f"{scope}:v{version}"
        product = self.store.create_control_event_product(
            product_ref=product_ref,
            version=version,
            scope=scope,
            source="internal",
            snapshot=snapshot,
        )
        return product

    def import_event_product(self, payload: Mapping[str, Any]) -> dict:
        """Consume an external, offline-readable event product JSON document.

        The document must carry ``product_ref``, ``version``, ``scope`` and an
        ``events`` list matching :func:`detect_events` output; it is validated
        by rebuilding every typed event and stored with ``source='external'``.
        """
        if not isinstance(payload, Mapping):
            raise ValidationError("event product must be a JSON object")
        for key in ("product_ref", "version", "scope", "events", "model"):
            if key not in payload:
                raise ValidationError(f"event product missing key: {key}")
        if payload["model"] != PRODUCT_MODEL:
            raise ValidationError(
                f"unsupported event product model {payload['model']!r}"
            )
        # Rebuild first: a malformed document must not be stored.
        events_from_product(payload)
        return self.store.create_control_event_product(
            product_ref=str(payload["product_ref"]),
            version=int(payload["version"]),
            scope=str(payload["scope"]),
            source="external",
            snapshot=dict(payload),
        )

    def _next_version(self, scope: str) -> int:
        existing = self.store.list_control_event_products(scope=scope)
        return 1 + max((p["version"] for p in existing), default=0)

    def _build_snapshot(
        self,
        *,
        scope: str,
        version: int,
        measurements: Sequence[ControlMeasurement],
        events: Sequence[ControlEvent],
        note: str | None,
    ) -> dict[str, Any]:
        return {
            "model": PRODUCT_MODEL,
            "product_ref": f"{scope}:v{version}",
            "scope": scope,
            "version": version,
            "note": note,
            "rules": {
                "single": "abs(residual) >= 2",
                "paired": "continued predecessor, same sign, both abs >= 1",
                "residual": "(potency - center) / scale",
                "breaks": ["missing", "level_change", "plan_gap"],
            },
            "measurements": [m.to_dict() for m in measurements],
            "events": [e.to_dict() for e in events],
        }

    def get_event_product(self, ref: str) -> dict:
        return self.store.get_control_event_product(ref)

    def list_event_products(self, *, scope: str | None = None) -> list[dict]:
        return self.store.list_control_event_products(scope=scope)

    def export_event_product(self, ref: str, path: str | Path) -> Path:
        """Write the self-contained snapshot JSON for offline transfer."""
        product = self.store.get_control_event_product(ref)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        import json

        out.write_text(
            json.dumps(product["snapshot"], ensure_ascii=False, indent=2)
        )
        return out

    # -- insurance recompute on a fixed event product ---------------------
    def accidents_from_product(
        self,
        product_ref: str,
        *,
        losses: Mapping[tuple[int, int], int] | None = None,
    ) -> list[Accident]:
        """Order the product's accidents and attach the current loss table.

        Losses come from the archived re-work table unless an explicit mapping
        is supplied.  Only alarming plan items contribute, and only non-negative
        integer losses are accepted.
        """
        product = self.store.get_control_event_product(product_ref)
        events = events_from_product(product["snapshot"])
        loss_map = self.store.get_rework_losses() if losses is None else dict(losses)
        return build_accidents(events, loss_map)

    def recompute_policy(
        self,
        product_ref: str,
        policy: Policy,
        *,
        losses: Mapping[tuple[int, int], int] | None = None,
    ) -> OptimalPlan:
        """Recompute one policy against a fixed, versioned event product.

        Different policies can be replayed repeatedly on the same product
        without touching the archive; the product itself never changes.
        """
        accidents = self.accidents_from_product(product_ref, losses=losses)
        return optimize(accidents, policy)

    def policy_from_product_snapshot(
        self,
        snapshot: Mapping[str, Any],
        policy: Policy,
        losses: Mapping[tuple[int, int], int] | None = None,
    ) -> OptimalPlan:
        """Pure offline recompute from an exported snapshot (no database)."""
        events = events_from_product(snapshot)
        accidents = build_accidents(
            events, {} if losses is None else dict(losses)
        )
        return optimize(accidents, policy)
