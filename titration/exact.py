"""Exact rational helpers and canonical serialisation.

Every comparison and potency computation in this project uses
``fractions.Fraction``.  Values enter the store as rational strings and are
never converted to binary floating point for arithmetic.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Iterable

_FRACTION_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:/\d+)?$")


class ValidationError(ValueError):
    """Input violates the finite model contract."""


class NotFoundError(LookupError):
    """Requested entity does not exist."""


class ConflictError(RuntimeError):
    """Optimistic-concurrency or immutability violation."""


def parse_fraction(value: object, *, field: str = "value") -> Fraction:
    """Parse a rational from ``int``, ``Fraction`` or a decimal/fraction string.

    Accepted spellings: ``"3"``, ``"-1.25"``, ``"7/3"``, ``Fraction`` and
    ``int``.  Binary ``float`` is rejected because silently accepting an
    imprecise literal would undermine exactness.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a rational number, not bool")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not _FRACTION_RE.match(text):
            raise ValidationError(f"{field} is not a rational literal: {value!r}")
        return Fraction(text)
    raise ValidationError(
        f"{field} must be int, Fraction or rational string, got {type(value).__name__}"
    )


def parse_rational_list(values: Iterable[object], *, field: str) -> list[Fraction]:
    parsed = [parse_fraction(v, field=f"{field}[{i}]") for i, v in enumerate(values)]
    return parsed


def format_fraction(value: Fraction) -> str:
    """Canonical text form: ``n`` when integral, else ``n/d``."""
    if not isinstance(value, Fraction):
        value = parse_fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def fraction_to_json(value: Fraction) -> str:
    """Canonical JSON text form of a rational: ``n`` or ``n/d``.

    Always a string so callers never mistake an exact rational for a binary
    float or an integer of a different unit.
    """
    if not isinstance(value, Fraction):
        value = parse_fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"
