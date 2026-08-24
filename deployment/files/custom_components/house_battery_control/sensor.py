"""Small diagnostic surface for house-battery control."""

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .config import DOMAIN
from .controller import Controller


async def async_setup_platform(
    hass: HomeAssistant,
    config: Mapping[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: Mapping[str, Any] | None = None,
) -> None:
    controller = hass.data.get(DOMAIN)
    if isinstance(controller, Controller):
        async_add_entities(
            (
                HeartbeatSensor(controller),
                HealthSensor(controller),
                ActionSensor(controller),
                ReserveSensor(controller),
                BatteryEnergySensor(controller),
                ReserveTargetSensor(controller),
                ReserveBalanceSensor(controller),
            )
        )


class _SnapshotSensor(CoordinatorEntity[Controller], SensorEntity):
    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class HeartbeatSensor(_SnapshotSensor):
    _attr_name = "House Battery Control Heartbeat"
    _attr_unique_id = f"{DOMAIN}_heartbeat"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.coordinator.data.heartbeat_at

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if data is None:
            return None
        return {
            "last_healthy_at": None if data.last_healthy_at is None else data.last_healthy_at.isoformat(),
            "last_error": data.last_error,
            "degraded_since": None if data.degraded_since is None else data.degraded_since.isoformat(),
            "fail_safe_since": None if data.fail_safe_since is None else data.fail_safe_since.isoformat(),
        }


class HealthSensor(_SnapshotSensor):
    _attr_name = "House Battery Control Health"
    _attr_unique_id = f"{DOMAIN}_health"
    _attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.coordinator.data.health.value


class ActionSensor(_SnapshotSensor):
    _attr_name = "House Battery Control Action"
    _attr_unique_id = f"{DOMAIN}_action"
    _attr_icon = "mdi:home-battery"

    @property
    def native_value(self):
        return None if self.coordinator.data is None else self.coordinator.data.action.value

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if data is None:
            return None
        return {
            "reason": data.reason,
            "cycle_state": data.cycle_state.value,
            "cycle_deadline": None if data.cycle_deadline is None else data.cycle_deadline.isoformat(),
            "charge_lease_deadline": None if data.charge_lease_deadline is None else data.charge_lease_deadline.isoformat(),
            "state_of_charge_percent": _float(data.state_of_charge_percent),
            "battery_power_kw": _float(data.battery_power_kw),
            "current_cheap_window": data.current_cheap_window,
            "next_cheap_window": data.next_cheap_window,
            "actuation": data.actuation_message,
            "last_error": data.last_error,
            "pending_operation": data.pending_operation,
            "attempt": data.attempt,
            "next_retry_at": None if data.next_retry_at is None else data.next_retry_at.isoformat(),
        }


class ReserveSensor(_SnapshotSensor):
    _attr_name = "House Battery Control Reserve"
    _attr_unique_id = f"{DOMAIN}_reserve"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-heart"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.reserve_soc_percent is not None

    @property
    def native_value(self):
        return _float(self.coordinator.data.reserve_soc_percent) if self.coordinator.data is not None else None


class _ReserveEnergySensor(_SnapshotSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def _value(self) -> object:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return super().available and self._value() is not None

    @property
    def native_value(self):
        return _float(self._value())


class BatteryEnergySensor(_ReserveEnergySensor):
    _attr_name = "House Battery Energy"
    _attr_unique_id = f"{DOMAIN}_energy"

    def _value(self) -> object:
        data = self.coordinator.data
        return None if data is None else data.battery_energy_kwh


class ReserveTargetSensor(_ReserveEnergySensor):
    _attr_name = "House Battery Reserve Target"
    _attr_unique_id = f"{DOMAIN}_reserve_target"

    def _value(self) -> object:
        data = self.coordinator.data
        return None if data is None else data.reserve_target_energy_kwh


class ReserveBalanceSensor(_ReserveEnergySensor):
    _attr_name = "House Battery Reserve Balance"
    _attr_unique_id = f"{DOMAIN}_reserve_balance"

    def _value(self) -> object:
        data = self.coordinator.data
        return None if data is None else data.reserve_balance_kwh


def _float(value: object) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "ActionSensor",
    "BatteryEnergySensor",
    "HealthSensor",
    "HeartbeatSensor",
    "ReserveBalanceSensor",
    "ReserveSensor",
    "ReserveTargetSensor",
    "async_setup_platform",
]
