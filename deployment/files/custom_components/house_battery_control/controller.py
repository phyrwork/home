"""Battery controller types."""

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

from . import battery, planner, tariff


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


def select_command(
    *,
    spec: battery.Spec,
    state: battery.State,
    tariff: tariff.Tariff,
    reserve: planner.ReserveInterval,
    export_hysteresis_kwh: Decimal,
    previous_command: Command | None,
) -> Command:
    """Select the desired inverter command from current controller inputs."""
    if not (
        spec.minimum_energy_kwh
        <= reserve.start_energy_kwh
        <= spec.capacity_kwh
        and spec.minimum_energy_kwh
        <= reserve.end_energy_kwh
        <= spec.capacity_kwh
    ):
        raise ValueError("Reserve energy must be within battery limits")
    if export_hysteresis_kwh < 0:
        raise ValueError("Export hysteresis must not be negative")

    if tariff.import_price_is_off_peak:
        return GridCharge(target_energy_kwh=spec.capacity_kwh)

    if (
        isinstance(previous_command, ForceExport)
        and state.energy_kwh > reserve.start_energy_kwh
    ):
        return ForceExport(target_energy_kwh=reserve.start_energy_kwh)

    if state.energy_kwh > reserve.start_energy_kwh + export_hysteresis_kwh:
        return ForceExport(target_energy_kwh=reserve.start_energy_kwh)

    return SelfConsumption(minimum_energy_kwh=reserve.end_energy_kwh)
