"""Battery types."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Spec:
    """Defines the battery's usable characteristics and limits."""

    capacity_kwh: Decimal
    """Maximum usable stored energy."""

    minimum_energy_kwh: Decimal
    """Lowest stored energy permitted during normal operation."""

    maximum_charge_power_kw: Decimal
    """Maximum power accepted by the battery."""

    maximum_discharge_power_kw: Decimal
    """Maximum power supplied by the battery."""

    charge_efficiency: Decimal
    """Fraction of input energy retained by the battery."""

    discharge_efficiency: Decimal
    """Fraction of stored energy delivered by the battery."""


@dataclass(frozen=True, slots=True)
class State:
    """Represents the battery state at an instant."""

    energy_kwh: Decimal
    """Usable energy currently stored."""
