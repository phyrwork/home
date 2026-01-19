from __future__ import annotations

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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COSTS,
    ATTR_LATEST_FINISH,
    ATTR_LATEST_START,
    ATTR_PROFILE,
    ATTR_PROFILE_ERROR,
    ATTR_PROFILE_INPUT,
    ATTR_PROFILE_SOURCE,
    ATTR_RATE_SOURCE,
    CONF_EXPORT_POWER_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    DOMAIN,
)
from .coordinator import EnergyCostForecastCoordinator


SENSORS = [
    SensorEntityDescription(key="now", name="Start Now Cost", has_entity_name=True),
    SensorEntityDescription(key="later", name="Start Later Costs", has_entity_name=True),
    SensorEntityDescription(
        key="min_time",
        name="Next Lowest Start Cost Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        has_entity_name=True,
    ),
    SensorEntityDescription(key="min", name="Lowest Start Cost", has_entity_name=True),
    SensorEntityDescription(key="max", name="Highest Start Cost", has_entity_name=True),
    SensorEntityDescription(
        key="now_percentile",
        name="Start Now Cost Percentile",
        has_entity_name=True,
    ),
]

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = EnergyCostForecastCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    device_name = entry.title
    if not device_name.lower().endswith(" cost"):
        device_name = f"{device_name} Cost"
    device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=device_name)

    entities = [
        EnergyCostForecastSensor(coordinator, entry, description, device_info)
        for description in SENSORS
    ]
    async_add_entities(entities)

    watch_entities = [
        entry.data.get(CONF_IMPORT_RATE_SENSOR),
        entry.data.get(CONF_EXPORT_RATE_SENSOR),
        entry.data.get(CONF_EXPORT_POWER_SENSOR),
    ]
    watch_entities = [entity_id for entity_id in watch_entities if entity_id]

    if watch_entities:
        async def _handle_event(event):
            await coordinator.async_request_refresh()

        unsub = async_track_state_change_event(
            hass,
            watch_entities,
            _handle_event,
        )
        hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["unsub"] = unsub


class EnergyCostForecastSensor(
    CoordinatorEntity[EnergyCostForecastCoordinator], SensorEntity
):
    def __init__(
        self,
        coordinator: EnergyCostForecastCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info
        if description.device_class:
            self._attr_device_class = description.device_class

    @property
    def native_unit_of_measurement(self) -> str | None:
        key = self.entity_description.key
        if key in {"now", "min", "max"}:
            return self.coordinator.data.get("cost_unit")
        if key == "now_percentile":
            return "%"
        return None

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        key = self.entity_description.key
        if key == "later":
            return data.get("start_now_time")
        if key == "min_time":
            best = data.get("min_time")
            start = best.get("start") if best else None
            return dt_util.parse_datetime(start) if start else None
        return data.get(key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        attrs = {
            ATTR_PROFILE: data.get(ATTR_PROFILE),
            ATTR_PROFILE_INPUT: data.get(ATTR_PROFILE_INPUT),
            ATTR_PROFILE_SOURCE: data.get(ATTR_PROFILE_SOURCE),
            ATTR_PROFILE_ERROR: data.get(ATTR_PROFILE_ERROR),
            ATTR_LATEST_START: data.get(ATTR_LATEST_START),
            ATTR_LATEST_FINISH: data.get(ATTR_LATEST_FINISH),
        }
        key = self.entity_description.key
        if key == "now":
            attrs[ATTR_RATE_SOURCE] = data.get(ATTR_RATE_SOURCE)
            attrs["start_now_time"] = data.get("start_now_time")
        elif key == "later":
            costs = data.get("later", [])
            attrs[ATTR_COSTS] = costs
            attrs["cost_unit"] = data.get("cost_unit")
            attrs["rates"] = [
                {
                    "start": item.get("start"),
                    "end": item.get("finish"),
                    "value_inc_vat": item.get("cost"),
                }
                for item in costs
            ]
            if costs:
                values = [item.get("cost") for item in costs if item.get("cost") is not None]
                if values:
                    attrs["min_cost"] = min(values)
                    attrs["max_cost"] = max(values)
                    attrs["average_cost"] = round(sum(values) / len(values), 4)
        elif key == "min_time":
            best = data.get("min_time") or {}
            attrs.update(best)
        return attrs
