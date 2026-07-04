"""Battery planning types."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from . import battery, energy, tariff
from .interval import TimeInterval


@dataclass(frozen=True, slots=True)
class Input:
    """Provides source data required to construct and simulate a plan."""

    now: datetime
    """Timezone-aware time from which planning begins."""

    battery_spec: battery.Spec
    """Characteristics and operating limits of the battery."""

    battery_state: battery.State
    """Observed battery state at the planning time."""

    tariff: tuple[tariff.TariffInterval, ...]
    """Combined future import and export tariff schedule."""

    load_forecast: tuple[energy.EnergyInterval, ...]
    """Forecast household energy consumption."""

    solar_forecast: tuple[energy.EnergyInterval, ...]
    """Forecast solar energy generation."""


@dataclass(frozen=True, slots=True)
class InputInterval:
    """Provides aligned planner inputs for one time interval."""

    interval: TimeInterval
    """Time interval represented by these inputs."""

    load_kwh: Decimal
    """Forecast household consumption during the interval."""

    solar_kwh: Decimal
    """Forecast solar generation during the interval."""

    tariff: tariff.Tariff
    """Import and export prices during the interval."""


@dataclass(frozen=True, slots=True)
class State:
    """Represents simulation state after processing an input interval."""

    time: datetime
    """Timezone-aware time represented by this state."""

    battery: battery.State
    """Projected battery state at this time."""

    charge_power_kw: Decimal
    """Signed battery power during the preceding interval.

    Positive values represent charging; negative values represent discharging.
    """

    grid_import_kwh: Decimal
    """Energy imported during the preceding interval."""

    grid_export_kwh: Decimal
    """Energy exported during the preceding interval."""
