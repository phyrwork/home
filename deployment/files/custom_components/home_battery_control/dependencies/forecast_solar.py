"""Forecast.Solar dependency types and mappings."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .. import energy
from ..interval import TimeInterval

WATT_HOURS_PER_KILOWATT_HOUR = Decimal(1000)


class Estimate(Protocol):
    """Defines the Forecast.Solar estimate data consumed by this integration."""

    wh_period: Mapping[datetime, int]
    """Forecast energy in watt-hours keyed by interval start."""


def to_energy_intervals(estimate: Estimate) -> tuple[energy.EnergyInterval, ...]:
    """Map a Forecast.Solar estimate to domain energy intervals."""
    periods = sorted(estimate.wh_period.items())
    if len(periods) < 2:
        return ()

    result: list[energy.EnergyInterval] = []
    for index, (start, watt_hours) in enumerate(periods):
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("Forecast.Solar timestamps must be timezone-aware")
        if watt_hours < 0:
            raise ValueError("Forecast.Solar energy cannot be negative")

        if index + 1 < len(periods):
            end = periods[index + 1][0]
        else:
            end = start + (start - periods[index - 1][0])
        if end <= start:
            raise ValueError("Forecast.Solar periods must increase")

        result.append(
            energy.EnergyInterval(
                interval=TimeInterval(start=start, end=end),
                energy_kwh=Decimal(watt_hours) / WATT_HOURS_PER_KILOWATT_HOUR,
            )
        )
    return tuple(result)
