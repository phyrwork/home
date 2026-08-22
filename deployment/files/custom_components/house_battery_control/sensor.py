"""Small diagnostic surface for house-battery control."""

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Coordinator


async def async_setup_platform(
    hass: HomeAssistant,
    config: Mapping[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: Mapping[str, Any] | None = None,
) -> None:
    coordinator = hass.data.get(DOMAIN)
    if isinstance(coordinator, Coordinator):
        async_add_entities((HeartbeatSensor(coordinator), HealthSensor(coordinator), ActionSensor(coordinator), ReserveSensor(coordinator)))


class _SnapshotSensor(CoordinatorEntity[Coordinator], SensorEntity):
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
            "state_of_charge_percent": _float(data.state_of_charge_percent),
            "battery_power_kw": _float(data.battery_power_kw),
            "current_cheap_window": data.current_cheap_window,
            "next_cheap_window": data.next_cheap_window,
            "actuation": data.actuation_message,
            "last_error": data.last_error,
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


def _float(value: object) -> float | None:
    return None if value is None else float(value)


__all__ = ["ActionSensor", "HealthSensor", "HeartbeatSensor", "ReserveSensor", "async_setup_platform"]
