"""Shared dependency mapping helpers."""

from datetime import datetime
from decimal import Decimal
from typing import TypeAlias

DecimalValue: TypeAlias = Decimal | int | float | str


def to_decimal(value: DecimalValue) -> Decimal:
    """Convert an external numeric value without preserving float error."""
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def to_datetime(value: str) -> datetime:
    """Convert an ISO 8601 value to a timezone-aware datetime."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"Expected a timezone-aware datetime: {value}")
    return result
