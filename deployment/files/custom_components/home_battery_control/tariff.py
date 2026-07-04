"""Energy tariff types."""

from dataclasses import dataclass
from decimal import Decimal

from .interval import TimeInterval


@dataclass(frozen=True, slots=True)
class Tariff:
    """Represents import and export energy prices."""

    import_price_per_kwh: Decimal
    """Price paid for imported energy."""

    export_price_per_kwh: Decimal
    """Price received for exported energy."""


@dataclass(frozen=True, slots=True)
class TariffInterval:
    """Represents a tariff applicable during a time interval."""

    interval: TimeInterval
    """Time interval during which the tariff applies."""

    tariff: Tariff
    """Import and export prices during the interval."""
