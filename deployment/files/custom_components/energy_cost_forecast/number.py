from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_START_STEP_MINUTES,
    DATA_TARGET_PERCENTILE,
    DEFAULT_START_STEP_MINUTES,
    DEFAULT_TARGET_PERCENTILE,
    DOMAIN,
)
from .coordinator import EnergyCostForecastCoordinator


@dataclass(frozen=True)
class EnergyCostForecastNumberDescription:
    key: str
    name: str
    min_value: float
    max_value: float
    step: float
    unit: str | None = None


NUMBER_DESCRIPTIONS = [
    EnergyCostForecastNumberDescription(
        key=DATA_TARGET_PERCENTILE,
        name="Target Percentile",
        min_value=0,
        max_value=100,
        step=1,
        unit="%",
    ),
    EnergyCostForecastNumberDescription(
        key=DATA_START_STEP_MINUTES,
        name="Start Step Minutes",
        min_value=1,
        max_value=1440,
        step=1,
        unit="min",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_name = entry.title
    if not device_name.lower().endswith(" cost"):
        device_name = f"{device_name} Cost"
    device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=device_name)
    async_add_entities(
        [
            EnergyCostForecastNumber(entry, description, device_info)
            for description in NUMBER_DESCRIPTIONS
        ]
    )


class EnergyCostForecastNumber(RestoreNumber, NumberEntity):
    def __init__(
        self,
        entry: ConfigEntry,
        description: EnergyCostForecastNumberDescription,
        device_info: DeviceInfo,
    ) -> None:
        self._entry_id = entry.entry_id
        self._description = description
        self._attr_name = description.name
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info
        self._attr_native_min_value = description.min_value
        self._attr_native_max_value = description.max_value
        self._attr_native_step = description.step
        self._attr_mode = NumberMode.BOX
        if description.unit:
            self._attr_native_unit_of_measurement = description.unit

    @property
    def native_value(self) -> float | None:
        entry_data = self.hass.data[DOMAIN].get(self._entry_id, {})
        return entry_data.get(self._description.key)

    async def async_set_native_value(self, value: float) -> None:
        entry_data = self.hass.data[DOMAIN].get(self._entry_id, {})
        entry_data[self._description.key] = float(value)
        coordinator: EnergyCostForecastCoordinator = entry_data.get("coordinator")
        if coordinator:
            await coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        entry_data = self.hass.data[DOMAIN].get(self._entry_id, {})
        if self._description.key not in entry_data:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                entry_data[self._description.key] = float(last.native_value)
                coordinator: EnergyCostForecastCoordinator = entry_data.get("coordinator")
                if coordinator:
                    await coordinator.async_request_refresh()
                return
            if self._description.key == DATA_TARGET_PERCENTILE:
                entry_data.setdefault(
                    DATA_TARGET_PERCENTILE, DEFAULT_TARGET_PERCENTILE
                )
            elif self._description.key == DATA_START_STEP_MINUTES:
                entry_data.setdefault(DATA_START_STEP_MINUTES, DEFAULT_START_STEP_MINUTES)
