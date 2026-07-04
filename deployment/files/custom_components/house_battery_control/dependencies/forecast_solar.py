"""Forecast.Solar dependency types and mappings."""

from decimal import Decimal
from typing import TypedDict

from .. import energy
from ..interval import TimeInterval
from ._common import DecimalValue, to_datetime, to_decimal

WATT_HOURS_PER_KILOWATT_HOUR = Decimal(1000)


class Forecast(TypedDict):
    """Describes Home Assistant's Forecast.Solar energy response."""

    wh_hours: dict[str, DecimalValue]
    """Forecast energy in watt-hours keyed by ISO 8601 interval start."""


def to_energy_intervals(forecast: Forecast) -> tuple[energy.EnergyInterval, ...]:
    """Map a Home Assistant Forecast.Solar response to energy intervals."""
    periods = sorted(
        (to_datetime(timestamp), to_decimal(watt_hours))
        for timestamp, watt_hours in forecast["wh_hours"].items()
    )
    if len(periods) < 2:
        return ()

    result: list[energy.EnergyInterval] = []
    for index, (start, watt_hours) in enumerate(periods):
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
                energy_kwh=watt_hours / WATT_HOURS_PER_KILOWATT_HOUR,
            )
        )
    return tuple(result)
