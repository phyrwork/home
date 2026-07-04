"""Octopus Energy dependency types and mappings."""

import json
from collections.abc import Sequence
from typing import TypedDict, cast

from .. import tariff
from ..interval import TimeInterval
from ._common import DecimalValue, to_datetime, to_decimal


class Rate(TypedDict):
    """Describes an Octopus Energy import rate interval."""

    start: str
    """Inclusive ISO 8601 start with a UTC offset."""

    end: str
    """Exclusive ISO 8601 end with a UTC offset."""

    value_inc_vat: DecimalValue
    """Import price per kWh including VAT."""


def to_tariff_intervals(
    rates: str | Sequence[Rate],
    export_price_per_kwh: DecimalValue,
) -> tuple[tariff.TariffInterval, ...]:
    """Map fused Octopus import rates and an export price to domain tariffs."""
    source_rates = cast(Sequence[Rate], json.loads(rates)) if isinstance(rates, str) else rates
    export_price = to_decimal(export_price_per_kwh)
    rates_with_prices = tuple(
        (source, to_decimal(source["value_inc_vat"])) for source in source_rates
    )
    if not rates_with_prices:
        return ()
    peak_import_price = max(price for _, price in rates_with_prices)
    result: list[tariff.TariffInterval] = []

    for source, import_price in rates_with_prices:
        interval = TimeInterval(
            start=to_datetime(source["start"]),
            end=to_datetime(source["end"]),
        )
        if interval.end <= interval.start:
            raise ValueError("Tariff interval end must be after its start")

        result.append(
            tariff.TariffInterval(
                interval=interval,
                tariff=tariff.Tariff(
                    import_price_per_kwh=import_price,
                    export_price_per_kwh=export_price,
                    import_price_is_off_peak=import_price < peak_import_price,
                ),
            )
        )
    return tuple(result)
