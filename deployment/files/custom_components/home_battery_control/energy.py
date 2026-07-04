"""Energy types."""

from dataclasses import dataclass
from decimal import Decimal

from .interval import TimeInterval


@dataclass(frozen=True, slots=True)
class EnergyInterval:
    """Represents energy attributed to a time interval."""

    interval: TimeInterval
    """Time interval over which the energy applies."""

    energy_kwh: Decimal
    """Non-negative energy measured or forecast during the interval."""
