from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    STATE_BASELINE_RATE,
    STATE_CURRENT_RATE,
    STATE_CURRENT_SAVINGS_RATE,
    STATE_LAST_TOTAL_ENERGY,
    STATE_LAST_UPDATED_TIME,
    STATE_TOTAL_ENERGY,
    STATE_TOTAL_SAVINGS,
)
from .coordinator import EnergyCostSavingTrackerCoordinator


@dataclass(frozen=True)
class EnergyCostSavingTrackerSensorDescription(SensorEntityDescription):
    pass


SENSORS = [
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_CURRENT_RATE,
        name="Current Rate",
    ),
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_BASELINE_RATE,
        name="Baseline Rate",
    ),
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_CURRENT_SAVINGS_RATE,
        name="Current Savings Rate",
    ),
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_TOTAL_ENERGY,
        name="Total Energy",
    ),
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_TOTAL_SAVINGS,
        name="Total Savings",
    ),
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_LAST_TOTAL_ENERGY,
        name="Last Total Energy",
    ),
    EnergyCostSavingTrackerSensorDescription(
        key=STATE_LAST_UPDATED_TIME,
        name="Last Updated Time",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyCostSavingTrackerCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
    )
    async_add_entities(
        [
            EnergyCostSavingTrackerSensor(coordinator, entry, description, device_info)
            for description in SENSORS
        ]
    )


class EnergyCostSavingTrackerSensor(
    CoordinatorEntity[EnergyCostSavingTrackerCoordinator], SensorEntity
):
    def __init__(
        self,
        coordinator: EnergyCostSavingTrackerCoordinator,
        entry: ConfigEntry,
        description: EnergyCostSavingTrackerSensorDescription,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = device_info
        if description.device_class:
            self._attr_device_class = description.device_class

    @property
    def native_unit_of_measurement(self) -> str | None:
        key = self.entity_description.key
        if key in {STATE_CURRENT_RATE, STATE_BASELINE_RATE, STATE_CURRENT_SAVINGS_RATE}:
            return "GBP/kWh"
        if key in {STATE_TOTAL_ENERGY, STATE_LAST_TOTAL_ENERGY}:
            return "kWh"
        if key == STATE_TOTAL_SAVINGS:
            return "GBP"
        return None

    @property
    def native_value(self) -> Any:
        value = self.coordinator.data.get(self.entity_description.key)
        if self.entity_description.key == STATE_LAST_UPDATED_TIME and value:
            return dt_util.parse_datetime(value)
        return value
