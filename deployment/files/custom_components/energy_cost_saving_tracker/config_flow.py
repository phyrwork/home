from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
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
    POWER_INTEGRATION_METHODS,
)


class EnergyCostSavingTrackerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            if bool(user_input.get(CONF_ENERGY_SENSOR)) == bool(
                user_input.get(CONF_POWER_SENSOR)
            ):
                errors["base"] = "energy_or_power_required"
            else:
                data = {key: value for key, value in user_input.items() if value not in ("", None)}
                unique_id = _unique_id(data)
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Optional(CONF_ENERGY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_CURRENT_RATE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_BASELINE_RATE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_ACTIVE_POWER_THRESHOLD,
                    default=DEFAULT_ACTIVE_POWER_THRESHOLD,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100000, step=1, mode="box")
                ),
                vol.Optional(
                    CONF_POWER_INTEGRATION_METHOD,
                    default=DEFAULT_POWER_INTEGRATION_METHOD,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=POWER_INTEGRATION_METHODS,
                        mode="dropdown",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_import(self, user_input):
        data = dict(user_input)
        unique_id = _unique_id(data)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME], data=data)


def _unique_id(data: dict) -> str:
    return slugify(data[CONF_NAME])
