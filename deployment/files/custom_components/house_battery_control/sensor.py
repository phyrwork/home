"""Diagnostic sensor for House Battery Control."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
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
    async_add_entities(
        (
            ControlSensor(coordinator),
            HeartbeatSensor(coordinator),
            HealthSensor(coordinator),
            BatteryEnergySensor(coordinator),
            ReserveTargetSensor(coordinator),
            ReserveBalanceSensor(coordinator),
        )
    )


class _SnapshotSensor(CoordinatorEntity[Coordinator], SensorEntity):
    """Base an entity's availability on the latest controller snapshot."""

    @property
    def available(self) -> bool:
        """Observation snapshots remain available while degraded or fail-safe."""
        return super().available and self.coordinator.data is not None


class ControlSensor(_SnapshotSensor):
    """Expose the current control decision and its calculation context."""

    _attr_name = "House Battery Control"
    _attr_unique_id = DOMAIN
    _attr_icon = "mdi:home-battery"

    @property
    def native_value(self) -> str | None:
        """Return the applied operating mode."""
        if self.coordinator.data is None:
            return None
        control = self.coordinator.data.control
        return None if control is None else f"observation_only:{control.operating_mode.value}"

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return scalar context explaining the current decision."""
        snapshot = self.coordinator.data
        if snapshot is None:
            return None

        spec = snapshot.battery_spec
        state = snapshot.battery_state
        source = snapshot.input_interval
        reserve = None if snapshot.decision is None else snapshot.decision.reserve
        control = snapshot.control
        if spec is None or state is None or source is None or reserve is None or control is None:
            return {
                "observation_only": True,
                "health": snapshot.health.value,
                "fail_safe_obligation": snapshot.fail_safe_obligation,
                "fail_safe_pending": snapshot.fail_safe_pending,
                "guard_state": snapshot.guard_state,
                "guard_quality": snapshot.guard_quality,
                "source_quality": snapshot.source_quality,
                "issues": snapshot.issues,
            }
        return {
            "observation_only": True,
            "health": snapshot.health.value,
            "fail_safe_obligation": snapshot.fail_safe_obligation,
            "fail_safe_pending": snapshot.fail_safe_pending,
            "guard_state": snapshot.guard_state,
            "guard_quality": snapshot.guard_quality,
            "source_quality": snapshot.source_quality,
            "issues": snapshot.issues,
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


class _EnergySensor(_SnapshotSensor):
    """Expose a controller energy value in kilowatt-hours."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT


class BatteryEnergySensor(_EnergySensor):
    """Expose the integration's normalized stored battery energy."""

    _attr_name = "House Battery Energy"
    _attr_unique_id = f"{DOMAIN}_energy"

    @property
    def native_value(self) -> float | None:
        """Return the current stored battery energy."""
        snapshot = self.coordinator.data
        if snapshot is None or (snapshot.diagnostic_energy_kwh is None and snapshot.battery_state is None):
            return None
        energy = snapshot.diagnostic_energy_kwh
        if energy is None:
            energy = snapshot.battery_state.energy_kwh
        return float(energy)


class ReserveTargetSensor(_EnergySensor):
    """Expose the battery energy currently required by the planner."""

    _attr_name = "House Battery Reserve Target"
    _attr_unique_id = f"{DOMAIN}_reserve_target"

    @property
    def native_value(self) -> float | None:
        """Return the current reserve target."""
        snapshot = self.coordinator.data
        if snapshot is None or (snapshot.reserve is None and snapshot.decision is None):
            return None
        reserve = snapshot.reserve
        if reserve is None:
            reserve = snapshot.decision.reserve
        return float(reserve.start_energy_kwh)


class ReserveBalanceSensor(_EnergySensor):
    """Expose stored battery energy relative to the reserve target."""

    _attr_name = "House Battery Reserve Balance"
    _attr_unique_id = f"{DOMAIN}_reserve_balance"

    @property
    def native_value(self) -> float | None:
        """Return positive surplus or negative reserve shortfall energy."""
        snapshot = self.coordinator.data
        if snapshot is None or (snapshot.reserve is None and snapshot.decision is None) or (snapshot.diagnostic_energy_kwh is None and snapshot.battery_state is None):
            return None
        reserve = snapshot.reserve or snapshot.decision.reserve
        energy = snapshot.diagnostic_energy_kwh
        if energy is None:
            energy = snapshot.battery_state.energy_kwh
        return float(
            energy - reserve.start_energy_kwh
        )


class HeartbeatSensor(_SnapshotSensor):
    """Expose the last completed coordinator cycle even when degraded."""

    _attr_name = "House Battery Control Heartbeat"
    _attr_unique_id = f"{DOMAIN}_heartbeat"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        snapshot = self.coordinator.data
        return None if snapshot is None else snapshot.heartbeat_at

    @property
    def extra_state_attributes(self):
        snapshot = self.coordinator.data
        if snapshot is None:
            return None
        return {
            "last_healthy_at": None if snapshot.last_healthy_at is None else snapshot.last_healthy_at.isoformat(),
            "health": snapshot.health.value,
            "fail_safe_obligation": snapshot.fail_safe_obligation,
            "fail_safe_pending": snapshot.fail_safe_pending,
        }


class HealthSensor(_SnapshotSensor):
    """Expose complete/degraded/fail-safe controller health."""

    _attr_name = "House Battery Control Health"
    _attr_unique_id = f"{DOMAIN}_health"
    _attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self):
        snapshot = self.coordinator.data
        return None if snapshot is None else snapshot.health.value

    @property
    def extra_state_attributes(self):
        snapshot = self.coordinator.data
        if snapshot is None:
            return None
        return {
            "guard_state": snapshot.guard_state,
            "guard_quality": snapshot.guard_quality,
            "source_quality": snapshot.source_quality,
            "issues": snapshot.issues,
            "unexpected_error": snapshot.unexpected_error,
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
