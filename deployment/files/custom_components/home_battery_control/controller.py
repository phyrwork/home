"""Battery controller types."""

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class GridCharge:
    """Requests that the battery charge from the grid."""

    target_energy_kwh: Decimal
    """Stored energy at which grid charging should stop."""


@dataclass(frozen=True, slots=True)
class ForceExport:
    """Requests that the battery discharge to the grid."""

    target_energy_kwh: Decimal
    """Stored energy at which forced export should stop."""


@dataclass(frozen=True, slots=True)
class SelfConsumption:
    """Requests that the battery serve household demand and store surplus solar."""

    minimum_energy_kwh: Decimal
    """Stored energy below which the battery should not discharge."""


@dataclass(frozen=True, slots=True)
class Hold:
    """Requests that the battery neither charge nor discharge."""


Command: TypeAlias = GridCharge | ForceExport | SelfConsumption | Hold
