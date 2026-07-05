"""Diagnostic sensor for House Battery Control."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import controller
from .const import DOMAIN
from .coordinator import Coordinator


async def async_setup_platform(
    hass: HomeAssistant,
    config: Mapping[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: Mapping[str, Any] | None = None,
) -> None:
    """Set up the control diagnostic sensor."""
    coordinator = hass.data[DOMAIN]
    if not isinstance(coordinator, Coordinator):
        return
    async_add_entities((ControlSensor(coordinator),))


class ControlSensor(CoordinatorEntity[Coordinator], SensorEntity):
    """Expose the current control decision and its calculation context."""

    _attr_name = "House Battery Control"
    _attr_unique_id = DOMAIN
    _attr_icon = "mdi:home-battery"

    @property
    def available(self) -> bool:
        """Return whether a successful decision is available."""
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> str | None:
        """Return the applied operating mode."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.control.operating_mode.value

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return scalar context explaining the current decision."""
        snapshot = self.coordinator.data
        if snapshot is None:
            return None

        spec = snapshot.battery_spec
        state = snapshot.battery_state
        source = snapshot.input_interval
        reserve = snapshot.decision.reserve
        control = snapshot.control
        return {
            "battery_energy_kwh": float(state.energy_kwh),
            "battery_state_of_charge_percent": float(
                state.energy_kwh * 100 / spec.capacity_kwh
            ),
            "load_kwh": float(source.load_kwh),
            "solar_kwh": float(source.solar_kwh),
            "net_load_kwh": float(source.load_kwh - source.solar_kwh),
            "import_price_per_kwh": float(
                source.tariff.import_price_per_kwh
            ),
            "export_price_per_kwh": float(
                source.tariff.export_price_per_kwh
            ),
            "import_price_is_off_peak": (
                source.tariff.import_price_is_off_peak
            ),
            "reserve_start_energy_kwh": float(reserve.start_energy_kwh),
            "reserve_end_energy_kwh": float(reserve.end_energy_kwh),
            "command_target_energy_kwh": _command_target(
                snapshot.decision.command
            ),
            "target_state_of_charge_percent": _optional_float(
                control.target_state_of_charge_percent
            ),
            "power_w": _optional_float(control.power_w),
            "expires_at": reserve.interval.end.isoformat(),
            "planning_horizon_end": snapshot.planning_horizon_end.isoformat(),
            "tariff_forecast_end": snapshot.tariff_forecast_end.isoformat(),
            "load_forecast_end": snapshot.load_forecast_end.isoformat(),
            "solar_forecast_end": snapshot.solar_forecast_end.isoformat(),
        }


def _command_target(command: controller.Command) -> float | None:
    match command:
        case controller.GridCharge(target_energy_kwh=target):
            return float(target)
        case controller.ForceExport(target_energy_kwh=target):
            return float(target)
        case controller.SelfConsumption(minimum_energy_kwh=minimum):
            return float(minimum)
        case controller.Hold():
            return None


def _optional_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
