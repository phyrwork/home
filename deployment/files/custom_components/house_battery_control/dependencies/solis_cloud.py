"""SolisCloud dependency types and mappings."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TypedDict

from .. import battery, controller
from ._common import DecimalValue, to_decimal

PERCENT = Decimal(100)
WATTS_PER_KILOWATT = Decimal(1000)


class BatteryTelemetry(TypedDict):
    """Describes battery telemetry obtained through SolisCloud."""

    state_of_charge_percent: DecimalValue
    """Current battery state of charge."""


class OperatingMode(StrEnum):
    """Identifies the Solis operating mode required by a control request."""

    GRID_CHARGE = "grid_charge"
    FORCE_EXPORT = "force_export"
    SELF_CONSUMPTION = "self_consumption"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class Control:
    """Describes a normalized SolisCloud control request."""

    operating_mode: OperatingMode
    """Operating mode to apply to the inverter."""

    target_state_of_charge_percent: Decimal | None
    """Battery state of charge at which the requested operation should stop."""

    power_w: Decimal | None
    """Charge or discharge power to configure for the requested operation."""


def to_battery_state(
    telemetry: BatteryTelemetry,
    spec: battery.Spec,
) -> battery.State:
    """Map SolisCloud battery telemetry to internal battery state."""
    state_of_charge_percent = to_decimal(telemetry["state_of_charge_percent"])
    _validate_percent(state_of_charge_percent)
    return battery.State(
        energy_kwh=spec.capacity_kwh * state_of_charge_percent / PERCENT
    )


def to_control(
    command: controller.Command,
    spec: battery.Spec,
) -> Control:
    """Map an internal controller command to a SolisCloud control request."""
    match command:
        case controller.GridCharge(target_energy_kwh=target):
            return Control(
                operating_mode=OperatingMode.GRID_CHARGE,
                target_state_of_charge_percent=_energy_percent(target, spec),
                power_w=spec.maximum_charge_power_kw * WATTS_PER_KILOWATT,
            )
        case controller.ForceExport(target_energy_kwh=target):
            return Control(
                operating_mode=OperatingMode.FORCE_EXPORT,
                target_state_of_charge_percent=_energy_percent(target, spec),
                power_w=spec.maximum_discharge_power_kw * WATTS_PER_KILOWATT,
            )
        case controller.SelfConsumption(minimum_energy_kwh=minimum):
            return Control(
                operating_mode=OperatingMode.SELF_CONSUMPTION,
                target_state_of_charge_percent=_energy_percent(minimum, spec),
                power_w=None,
            )
        case controller.Hold():
            return Control(
                operating_mode=OperatingMode.HOLD,
                target_state_of_charge_percent=None,
                power_w=None,
            )


def _energy_percent(energy_kwh: Decimal, spec: battery.Spec) -> Decimal:
    if spec.capacity_kwh <= 0:
        raise ValueError("Battery capacity must be positive")
    percent = energy_kwh * PERCENT / spec.capacity_kwh
    _validate_percent(percent)
    return percent


def _validate_percent(percent: Decimal) -> None:
    if not 0 <= percent <= PERCENT:
        raise ValueError("Battery state of charge must be between 0 and 100")
