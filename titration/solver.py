"""Exact selection of K well-separated candidate points.

Given candidate strengths ``s`` (already filtered to ``s >= T`` and to the
closed volume range) and their strictly increasing volumes ``v``, choose
exactly K candidates such that

* every pair of chosen volumes differs by at least ``d`` (``|v_i - v_j| >= d``),
* the sum of strengths is maximal,
* among all maximal-sum selections the ascending volume list is
  lexicographically smallest.

Important subtlety: for fixed K the maximum total strength is independent of
the volume values, but not of *which* candidates are eligible.  Because
candidates are sorted by volume and the gap constraint is monotone in index
range only for *adjacent* selections, a selection is legal iff every pair of
chosen candidates differs in volume by >= d.  For a sorted list that is
equivalent to every adjacent chosen pair satisfying it.

The DP below works on indices in volume-sorted candidate order.  It computes,
for each position and each count, the best (maximum strength, then
lexicographically smallest volume tuple) suffix selection.  All strengths are
``Fraction`` values, so no floating point is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from typing import Sequence

from .exact import format_fraction


@dataclass(frozen=True)
class Candidate:
    index: int          # index into the curve point list (internal point)
    volume: Fraction
    strength: Fraction
    left_slope: Fraction
    right_slope: Fraction


@dataclass
class SelectionResult:
    volumes: list[Fraction] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    objective: Fraction | None = None
    feasible: bool = False
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "selected_volumes": [str(format_fraction(v)) for v in self.volumes],
            "selected_indices": list(self.indices),
            "objective": None if self.objective is None else format_fraction(self.objective),
            "feasible": self.feasible,
            "reason": self.reason,
        }


def solve_selection(
    candidates: Sequence[Candidate],
    k: int,
    d: Fraction,
) -> SelectionResult:
    """Return the unique specified optimal selection (or NO_ENDPOINT).

    ``candidates`` may be unsorted; they are sorted by volume first.  Strength
    ties are resolved by lexicographically smallest ascending volume list,
    which requires comparing full tuples, not greedily picking the smallest
    first volume.
    """
    if k < 0:
        raise ValueError("K must be a non-negative integer")
    if d < 0:
        raise ValueError("d must be non-negative")

    ordered = sorted(candidates, key=lambda c: c.volume)
    n = len(ordered)
    if k == 0:
        return SelectionResult(
            volumes=[], indices=[], objective=Fraction(0), feasible=True
        )
    if n < k:
        return SelectionResult(
            feasible=False,
            reason=f"only {n} candidate point(s) available for K={k}",
            objective=None,
        )

    # best[i][c] = (total_strength, volume_tuple, index_tuple) choosing c
    # candidates from ordered[i:], respecting the gap constraint.
    # We fill from the right.
    best: list[list[tuple[Fraction, tuple, tuple] | None]] = [
        [None] * (k + 1) for _ in range(n + 1)
    ]
    for i in range(n + 1):
        best[i][0] = (Fraction(0), (), ())

    for i in range(n - 1, -1, -1):
        for c in range(1, k + 1):
            options: list[tuple[Fraction, tuple, tuple]] = []
            # Option 1: skip candidate i.
            skip = best[i + 1][c]
            if skip is not None:
                options.append(skip)
            # Option 2: take candidate i.  Next candidate must have volume
            # at least d away; since ordered is sorted by volume, the first
            # eligible index is found by advancing j.
            if c - 1 <= (n - (i + 1)):
                j = i + 1
                while j < n and ordered[j].volume - ordered[i].volume < d:
                    j += 1
                take_rest = best[j][c - 1]
                if take_rest is not None:
                    total = ordered[i].strength + take_rest[0]
                    vols = (ordered[i].volume,) + take_rest[1]
                    idxs = (ordered[i].index,) + take_rest[2]
                    options.append((total, vols, idxs))
            if not options:
                best[i][c] = None
                continue
            # Max strength, then lexicographically smallest ascending volume
            # tuple.
            best[i][c] = min(
                options,
                key=lambda opt: (-opt[0], opt[1]),
            )

    result = best[0][k]
    if result is None:
        return SelectionResult(
            feasible=False,
            reason=(
                f"no selection of {k} candidate point(s) with pairwise "
                f"volume separation >= {format_fraction(d)} mL"
            ),
            objective=None,
        )
    total, vols, idxs = result
    return SelectionResult(
        volumes=list(vols),
        indices=list(idxs),
        objective=total,
        feasible=True,
        reason=None,
    )


def brute_force_selection(
    candidates: Sequence[Candidate],
    k: int,
    d: Fraction,
) -> SelectionResult:
    """Reference implementation enumerating all combinations.

    Intended for tests and small curves (n choose K manageable).
    """
    ordered = sorted(candidates, key=lambda c: c.volume)
    n = len(ordered)
    if k <= 0:
        raise ValueError("K must be a positive integer")
    feasible = [
        combo
        for combo in combinations(range(n), k)
        if all(
            ordered[b].volume - ordered[a].volume >= d
            for a, b in zip(combo, combo[1:])
        )
    ]
    if not feasible:
        return SelectionResult(
            feasible=False,
            reason="brute force found no legal combination",
            objective=None,
        )
    scored = []
    for combo in feasible:
        total = sum((ordered[i].strength for i in combo), Fraction(0))
        vols = tuple(ordered[i].volume for i in combo)
        idxs = tuple(ordered[i].index for i in combo)
        scored.append((total, vols, idxs))
    total, vols, idxs = min(scored, key=lambda opt: (-opt[0], opt[1]))
    return SelectionResult(
        volumes=list(vols),
        indices=list(idxs),
        objective=total,
        feasible=True,
        reason=None,
    )


def _prefix_best(
    ordered: Sequence[Candidate], k: int, d: Fraction
) -> list[list[Fraction | None]]:
    """pref[i][c] = max strength choosing c from ordered[0..i] with gap >= d."""
    n = len(ordered)
    pref: list[list[Fraction | None]] = [
        [None] * (k + 1) for _ in range(n)
    ]
    for i in range(n):
        pref[i][0] = Fraction(0)
    for i in range(n):
        for c in range(1, k + 1):
            options: list[Fraction] = []
            if i > 0 and pref[i - 1][c] is not None:
                options.append(pref[i - 1][c])
            # take i, previous must be <= v_i - d
            p = i - 1
            while p >= 0 and ordered[i].volume - ordered[p].volume < d:
                p -= 1
            base = Fraction(0) if c == 1 else (
                None if p < 0 else pref[p][c - 1]
            )
            if base is not None:
                options.append(base + ordered[i].strength)
            if options:
                pref[i][c] = max(options)
    return pref


def _suffix_best(
    ordered: Sequence[Candidate], k: int, d: Fraction
) -> list[list[Fraction | None]]:
    """suf[i][c] = max strength choosing c from ordered[i:] with gap >= d."""
    n = len(ordered)
    suf: list[list[Fraction | None]] = [
        [None] * (k + 1) for _ in range(n + 1)
    ]
    for i in range(n + 1):
        suf[i][0] = Fraction(0)
    for i in range(n - 1, -1, -1):
        for c in range(1, k + 1):
            options: list[Fraction] = []
            if suf[i + 1][c] is not None:
                options.append(suf[i + 1][c])
            j = i + 1
            while j < n and ordered[j].volume - ordered[i].volume < d:
                j += 1
            if suf[j][c - 1] is not None:
                options.append(ordered[i].strength + suf[j][c - 1])
            if options:
                suf[i][c] = max(options)
    return suf


def _last_with_max_volume(ordered: Sequence[Candidate], bound: Fraction) -> int:
    """Largest index with volume <= bound, or -1."""
    lo, hi, ans = 0, len(ordered) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ordered[mid].volume <= bound:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _first_with_min_volume(ordered: Sequence[Candidate], bound: Fraction) -> int:
    """Smallest index with volume >= bound, or len(ordered)."""
    lo, hi, ans = 0, len(ordered) - 1, len(ordered)
    while lo <= hi:
        mid = (lo + hi) // 2
        if ordered[mid].volume >= bound:
            ans = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return ans


def analyze_exclusions(
    candidates: Sequence[Candidate],
    k: int,
    d: Fraction,
    optimum: Fraction,
) -> dict[int, dict]:
    """For every candidate, the exact best total strength of a K-selection
    that includes it.  Used to explain, rigorously, why losing competitors
    were excluded.

    Returns a mapping ``candidate.index -> {"max_total_with_point": Fraction
    | None, "deficit": Fraction | None, "feasible_with_point": bool,
    "tied_with_optimum": bool}``.
    """
    ordered = sorted(candidates, key=lambda c: c.volume)
    n = len(ordered)
    if k <= 0:
        raise ValueError("K must be a positive integer")
    pref = _prefix_best(ordered, k, d)
    suf = _suffix_best(ordered, k, d)
    out: dict[int, dict] = {}
    for pos, cand in enumerate(ordered):
        # The candidate itself must be excluded from both sides.  Volume-based
        # bounds alone would re-include it when d == 0, so cap them by index.
        left_end = min(
            _last_with_max_volume(ordered, cand.volume - d), pos - 1
        )
        right_start = max(
            _first_with_min_volume(ordered, cand.volume + d), pos + 1
        )
        best: Fraction | None = None
        for left_count in range(0, k):
            right_count = k - 1 - left_count
            left_val = Fraction(0) if left_count == 0 else (
                pref[left_end][left_count] if left_end >= 0 else None
            )
            if left_val is None:
                continue
            right_val = Fraction(0) if right_count == 0 else (
                suf[right_start][right_count] if right_start <= n - 1 else None
            )
            if right_val is None:
                continue
            total = cand.strength + left_val + right_val
            if best is None or total > best:
                best = total
        out[cand.index] = {
            "max_total_with_point": best,
            "deficit": None if best is None else optimum - best,
            "feasible_with_point": best is not None,
            "tied_with_optimum": best is not None and best == optimum,
        }
    return out
