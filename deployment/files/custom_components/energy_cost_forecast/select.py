from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DATA_START_STEP_MODE,
    DEFAULT_START_MODE,
    DOMAIN,
    START_MODE_OPTIONS,
)
from .coordinator import EnergyCostForecastCoordinator


@dataclass(frozen=True)
class EnergyCostForecastSelectDescription:
    key: str
    name: str
    options: list[str]


SELECT_DESCRIPTIONS = [
    EnergyCostForecastSelectDescription(
        key=DATA_START_STEP_MODE,
        name="Start Step Mode",
        options=START_MODE_OPTIONS,
    )
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
            EnergyCostForecastSelect(entry, description, device_info)
            for description in SELECT_DESCRIPTIONS
        ]
    )


class EnergyCostForecastSelect(RestoreEntity, SelectEntity):
    def __init__(
        self,
        entry: ConfigEntry,
        description: EnergyCostForecastSelectDescription,
        device_info: DeviceInfo,
    ) -> None:
        self._entry_id = entry.entry_id
        self._description = description
        self._attr_name = description.name
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info
        self._attr_options = description.options

    @property
    def current_option(self) -> str | None:
        entry_data = self.hass.data[DOMAIN].get(self._entry_id, {})
        return entry_data.get(self._description.key)

    async def async_select_option(self, option: str) -> None:
        entry_data = self.hass.data[DOMAIN].get(self._entry_id, {})
        if option not in self._description.options:
            return
        entry_data[self._description.key] = option
        coordinator: EnergyCostForecastCoordinator = entry_data.get("coordinator")
        if coordinator:
            await coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        entry_data = self.hass.data[DOMAIN].get(self._entry_id, {})
        if self._description.key not in entry_data:
            last = await self.async_get_last_state()
            if last and last.state in self._description.options:
                entry_data[self._description.key] = last.state
                coordinator: EnergyCostForecastCoordinator = entry_data.get("coordinator")
                if coordinator:
                    await coordinator.async_request_refresh()
                return
            entry_data.setdefault(DATA_START_STEP_MODE, DEFAULT_START_MODE)
