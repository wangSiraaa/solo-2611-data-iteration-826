"""Out-of-control event reconstruction from archived control measurements.

The archive is read *by work station and planned measurement sequence*.  Each
received measurement references a **frozen potency** and carries the control
level, center and positive scale that were in force at that plan number.  The
exact residual is

    residual = (potency - center) / scale

Two alarm rules apply to a received item:

* ``single``     -- ``abs(residual) >= 2``
* ``paired``     -- the immediately *continued* predecessor on the same
                    station/level is present, adjacent in plan number, has the
                    same sign, and both absolute residuals are ``>= 1``.

Missing measurements, a change of control level and plan-number gaps all break
the continuation chain (both for the paired rule and for event continuity);
a measurement that arrives late can fill a gap later.  A non-alarming received
item never starts or extends an event but also does not erase history: two
alarming runs stay separate events.

Maximal runs of consecutive (in the continued sense) alarming planned items on
the same station and level form one event.  Everything here is pure and uses
``fractions.Fraction``; persistence, versioning and the insurance recompute
live elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .exact import ValidationError, fraction_to_json, parse_fraction

SINGLE_THRESHOLD = 2
PAIRED_THRESHOLD = 1

# Rule identifiers used in stored products and on the CLI.
RULE_SINGLE = "single"
RULE_PAIRED = "paired"


@dataclass(frozen=True)
class ControlMeasurement:
    """One archived planned control item.

    ``registered_at`` orders accidents (last alarming item of each event);
    ``received`` is false while the item is still outstanding, in which case
    its numeric fields may be ``None``.  A late arrival flips ``received`` and
    fills the gap it used to create.
    """

    station: int
    plan_no: int
    level: int
    center: Fraction | None
    scale: Fraction | None
    potency: Fraction | None
    registered_at: str
    received: bool = True
    source_ref: str | None = None
    arrived_at: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ControlMeasurement":
        try:
            station = int(payload["station"])
            plan_no = int(payload["plan_no"])
            level = int(payload["level"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(
                f"measurement needs integer station/plan_no/level: {exc}"
            ) from exc
        registered_at = payload.get("registered_at")
        if not registered_at or not str(registered_at).strip():
            raise ValidationError(
                f"measurement station={station} plan={plan_no} needs"
                " registered_at"
            )
        arrived_at = payload.get("arrived_at")
        received = bool(payload.get("received", True))
        if not received:
            return cls(
                station=station,
                plan_no=plan_no,
                level=level,
                center=None,
                scale=None,
                potency=None,
                registered_at=str(registered_at),
                received=False,
                source_ref=payload.get("source_ref"),
                arrived_at=str(arrived_at) if arrived_at else None,
            )
        center = parse_fraction(payload["center"], field="center")
        scale = parse_fraction(payload["scale"], field="scale")
        potency = parse_fraction(payload["potency"], field="potency")
        if scale <= 0:
            raise ValidationError(
                f"station={station} plan={plan_no}: scale must be positive"
            )
        return cls(
            station=station,
            plan_no=plan_no,
            level=level,
            center=center,
            scale=scale,
            potency=potency,
            registered_at=str(registered_at),
            received=True,
            source_ref=payload.get("source_ref"),
            arrived_at=str(arrived_at) if arrived_at else None,
        )

    def residual(self) -> Fraction:
        if not self.received or self.center is None or self.scale is None:
            raise ValidationError(
                f"station={self.station} plan={self.plan_no} has no values"
            )
        return (self.potency - self.center) / self.scale

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "plan_no": self.plan_no,
            "level": self.level,
            "center": None if self.center is None else fraction_to_json(self.center),
            "scale": None if self.scale is None else fraction_to_json(self.scale),
            "potency": None
            if self.potency is None
            else fraction_to_json(self.potency),
            "registered_at": self.registered_at,
            "arrived_at": self.arrived_at,
            "received": self.received,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class AlarmSource:
    """Why one planned item alarmed, with the records supporting it.

    For the paired rule the predecessor is included even though it need not be
    an alarming/loss item itself.
    """

    rules: tuple[str, ...]
    residual: Fraction
    predecessor_plan_no: int | None
    predecessor_residual: Fraction | None
    record: dict[str, Any]
    predecessor_record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": list(self.rules),
            "residual": fraction_to_json(self.residual),
            "predecessor_plan_no": self.predecessor_plan_no,
            "predecessor_residual": None
            if self.predecessor_residual is None
            else fraction_to_json(self.predecessor_residual),
            "record": self.record,
            "predecessor_record": self.predecessor_record,
        }


@dataclass(frozen=True)
class ControlEvent:
    """One reconstructed out-of-control event (immutable product content)."""

    station: int
    level: int
    first_plan_no: int
    last_plan_no: int
    last_registered_at: str
    plan_numbers: tuple[int, ...]
    alarms: tuple[AlarmSource, ...]

    @property
    def event_key(self) -> str:
        return f"S{self.station}-L{self.level}-P{self.first_plan_no}-{self.last_plan_no}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "station": self.station,
            "level": self.level,
            "first_plan_no": self.first_plan_no,
            "last_plan_no": self.last_plan_no,
            "last_registered_at": self.last_registered_at,
            "plan_numbers": list(self.plan_numbers),
            "alarms": [a.to_dict() for a in self.alarms],
        }


def _same_sign(a: Fraction, b: Fraction) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def _close_run(
    events: list[ControlEvent],
    station: int,
    run_items: list[tuple[ControlMeasurement, Fraction, AlarmSource]],
) -> None:
    """Turn a maximal alarming run into one event (module-level, no closure)."""
    if not run_items:
        return
    first = run_items[0][0]
    last = run_items[-1][0]
    events.append(
        ControlEvent(
            station=station,
            level=first.level,
            first_plan_no=first.plan_no,
            last_plan_no=last.plan_no,
            last_registered_at=last.registered_at,
            plan_numbers=tuple(m.plan_no for m, _, _ in run_items),
            alarms=tuple(src for _, _, src in run_items),
        )
    )


def detect_events(
    measurements: Sequence[ControlMeasurement | Mapping[str, Any]],
) -> list[ControlEvent]:
    """Reconstruct all out-of-control events from an archive slice.

    Inputs may be :class:`ControlMeasurement` objects or plain JSON-style
    dicts.  Processing is per station, in ascending planned order; the paired
    predecessor must be received, adjacent in plan number and on the same
    control level.
    """
    items = [
        m if isinstance(m, ControlMeasurement) else ControlMeasurement.from_dict(m)
        for m in measurements
    ]
    by_station: dict[int, list[ControlMeasurement]] = {}
    seen: set[tuple[int, int]] = set()
    for m in items:
        key = (m.station, m.plan_no)
        if key in seen:
            raise ValidationError(
                f"duplicate planned item station={m.station} plan={m.plan_no}"
            )
        seen.add(key)
        by_station.setdefault(m.station, []).append(m)

    events: list[ControlEvent] = []
    for station in sorted(by_station):
        ordered = sorted(by_station[station], key=lambda m: m.plan_no)
        # Continued predecessor: adjacent plan number, same level, received.
        prev: ControlMeasurement | None = None
        prev_residual: Fraction | None = None
        run: list[tuple[ControlMeasurement, Fraction, AlarmSource]] = []

        for m in ordered:
            if not m.received:
                # A gap breaks the continuation chain and closes any run.
                _close_run(events, station, run)
                run = []
                prev, prev_residual = None, None
                continue

            residual = m.residual()
            rules: list[str] = []
            paired_pred: ControlMeasurement | None = None
            paired_residual: Fraction | None = None
            if abs(residual) >= SINGLE_THRESHOLD:
                rules.append(RULE_SINGLE)
            if (
                prev is not None
                and prev_residual is not None
                and prev.level == m.level
                and prev.plan_no + 1 == m.plan_no
                and _same_sign(prev_residual, residual)
                and abs(prev_residual) >= PAIRED_THRESHOLD
                and abs(residual) >= PAIRED_THRESHOLD
            ):
                rules.append(RULE_PAIRED)
                paired_pred, paired_residual = prev, prev_residual

            if rules:
                # A level change or plan gap starts a new event.
                if run and (
                    run[0][0].level != m.level
                    or run[-1][0].plan_no + 1 != m.plan_no
                ):
                    _close_run(events, station, run)
                    run = []
                source = AlarmSource(
                    rules=tuple(rules),
                    residual=residual,
                    predecessor_plan_no=None
                    if paired_pred is None
                    else paired_pred.plan_no,
                    predecessor_residual=paired_residual,
                    record=m.to_dict(),
                    predecessor_record=None
                    if paired_pred is None
                    else paired_pred.to_dict(),
                )
                run.append((m, residual, source))
            else:
                # A non-alarming item separates two runs but remains a valid
                # predecessor candidate for the next item's paired rule.
                _close_run(events, station, run)
                run = []

            # The received item is the next item's predecessor only when the
            # adjacency/level chain is unbroken.
            prev, prev_residual = m, residual

        _close_run(events, station, run)
    return events


def event_sort_key(event: ControlEvent) -> tuple[str, int, int]:
    """Accident order: last alarm registration, then station, then first item."""
    return (event.last_registered_at, event.station, event.first_plan_no)


def sort_events(events: Sequence[ControlEvent]) -> list[ControlEvent]:
    return sorted(events, key=event_sort_key)


def events_from_product(payload: Mapping[str, Any]) -> list[ControlEvent]:
    """Rebuild typed events from an offline-readable event product JSON.

    Only the documented public JSON shape is needed; no database is involved.
    """
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise ValidationError("event product needs an 'events' list")
    rebuilt: list[ControlEvent] = []
    for ev in raw_events:
        alarms = []
        for alarm in ev["alarms"]:
            record = alarm["record"]
            pred_record = alarm.get("predecessor_record")
            alarms.append(
                AlarmSource(
                    rules=tuple(alarm["rules"]),
                    residual=parse_fraction(alarm["residual"], field="residual"),
                    predecessor_plan_no=alarm.get("predecessor_plan_no"),
                    predecessor_residual=None
                    if alarm.get("predecessor_residual") is None
                    else parse_fraction(
                        alarm["predecessor_residual"],
                        field="predecessor_residual",
                    ),
                    record=record,
                    predecessor_record=pred_record,
                )
            )
        rebuilt.append(
            ControlEvent(
                station=int(ev["station"]),
                level=int(ev["level"]),
                first_plan_no=int(ev["first_plan_no"]),
                last_plan_no=int(ev["last_plan_no"]),
                last_registered_at=str(ev["last_registered_at"]),
                plan_numbers=tuple(int(x) for x in ev["plan_numbers"]),
                alarms=tuple(alarms),
            )
        )
    return rebuilt
