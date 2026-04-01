from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntry
from homeassistant.util import slugify

from .const import (
    CONF_ACTIVE_POWER_THRESHOLD,
    CONF_BASELINE_RATE_SENSOR,
    CONF_CURRENT_RATE_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_NAME,
    CONF_POWER_INTEGRATION_METHOD,
    CONF_POWER_SENSOR,
    DEFAULT_ACTIVE_POWER_THRESHOLD,
    DEFAULT_POWER_INTEGRATION_METHOD,
    DOMAIN,
    PLATFORMS,
    POWER_INTEGRATION_METHODS,
)
from .coordinator import EnergyCostSavingTrackerCoordinator


_LOGGER = logging.getLogger(__name__)


def _validate_tracker_config(data: dict) -> dict:
    if bool(data.get(CONF_ENERGY_SENSOR)) == bool(data.get(CONF_POWER_SENSOR)):
        raise vol.Invalid("Exactly one of energy_sensor or power_sensor must be set")
    return data


TRACKER_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_ENERGY_SENSOR): cv.entity_id,
            vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
            vol.Required(CONF_CURRENT_RATE_SENSOR): cv.entity_id,
            vol.Required(CONF_BASELINE_RATE_SENSOR): cv.entity_id,
            vol.Optional(CONF_ACTIVE_POWER_THRESHOLD, default=DEFAULT_ACTIVE_POWER_THRESHOLD): vol.Coerce(
                float
            ),
            vol.Optional(
                CONF_POWER_INTEGRATION_METHOD,
                default=DEFAULT_POWER_INTEGRATION_METHOD,
            ): vol.In(POWER_INTEGRATION_METHODS),
        }
    ),
    _validate_tracker_config,
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(cv.ensure_list, [TRACKER_SCHEMA]),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    entries = config.get(DOMAIN)
    if not entries:
        return True

    existing_entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.source == SOURCE_IMPORT
    ]

    for item in entries:
        data = dict(item)
        unique_id = _unique_id(data)
        matching_entries = _find_matching_entries(existing_entries, data, unique_id)

        if matching_entries:
            matching_entries.sort(key=lambda entry: entry.entry_id)
            keeper = matching_entries[0]
            old_title = keeper.title
            hass.config_entries.async_update_entry(
                keeper,
                data=data,
                title=data[CONF_NAME],
                unique_id=unique_id,
            )
            if old_title != data[CONF_NAME]:
                _rename_entry_entities(hass, keeper.entry_id, old_title, data[CONF_NAME])
            for stale_entry in matching_entries[1:]:
                await hass.config_entries.async_remove(stale_entry.entry_id)
            continue

        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=data,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = EnergyCostSavingTrackerCoordinator(hass, entry)
    await coordinator.async_initialize()
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None) or {}
        coordinator: EnergyCostSavingTrackerCoordinator | None = entry_data.get("coordinator")
        if coordinator:
            await coordinator.async_shutdown()
    return unload_ok


def _unique_id(data: dict) -> str:
    return slugify(data[CONF_NAME])


def _find_matching_entries(
    existing_entries: list[ConfigEntry],
    data: dict,
    unique_id: str,
) -> list[ConfigEntry]:
    matching_entries = [
        entry for entry in existing_entries if entry.unique_id == unique_id
    ]
    if matching_entries:
        return matching_entries

    matching_entries = [
        entry for entry in existing_entries if entry.title == data[CONF_NAME]
    ]
    if matching_entries:
        return matching_entries

    target_signature = _tracker_signature(data)
    return [
        entry
        for entry in existing_entries
        if _tracker_signature(entry.data) == target_signature
    ]


def _tracker_signature(data: dict) -> tuple[str | None, str | None, str | None, str | None]:
    return (
        data.get(CONF_ENERGY_SENSOR),
        data.get(CONF_POWER_SENSOR),
        data.get(CONF_CURRENT_RATE_SENSOR),
        data.get(CONF_BASELINE_RATE_SENSOR),
    )


def _rename_entry_entities(
    hass: HomeAssistant,
    entry_id: str,
    old_title: str,
    new_title: str,
) -> None:
    old_slug = slugify(old_title)
    new_slug = slugify(new_title)
    if old_slug == new_slug:
        return

    entity_registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(entity_registry, entry_id):
        new_entity_id = _renamed_entity_id(entry, old_slug, new_slug)
        if new_entity_id is None:
            continue
        entity_registry.async_update_entity(
            entry.entity_id,
            new_entity_id=new_entity_id,
        )


def _renamed_entity_id(
    entry: RegistryEntry,
    old_slug: str,
    new_slug: str,
) -> str | None:
    prefix = f"{entry.domain}.{old_slug}_"
    if not entry.entity_id.startswith(prefix):
        return None
    return f"{entry.domain}.{new_slug}_{entry.entity_id.removeprefix(prefix)}"
