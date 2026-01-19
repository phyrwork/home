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
    CONF_PROFILE_FILE,
    CONF_START_BY,
    DOMAIN,
    PLATFORMS,
)
from .helpers import normalize_time


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_IMPORT_RATE_SENSOR): cv.string,
                        vol.Optional(CONF_EXPORT_RATE_SENSOR): cv.string,
                        vol.Optional(CONF_EXPORT_POWER_SENSOR): cv.string,
                        vol.Optional(CONF_PROFILE): vol.Any(cv.string, list),
                        vol.Optional(CONF_PROFILE_FILE): cv.string,
                        vol.Optional(CONF_START_BY): cv.string,
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

    for item in entries:
        data = dict(item)
        start_by = normalize_time(data.get(CONF_START_BY))
        data[CONF_START_BY] = start_by

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
    hass.data[DOMAIN][entry.entry_id] = {}
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
