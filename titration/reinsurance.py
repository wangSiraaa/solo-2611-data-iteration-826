"""Re-work insurance recompute over a fixed sequence of accidents.

An accident aggregates only the non-negative re-work losses of its alarming
planned items.  Accidents are settled in a fixed order (registration time of
the last alarming item, then station, then first plan number).

Contract terms
--------------
``deductible D``      per-accident self-retention
``limit L``           initial payable limit, also the level a restoration buys back
``restorations R``    maximum number of restoration rights over the policy period
``restoration_fee F`` fixed fee paid each time a right is used

At accident *i* with remaining balance ``B`` and loss ``x``::

    payable = max(0, min(max(0, x - D), B))
    B      -= payable

After settlement, while ``B < L`` one right may be consumed and fee ``F`` paid
to reset ``B = L``; the restored balance can only serve later accidents.  The
objective is the maximum attainable net indemnity::

    net = total payables - total restoration fees

All money is integral and may be very large; Python integers are exact.  The
optimizer is a memoized dynamic program over ``(accident index, rights left,
balance)``.  At most 80 accidents and 6 restorations keep the reachable state
space small even though balances are unbounded integers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .exact import ValidationError

MAX_ACCIDENTS = 80
MAX_RESTORATIONS = 6


@dataclass(frozen=True)
class Policy:
    deductible: int
    limit: int
    restorations: int
    restoration_fee: int

    @classmethod
    def from_values(
        cls,
        deductible: int,
        limit: int,
        restorations: int,
        restoration_fee: int,
    ) -> "Policy":
        for name, value in (
            ("deductible", deductible),
            ("limit", limit),
            ("restorations", restorations),
            ("restoration_fee", restoration_fee),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{name} must be an integer")
            if value < 0:
                raise ValidationError(f"{name} must be non-negative")
        if limit <= 0:
            raise ValidationError("limit must be positive")
        if restorations > MAX_RESTORATIONS:
            raise ValidationError(
                f"at most {MAX_RESTORATIONS} restoration rights are supported"
            )
        return cls(deductible, limit, restorations, restoration_fee)


@dataclass(frozen=True)
class Accident:
    """One settled accident: identity, ordered loss and source event key."""

    event_key: str
    station: int
    level: int
    first_plan_no: int
    last_plan_no: int
    last_registered_at: str
    loss: int


@dataclass(frozen=True)
class AccidentDecision:
    """Full per-accident ledger row for one optimal plan."""

    accident_index: int
    event_key: str
    station: int
    first_plan_no: int
    last_plan_no: int
    last_registered_at: str
    loss: int
    payable: int
    balance_before: int
    balance_after_claim: int
    restoration_used: bool
    restoration_fee: int
    balance_after: int
    rights_remaining: int


@dataclass(frozen=True)
class OptimalPlan:
    net_indemnity: int
    total_payable: int
    total_fees: int
    rights_used: int
    rights_remaining: int
    decisions: tuple[AccidentDecision, ...]

    def to_dict(self) -> dict:
        return {
            "net_indemnity": self.net_indemnity,
            "total_payable": self.total_payable,
            "total_fees": self.total_fees,
            "rights_used": self.rights_used,
            "rights_remaining": self.rights_remaining,
            "decisions": [
                {
                    "accident_index": d.accident_index,
                    "event_key": d.event_key,
                    "station": d.station,
                    "first_plan_no": d.first_plan_no,
                    "last_plan_no": d.last_plan_no,
                    "last_registered_at": d.last_registered_at,
                    "loss": d.loss,
                    "payable": d.payable,
                    "balance_before": d.balance_before,
                    "balance_after_claim": d.balance_after_claim,
                    "restoration_used": d.restoration_used,
                    "restoration_fee": d.restoration_fee,
                    "balance_after": d.balance_after,
                    "rights_remaining": d.rights_remaining,
                }
                for d in self.decisions
            ],
        }


def _as_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer, got {type(value).__name__}")
    return value


def build_accidents(
    events: Sequence,
    losses: Mapping[tuple[int, int] | str, int] | Mapping[str, int],
) -> list[Accident]:
    """Attach per-plan re-work losses to events and order the accidents.

    ``losses`` maps either ``(station, plan_no)`` or an event key to an
    integer.  An accident sums the non-negative losses of its alarming plan
    items only; a missing loss entry is a zero contribution.
    """
    from .control_events import ControlEvent, event_sort_key

    if len(events) > MAX_ACCIDENTS:
        raise ValidationError(
            f"at most {MAX_ACCIDENTS} accidents per policy period, got {len(events)}"
        )
    tuple_keys = any(isinstance(k, tuple) for k in losses)
    accidents: list[Accident] = []
    for order, event in enumerate(sorted(events, key=event_sort_key)):
        if not isinstance(event, ControlEvent):
            raise ValidationError("build_accidents expects ControlEvent objects")
        total = 0
        for plan_no in event.plan_numbers:
            if tuple_keys:
                value = losses.get((event.station, plan_no), 0)
            else:
                value = losses.get(
                    f"{event.station}:{plan_no}",
                    losses.get(f"S{event.station}-P{plan_no}", 0),
                )
            value = _as_int(value, field=f"loss station={event.station} plan={plan_no}")
            if value < 0:
                raise ValidationError(
                    f"re-work loss must be non-negative: station="
                    f"{event.station} plan={plan_no} got {value}"
                )
            if value > 0:
                total += value
        accidents.append(
            Accident(
                event_key=event.event_key,
                station=event.station,
                level=event.level,
                first_plan_no=event.first_plan_no,
                last_plan_no=event.last_plan_no,
                last_registered_at=event.last_registered_at,
                loss=total,
            )
        )
    return accidents


def settle_payable(loss: int, deductible: int, balance: int) -> int:
    """``max(0, min(max(0, loss - D), balance))`` with a non-negative floor."""
    eligible = loss - deductible
    if eligible <= 0 or balance <= 0:
        return 0
    return min(eligible, balance)


def optimize(accidents: Sequence[Accident], policy: Policy) -> OptimalPlan:
    """Maximize net indemnity and return one executable optimal plan.

    Ties are resolved deterministically: keeping a right (no restoration) is
    preferred whenever both choices attain the same future net indemnity.
    """
    if len(accidents) > MAX_ACCIDENTS:
        raise ValidationError(
            f"at most {MAX_ACCIDENTS} accidents per policy period, got {len(accidents)}"
        )
    n = len(accidents)

    def best(i: int, rights: int, balance: int) -> int:
        """Best net indemnity from accident i onward given rights/balance."""
        if i == n:
            return 0
        key = (i, rights, balance)
        cached = memo.get(key)
        if cached is not None:
            return cached
        accident = accidents[i]
        payable = settle_payable(
            accident.loss, policy.deductible, balance
        )
        after_claim = balance - payable
        # Option A: do not restore here.
        keep = payable + best(i + 1, rights, after_claim)
        chosen = keep
        restore = False
        # Option B: buy the limit back (only while below L and a right exists).
        if rights > 0 and after_claim < policy.limit:
            bought = (
                payable
                - policy.restoration_fee
                + best(i + 1, rights - 1, policy.limit)
            )
            if bought > chosen:
                chosen, restore = bought, True
        memo[key] = chosen
        choice[key] = restore
        return chosen

    memo: dict[tuple[int, int, int], int] = {}
    choice: dict[tuple[int, int, int], bool] = {}
    net = best(0, policy.restorations, policy.limit)

    decisions: list[AccidentDecision] = []
    rights = policy.restorations
    balance = policy.limit
    total_payable = 0
    total_fees = 0
    for i, accident in enumerate(accidents):
        balance_before = balance
        payable = settle_payable(accident.loss, policy.deductible, balance)
        after_claim = balance - payable
        restore = choice[(i, rights, balance)]
        if restore:
            fee = policy.restoration_fee
            balance = policy.limit
            rights -= 1
            total_fees += fee
        else:
            fee = 0
            balance = after_claim
        total_payable += payable
        decisions.append(
            AccidentDecision(
                accident_index=i,
                event_key=accident.event_key,
                station=accident.station,
                first_plan_no=accident.first_plan_no,
                last_plan_no=accident.last_plan_no,
                last_registered_at=accident.last_registered_at,
                loss=accident.loss,
                payable=payable,
                balance_before=balance_before,
                balance_after_claim=after_claim,
                restoration_used=restore,
                restoration_fee=fee,
                balance_after=balance,
                rights_remaining=rights,
            )
        )
    return OptimalPlan(
        net_indemnity=net,
        total_payable=total_payable,
        total_fees=total_fees,
        rights_used=policy.restorations - rights,
        rights_remaining=rights,
        decisions=tuple(decisions),
    )


def brute_force_optimum(
    accidents: Sequence[Accident], policy: Policy
) -> int:
    """Exhaustive reference over every restoration placement (small inputs).

    Enumerates each subset of the at-most-:data:`MAX_RESTORATIONS` rights and
    every legal placement opportunity; used by the test suite to audit the DP.
    """
    best_net = -1
    n = len(accidents)

    def recurse(i: int, rights: int, balance: int, net: int) -> None:
        nonlocal best_net
        if i == n:
            if net > best_net:
                best_net = net
            return
        accident = accidents[i]
        payable = settle_payable(accident.loss, policy.deductible, balance)
        after_claim = balance - payable
        recurse(i + 1, rights, after_claim, net + payable)
        if rights > 0 and after_claim < policy.limit:
            recurse(
                i + 1,
                rights - 1,
                policy.limit,
                net + payable - policy.restoration_fee,
            )

    recurse(0, policy.restorations, policy.limit, 0)
    return best_net


def evaluate_plan(
    accidents: Sequence[Accident],
    policy: Policy,
    restore_after: Sequence[int] = (),
) -> OptimalPlan:
    """Replay one explicitly chosen plan (restoration after given indices).

    Useful for presenting an alternative executable plan or auditing.  A
    restoration is legal only while rights remain and the balance is below
    ``L``.
    """
    wanted = set(restore_after)
    if any(not (0 <= i < len(accidents)) for i in wanted):
        raise ValidationError("restore_after index out of range")
    decisions: list[AccidentDecision] = []
    rights = policy.restorations
    balance = policy.limit
    total_payable = 0
    total_fees = 0
    for i, accident in enumerate(accidents):
        balance_before = balance
        payable = settle_payable(accident.loss, policy.deductible, balance)
        after_claim = balance - payable
        restore = i in wanted
        if restore:
            if rights <= 0 or after_claim >= policy.limit:
                raise ValidationError(
                    f"restoration after accident {i} is not legal"
                    " (no rights left or balance already at limit)"
                )
            fee = policy.restoration_fee
            balance = policy.limit
            rights -= 1
            total_fees += fee
        else:
            fee = 0
            balance = after_claim
        total_payable += payable
        decisions.append(
            AccidentDecision(
                accident_index=i,
                event_key=accident.event_key,
                station=accident.station,
                first_plan_no=accident.first_plan_no,
                last_plan_no=accident.last_plan_no,
                last_registered_at=accident.last_registered_at,
                loss=accident.loss,
                payable=payable,
                balance_before=balance_before,
                balance_after_claim=after_claim,
                restoration_used=restore,
                restoration_fee=fee,
                balance_after=balance,
                rights_remaining=rights,
            )
        )
    return OptimalPlan(
        net_indemnity=total_payable - total_fees,
        total_payable=total_payable,
        total_fees=total_fees,
        rights_used=policy.restorations - rights,
        rights_remaining=rights,
        decisions=tuple(decisions),
    )
