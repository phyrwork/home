"""Control a house battery using tariff and energy forecasts."""

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant

from .config import Config, from_mapping
from .const import DOMAIN

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(dict, from_mapping),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up House Battery Control from YAML."""
    typed_config = config.get(DOMAIN)
    if not isinstance(typed_config, Config):
        return True
    hass.data[DOMAIN] = typed_config
    return True
