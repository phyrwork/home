from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_EXPORT_POWER_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_NAME,
    CONF_PROFILE,
    CONF_POWER_PROFILE_FILE,
    CONF_POWER_PROFILE_ENTITY,
    CONF_START_AFTER,
    CONF_START_BEFORE,
    CONF_FINISH_AFTER,
    CONF_FINISH_BEFORE,
    CONF_START_STEP_MODE,
    CONF_START_STEP_MINUTES,
    CONF_TARGET_PERCENTILE,
    CONF_UPDATE_INTERVAL_MINUTES,
    DATA_START_STEP_MODE,
    DATA_START_STEP_MINUTES,
    DEFAULT_START_MODE,
    DEFAULT_START_STEP_MINUTES,
    DOMAIN,
    PLATFORMS,
    START_MODE_KEY_TO_LABEL,
    START_MODE_LABEL_TO_KEY,
    START_MODE_OPTIONS,
    DATA_TARGET_PERCENTILE,
    DEFAULT_TARGET_PERCENTILE,
)
from .coordinator import EnergyCostForecastCoordinator


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_IMPORT_RATE_SENSOR): cv.entity_id,
                        vol.Optional(CONF_EXPORT_RATE_SENSOR): cv.entity_id,
                        vol.Optional(CONF_EXPORT_POWER_SENSOR): cv.entity_id,
                        vol.Optional(CONF_PROFILE): vol.Any(cv.string, list),
                        vol.Optional(CONF_POWER_PROFILE_FILE): cv.string,
                        vol.Optional(CONF_POWER_PROFILE_ENTITY): cv.entity_id,
                        vol.Optional(CONF_TARGET_PERCENTILE): vol.All(
                            vol.Coerce(float),
                            vol.Range(min=0, max=100),
                        ),
                        vol.Optional(CONF_START_AFTER): cv.string,
                        vol.Optional(CONF_START_BEFORE): cv.string,
                        vol.Optional(CONF_FINISH_AFTER): cv.string,
                        vol.Optional(CONF_FINISH_BEFORE): cv.string,
                        vol.Optional(CONF_START_STEP_MODE): cv.string,
                        vol.Optional(CONF_START_STEP_MINUTES): vol.All(
                            vol.Coerce(int),
                            vol.Range(min=0, max=1440),
                        ),
                        vol.Optional(CONF_UPDATE_INTERVAL_MINUTES): vol.All(
                            vol.Coerce(int),
                            vol.Range(min=0, max=1440),
                        ),
                    }
                )
            ],
        )
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    entries = config.get(DOMAIN)
    if not entries:
        return True

    existing_entries = {
        entry.unique_id: entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.unique_id
    }

    for item in entries:
        data = dict(item)
        if "start_mode" in data and CONF_START_STEP_MODE not in data:
            data[CONF_START_STEP_MODE] = data["start_mode"]

        unique_id = f"{data[CONF_NAME]}::{data[CONF_IMPORT_RATE_SENSOR]}"
        entry = existing_entries.get(unique_id)
        if entry:
            merged = dict(entry.data)
            # TODO: Clear removed config keys on import (e.g., finish_before set to "")
            # so disabling constraints doesn't keep old values in the config entry.
            for key in (
                CONF_NAME,
                CONF_IMPORT_RATE_SENSOR,
                CONF_EXPORT_RATE_SENSOR,
                CONF_EXPORT_POWER_SENSOR,
                CONF_PROFILE,
                CONF_POWER_PROFILE_FILE,
                CONF_POWER_PROFILE_ENTITY,
                CONF_TARGET_PERCENTILE,
                CONF_START_AFTER,
                CONF_START_BEFORE,
                CONF_FINISH_AFTER,
                CONF_FINISH_BEFORE,
                CONF_START_STEP_MINUTES,
                CONF_START_STEP_MODE,
                CONF_UPDATE_INTERVAL_MINUTES,
            ):
                if key in data:
                    merged[key] = data[key]
            hass.config_entries.async_update_entry(entry, data=merged)
            continue

        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=data,
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = EnergyCostForecastCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    start_mode = entry.data.get(CONF_START_STEP_MODE, DEFAULT_START_MODE)
    if start_mode in START_MODE_KEY_TO_LABEL:
        start_mode = START_MODE_KEY_TO_LABEL[start_mode]
    elif start_mode in START_MODE_LABEL_TO_KEY:
        start_mode = start_mode
    else:
        start_mode = DEFAULT_START_MODE
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        DATA_TARGET_PERCENTILE: float(
            entry.data.get(CONF_TARGET_PERCENTILE, DEFAULT_TARGET_PERCENTILE)
        ),
        DATA_START_STEP_MINUTES: int(
            entry.data.get(CONF_START_STEP_MINUTES, DEFAULT_START_STEP_MINUTES)
        ),
        DATA_START_STEP_MODE: start_mode,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None) or {}
        unsub = entry_data.get("unsub")
        if unsub:
            unsub()
    return unload_ok
