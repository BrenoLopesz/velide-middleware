"""Canonical keys for Farmax's integral DOUBLE PRECISION references."""

from decimal import Decimal, InvalidOperation


def normalize_farmax_id(value) -> str:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid Farmax reference: {!r}".format(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("Farmax references must be finite integers")
    return str(int(number))
